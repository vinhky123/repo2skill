from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
import os
from pathlib import Path
import re
import signal
import shutil
import stat
import subprocess
import sys
import tempfile
import threading
import time
from typing import Literal

from .maintenance_models import ClaimOperation


PROMPT_MAX_BYTES = 1024 * 1024
SCHEMA_MAX_BYTES = 128 * 1024
OUTPUT_MAX_BYTES = 1024 * 1024
INVOCATION_MAX_PIPE_BYTES = 256 * 1024
INVOCATION_CLEANUP_RESERVE_SECONDS = 1.0


@dataclass(frozen=True, slots=True)
class AgentRequest:
    jobId: str
    snapshotRoot: Path
    promptBytes: bytes
    schemaBytes: bytes
    requestHash: str
    sourceRoot: Path | None = None


@dataclass(frozen=True, slots=True)
class AgentResponse:
    status: str
    outputBytes: bytes
    diagnostic: str | None
    exitCode: int | None = None


PROBE_TIMEOUT_SECONDS = 5.0
PROBE_MAX_OUTPUT_BYTES = 256 * 1024
_READ_CHUNK_BYTES = 16 * 1024
_CLEANUP_RESERVE_SECONDS = 0.5
_OPTION_LINE = re.compile(r"^\s{2,6}(?:-[A-Za-z],\s*)?--([\w-]+)\b")
_SECTION_LINE = re.compile(r"^[A-Za-z][A-Za-z0-9 /_-]*:\s*$")


@dataclass(frozen=True)
class AgentCapability:
    executable: Path | None
    version: str | None
    supported: bool
    missingCapabilities: tuple[str, ...]


class _Capture:
    def __init__(self, limit: int, process: subprocess.Popen[bytes]):
        self.limit = limit
        self.process = process
        self.total = 0
        self.exceeded = False
        self.lock = threading.Lock()

    def accept(self, chunk: bytes) -> bytes:
        with self.lock:
            remaining = self.limit - self.total
            accepted = chunk[: max(0, remaining)]
            self.total += len(accepted)
            if len(accepted) != len(chunk):
                self.exceeded = True
                try:
                    self.process.kill()
                except OSError:
                    pass
            return accepted


class _ProbeFailure(RuntimeError):
    def __init__(
        self,
        reason: Literal[
            "timeout", "output-cap", "unavailable", "nonzero-exit", "pipe-cleanup"
        ],
    ):
        self.reason = reason
        super().__init__(reason)


def probe_agent(executable: Path | None, *, source_root: Path | None = None) -> AgentCapability:
    """Check local Codex CLI help for the explicit maintenance invocation surface.

    This only inspects ``--version`` and ``exec --help``. It does not authenticate,
    select a model, contact a provider, or prove sandbox behavior.
    """
    resolved = _resolve_executable(executable, source_root=source_root)
    if resolved is None:
        return AgentCapability(None, None, False, ("executable",))

    missing: list[str] = []
    version: str | None = None
    try:
        with _clean_probe_directory() as cwd:
            version_result = _run_bounded(resolved, ("--version",), cwd)
            version = version_result.stdout.decode("utf-8", errors="replace").strip()
            if not version:
                missing.append("version-output")
            help_result = _run_bounded(resolved, ("exec", "--help"), cwd)
    except _ProbeFailure as error:
        missing.append(f"probe-{error.reason}")
        return AgentCapability(resolved, version, False, tuple(missing))
    except OSError:
        return AgentCapability(resolved, version, False, ("probe-unavailable",))

    help_text = (help_result.stdout + b"\n" + help_result.stderr).decode(
        "utf-8", errors="replace"
    )
    option_blocks = _option_blocks(_section(help_text, "Options"))
    if not _supports_stdin_prompt(help_text):
        missing.append("stdin-prompt")
    for flag, capability in (
        ("output-schema", "output-schema"),
        ("output-last-message", "output-file"),
        ("ignore-user-config", "user-config"),
        ("cd", "working-directory"),
        ("ephemeral", "ephemeral"),
        ("skip-git-repo-check", "skip-git-repo-check"),
    ):
        if flag not in option_blocks:
            missing.append(capability)
    sandbox_help = option_blocks.get("sandbox", "")
    if not _supports_read_only_sandbox(sandbox_help):
        missing.append("read-only-sandbox")
    return AgentCapability(resolved, version, not missing, tuple(missing))


def _section(help_text: str, name: str) -> str:
    lines = help_text.splitlines()
    for index, line in enumerate(lines):
        if line.strip().casefold() == f"{name}:".casefold():
            end = index + 1
            while end < len(lines) and not _SECTION_LINE.match(lines[end]):
                end += 1
            return "\n".join(lines[index + 1 : end])
    return ""


def _option_blocks(options_text: str) -> dict[str, str]:
    lines = options_text.splitlines()
    starts = [
        (index, match.group(1).casefold())
        for index, line in enumerate(lines)
        if (match := _OPTION_LINE.match(line))
    ]
    blocks: dict[str, str] = {}
    for position, (start, name) in enumerate(starts):
        end = starts[position + 1][0] if position + 1 < len(starts) else len(lines)
        blocks[name] = "\n".join(lines[start:end]).casefold()
    return blocks


def _supports_stdin_prompt(help_text: str) -> bool:
    prompt_block = _argument_block(help_text, "PROMPT")
    if not prompt_block:
        return False
    text = prompt_block.casefold()
    stdin_source = (
        r"(?:the\s+)?(?:instructions|prompt)\s+"
        r"(?:are|is)\s+read\s+from\s+stdin"
    )
    accepts_omitted = re.search(
        rf"\bif\s+not provided as an argument"
        rf"(?:\s*\(\s*or\s+if\s+`?-`?\s+is\s+used\s*\))?,"
        rf"\s*{stdin_source}\b",
        text,
    )
    accepts_dash = re.search(
        rf"\bif\s+`?-`?\s+is\s+used,\s*{stdin_source}\b", text
    )
    return bool(accepts_omitted or accepts_dash)


def _argument_block(help_text: str, argument: str) -> str:
    arguments = _section(help_text, "Arguments")
    lines = arguments.splitlines()
    expected = f"[{argument}]".casefold()
    for index, line in enumerate(lines):
        if line.strip().casefold().split()[0:1] == [expected]:
            end = index + 1
            while end < len(lines):
                if re.match(r"^\s{2,4}\[[A-Z][A-Z0-9_-]*\](?:\s|$)", lines[end]):
                    break
                end += 1
            return "\n".join(lines[index:end])
    return ""


def _supports_read_only_sandbox(sandbox_help: str) -> bool:
    if not sandbox_help:
        return False
    values = re.search(r"\[possible values\s*:\s*([^\]]+)\]", sandbox_help)
    if values is None:
        return False
    choices = [choice.strip().casefold() for choice in values.group(1).split(",")]
    return all(re.fullmatch(r"[a-z][a-z0-9_-]*", choice) for choice in choices) and (
        "read-only" in choices
    )


def _resolve_executable(executable: Path | None, *, source_root: Path | None = None) -> Path | None:
    if executable is None:
        excluded_roots = {Path(__file__).resolve().parents[2]}
        requested_root = Path.cwd() if source_root is None else Path(source_root)
        try:
            excluded_roots.add(requested_root.resolve(strict=True))
        except OSError:
            return None
        safe_entries: list[str] = []
        for entry in os.environ.get("PATH", "").split(os.pathsep):
            if not entry:
                continue
            candidate = Path(entry)
            if not candidate.is_absolute():
                continue
            try:
                resolved = candidate.resolve(strict=True)
            except OSError:
                continue
            if any(resolved == root or root in resolved.parents for root in excluded_roots):
                continue
            safe_entries.append(str(resolved))
        found = shutil.which("codex", path=os.pathsep.join(safe_entries))
        if not found:
            return None
        try:
            resolved = Path(found).resolve(strict=True)
        except OSError:
            return None
        if any(resolved == root or root in resolved.parents for root in excluded_roots):
            return None
        return resolved if resolved.is_file() else None
    try:
        path = executable.expanduser().resolve(strict=True)
    except OSError:
        return None
    return path if path.is_file() else None


def request_hash(job_id: str, prompt_bytes: bytes, schema_bytes: bytes) -> str:
    return "sha256:" + hashlib.sha256(job_id.encode("utf-8") + b"\0" + prompt_bytes + b"\0" + schema_bytes).hexdigest()


def invoke_agent(capability: AgentCapability, request: AgentRequest, timeout_seconds: float) -> AgentResponse:
    """Run the probed read-only CLI surface with bounded stdin, pipes, and output file."""
    if not capability.supported or capability.executable is None:
        return AgentResponse("unavailable", b"", "agent capability is unavailable: " + ",".join(capability.missingCapabilities))
    if (not isinstance(request.jobId, str) or not request.jobId or not isinstance(request.promptBytes, bytes)
            or not isinstance(request.schemaBytes, bytes) or not isinstance(request.snapshotRoot, Path)
            or (request.sourceRoot is not None and not isinstance(request.sourceRoot, Path))
            or isinstance(timeout_seconds, bool)):
        return AgentResponse("invalid-request", b"", "request fields or timeout have invalid types")
    if len(request.promptBytes) > PROMPT_MAX_BYTES or len(request.schemaBytes) > SCHEMA_MAX_BYTES:
        return AgentResponse("input-cap", b"", "prompt or schema exceeds configured byte limit")
    if request.requestHash != request_hash(request.jobId, request.promptBytes, request.schemaBytes):
        return AgentResponse("invalid-request", b"", "request hash does not match job, prompt, and schema bytes")
    if not isinstance(timeout_seconds, (int, float)) or timeout_seconds <= 0:
        return AgentResponse("invalid-request", b"", "timeout must be positive")
    try:
        if request.sourceRoot is None:
            return AgentResponse("invalid-request", b"", "target source root is required for safe invocation cwd selection")
        source_root = request.sourceRoot.resolve(strict=True)
        if not source_root.is_dir():
            return AgentResponse("invalid-request", b"", "target source root must be an existing directory")
        snapshot = request.snapshotRoot.resolve(strict=True)
        if not snapshot.is_dir():
            raise ValueError("snapshotRoot is not a directory")
        package_root = Path(__file__).resolve().parents[2]
        temp_base = Path(tempfile.gettempdir()).resolve(strict=True)
        if _inside(temp_base, source_root) or _inside(temp_base, package_root):
            return AgentResponse("unavailable", b"", "temporary directory base must be outside the target and package source repositories")
        with tempfile.TemporaryDirectory(prefix="repo2skill-agent-") as temp_name:
            workspace = Path(temp_name).resolve()
            if _inside(workspace, source_root) or _inside(workspace, package_root):
                return AgentResponse("unavailable", b"", "temporary invocation cwd must be outside source repositories")
            if (workspace / "AGENTS.md").exists() or (workspace / "AGENTS.md").is_symlink():
                return AgentResponse("unavailable", b"", "temporary invocation cwd contains repository instructions")
            schema_path = workspace / "schema.json"
            output_path = workspace / "response.json"
            with schema_path.open("xb") as stream:
                stream.write(request.schemaBytes)
                stream.flush()
                os.fsync(stream.fileno())
            argv = _command(capability.executable, (
                "exec", "--ephemeral", "--skip-git-repo-check", "--sandbox", "read-only",
                "--ignore-user-config", "--cd", str(workspace), "--output-schema", str(schema_path),
                "--output-last-message", str(output_path), "-",
            ))
            deadline = time.monotonic() + float(timeout_seconds)
            reserve = min(INVOCATION_CLEANUP_RESERVE_SECONDS, float(timeout_seconds) / 4)
            run_deadline = deadline - reserve
            environment = os.environ.copy()
            process_options: dict[str, object] = {}
            if os.name == "nt":
                process_options["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP
            else:
                process_options["start_new_session"] = True
            try:
                process = subprocess.Popen(argv, cwd=workspace, shell=False, stdin=subprocess.PIPE,
                    stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=environment, **process_options)
            except OSError as error:
                return AgentResponse("unavailable", b"", f"agent could not be started: {error}")
            assert process.stdin is not None
            kill_job = _create_windows_kill_job(process)
            output_overflow = threading.Event()
            watcher_stop = threading.Event()
            def watch_output() -> None:
                while not watcher_stop.wait(0.02):
                    try:
                        info = output_path.lstat()
                        if not stat.S_ISREG(info.st_mode) or info.st_size > OUTPUT_MAX_BYTES:
                            output_overflow.set()
                            process.kill()
                            return
                    except FileNotFoundError:
                        pass
                    except OSError:
                        output_overflow.set()
                        process.kill()
                        return
            watcher = threading.Thread(target=watch_output, daemon=True)
            watcher.start()
            write_error: list[BaseException] = []
            def write_prompt() -> None:
                try:
                    process.stdin.write(request.promptBytes)
                    process.stdin.close()
                except (BrokenPipeError, OSError) as error:
                    write_error.append(error)
            writer = threading.Thread(target=write_prompt, daemon=True)
            writer.start()
            try:
                result = _collect_invocation(process, argv, deadline, run_deadline, kill_job)
            except _ProbeFailure as error:
                watcher_stop.set()
                watcher.join(timeout=max(0.01, deadline - time.monotonic()))
                writer.join(timeout=max(0.01, deadline - time.monotonic()))
                return AgentResponse(error.reason, b"", f"agent invocation failed: {error.reason}")
            finally:
                watcher_stop.set()
                watcher.join(timeout=max(0.01, deadline - time.monotonic()))
                writer.join(timeout=max(0.01, deadline - time.monotonic()))
                _close_windows_kill_job(kill_job)
            if output_overflow.is_set():
                return AgentResponse("output-cap", b"", "structured output file exceeded byte limit or was redirected")
            if write_error:
                return AgentResponse("input-error", b"", "could not send the complete prompt to the agent")
            if result.returncode != 0:
                return AgentResponse("nonzero-exit", b"", f"agent exited with status {result.returncode}", result.returncode)
            try:
                before = output_path.lstat()
                if not stat.S_ISREG(before.st_mode) or before.st_size > OUTPUT_MAX_BYTES:
                    return AgentResponse("output-cap", b"", "structured output is not a bounded regular file")
                flags = os.O_RDONLY | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOFOLLOW", 0)
                descriptor = os.open(output_path, flags)
                with os.fdopen(descriptor, "rb") as stream:
                    opened_before = os.fstat(stream.fileno())
                    output = stream.read(OUTPUT_MAX_BYTES + 1)
                    opened_after = os.fstat(stream.fileno())
                after = output_path.lstat()
                stable = (opened_before.st_dev, opened_before.st_ino, opened_before.st_size, opened_before.st_mtime_ns) == (
                    opened_after.st_dev, opened_after.st_ino, opened_after.st_size, opened_after.st_mtime_ns)
                same_file = (before.st_dev, before.st_ino) == (opened_before.st_dev, opened_before.st_ino)
                if (len(output) > OUTPUT_MAX_BYTES or not stat.S_ISREG(opened_before.st_mode)
                        or not stat.S_ISREG(after.st_mode) or not stable or not same_file
                        or before.st_size != after.st_size):
                    return AgentResponse("output-cap", b"", "structured output changed or exceeded byte limit")
            except FileNotFoundError:
                return AgentResponse("missing-output", b"", "agent did not create the structured output file")
            except OSError as error:
                return AgentResponse("output-invalid", b"", f"could not safely read structured output: {error}")
            diagnostic_bytes = result.stderr[:8192]
            diagnostic = diagnostic_bytes.decode("utf-8", errors="ignore") or None
            return AgentResponse("ok", output, diagnostic, result.returncode)
    except (OSError, ValueError) as error:
        return AgentResponse("unavailable", b"", str(error))


def _inside(path: Path, root: Path) -> bool:
    return path == root or root in path.parents


def _collect_invocation(process: subprocess.Popen[bytes], argv: list[str], deadline: float,
                        run_deadline: float, kill_job: tuple[object, object] | None) -> subprocess.CompletedProcess[bytes]:
    assert process.stdout is not None and process.stderr is not None
    capture = _Capture(INVOCATION_MAX_PIPE_BYTES, process)
    stdout_chunks: list[bytes] = []
    stderr_chunks: list[bytes] = []
    def drain(stream: object, destination: list[bytes]) -> None:
        read = getattr(stream, "read")
        while True:
            chunk = read(_READ_CHUNK_BYTES)
            if not chunk:
                return
            accepted = capture.accept(chunk)
            if accepted:
                destination.append(accepted)
            if capture.exceeded:
                return
    readers = (threading.Thread(target=drain, args=(process.stdout, stdout_chunks), daemon=True),
               threading.Thread(target=drain, args=(process.stderr, stderr_chunks), daemon=True))
    for reader in readers: reader.start()
    try:
        returncode = process.wait(timeout=max(0.0, run_deadline - time.monotonic()))
    except subprocess.TimeoutExpired as error:
        _terminate_process_tree(process, deadline, kill_job)
        _join_readers(readers, deadline)
        raise _ProbeFailure("timeout") from error
    _join_readers(readers, run_deadline)
    if any(reader.is_alive() for reader in readers):
        _terminate_process_tree(process, deadline, kill_job)
        _join_readers(readers, deadline)
        raise _ProbeFailure("pipe-cleanup")
    if capture.exceeded:
        raise _ProbeFailure("output-cap")
    return subprocess.CompletedProcess(argv, returncode, b"".join(stdout_chunks), b"".join(stderr_chunks))


def validate_operations(response: AgentResponse, context) -> tuple[ClaimOperation, ...]:
    """Validate a complete response transaction against one committed context."""
    if response.status != "ok":
        raise ValueError(f"agent response is not successful: {response.status}")
    if len(response.outputBytes) > OUTPUT_MAX_BYTES:
        raise ValueError("agent response exceeds byte limit")
    def unique_object(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise ValueError(f"duplicate JSON key: {key}")
            result[key] = value
        return result
    try:
        raw = json.loads(response.outputBytes.decode("utf-8"),
            parse_constant=lambda value: (_ for _ in ()).throw(ValueError(value)),
            object_pairs_hook=unique_object)
    except (UnicodeError, json.JSONDecodeError, ValueError) as error:
        raise ValueError("agent response is not complete valid JSON") from error
    if not isinstance(raw, dict) or set(raw) != {"jobId", "snapshotId", "operations"}:
        raise ValueError("agent response envelope has unexpected fields")
    if raw["jobId"] != context.job.jobId or raw["snapshotId"] != context.snapshot.snapshotId:
        raise ValueError("agent response job or snapshot binding does not match")
    if not isinstance(raw["operations"], list):
        raise ValueError("agent operations must be an array")
    known_facts = {fact.factId: fact for fact in context.facts}
    known_evidence = {item.evidenceId: item for item in context.evidence}
    prior_by_ref = {f"{claim.claimId}@{claim.claimRevision}": claim for claim in context.priorClaims}
    operations: list[ClaimOperation] = []
    seen: set[tuple[str, str]] = set()
    for item in raw["operations"]:
        if not isinstance(item, dict) or set(item) != {"op", "semanticKey", "scope", "renderedText", "factIds", "evidenceIds", "claimRef"}:
            raise ValueError("operation has missing or unexpected fields")
        try:
            operation = ClaimOperation(item["op"], item["semanticKey"], item["scope"], item["renderedText"],
                tuple(item["factIds"]), tuple(item["evidenceIds"]), item["claimRef"])
        except (TypeError, ValueError) as error:
            raise ValueError("operation fields are invalid") from error
        identity = (operation.scope, operation.semanticKey)
        if identity in seen:
            raise ValueError("duplicate maintenance operation")
        seen.add(identity)
        if operation.op in {"add", "update"}:
            if not operation.factIds or tuple(sorted(set(operation.factIds))) != operation.factIds:
                raise ValueError("operation fact IDs must be sorted, unique, and nonempty")
            facts = [known_facts.get(fact_id) for fact_id in operation.factIds]
            if any(fact is None or fact.snapshotId != context.snapshot.snapshotId or fact.scope != operation.scope or fact.factType != "source.fragment.v1" for fact in facts):
                raise ValueError("operation references a fact outside the committed snapshot")
            expected_evidence = tuple(sorted({evidence_id for fact in facts for evidence_id in fact.evidenceIds}))
            if operation.evidenceIds != expected_evidence or any(evidence_id not in known_evidence or known_evidence[evidence_id].snapshotId != context.snapshot.snapshotId for evidence_id in expected_evidence):
                raise ValueError("operation evidence does not exactly support its facts")
            if not operation.renderedText or not operation.renderedText.strip():
                raise ValueError("operation text must be nonempty")
            if operation.op == "add" and operation.claimRef is not None:
                raise ValueError("add operation cannot reference a prior claim")
            if operation.op == "update":
                if operation.claimRef not in prior_by_ref:
                    raise ValueError("update operation references an unknown prior claim")
                prior = prior_by_ref[operation.claimRef]
                if prior.scope != operation.scope or prior.semanticKey != operation.semanticKey:
                    raise ValueError("update operation binding does not match the prior claim")
                if (getattr(prior, "kind", None) != "inferred"
                        or getattr(prior, "origin", None) != "llm"
                        or getattr(prior, "supportMode", None) != "evidence-backed-prose"):
                    raise ValueError("update operations may edit only prior model-authored prose claims")
        else:
            if operation.renderedText is not None or operation.factIds or operation.evidenceIds:
                raise ValueError("delete operation must not include generated text or support")
            if operation.claimRef not in prior_by_ref:
                raise ValueError("delete operation references an unknown prior claim")
            prior = prior_by_ref[operation.claimRef]
            if prior.scope != operation.scope or prior.semanticKey != operation.semanticKey:
                raise ValueError("delete operation binding does not match the prior claim")
        operations.append(operation)
    return tuple(operations)


def _command(executable: Path, arguments: tuple[str, ...]) -> list[str]:
    if executable.suffix.lower() == ".py":
        return [sys.executable, str(executable), *arguments]
    return [str(executable), *arguments]


def _clean_probe_directory() -> tempfile.TemporaryDirectory[str]:
    source_root = Path(__file__).resolve().parents[2]
    temporary = tempfile.TemporaryDirectory(prefix="repo2skill-codex-probe-")
    cwd = Path(temporary.name).resolve()
    if cwd == source_root or source_root in cwd.parents:
        temporary.cleanup()
        raise _ProbeFailure("unavailable")
    return temporary


def _run_bounded(
    executable: Path, arguments: tuple[str, ...], cwd: str
) -> subprocess.CompletedProcess[bytes]:
    environment = os.environ.copy()
    argv = _command(executable, arguments)
    deadline = time.monotonic() + PROBE_TIMEOUT_SECONDS
    run_deadline = deadline - min(_CLEANUP_RESERVE_SECONDS, PROBE_TIMEOUT_SECONDS / 4)
    process_options: dict[str, object] = {}
    if os.name == "nt":
        process_options["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP
    else:
        process_options["start_new_session"] = True
    try:
        process = subprocess.Popen(
            argv,
            cwd=cwd,
            shell=False,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=environment,
            **process_options,
        )
    except OSError as error:
        raise _ProbeFailure("unavailable") from error
    assert process.stdout is not None and process.stderr is not None

    kill_job = _create_windows_kill_job(process)
    try:
        return _collect_bounded_process(process, argv, deadline, run_deadline, kill_job)
    finally:
        _close_windows_kill_job(kill_job)


def _collect_bounded_process(
    process: subprocess.Popen[bytes],
    argv: list[str],
    deadline: float,
    run_deadline: float,
    kill_job: tuple[object, object] | None,
) -> subprocess.CompletedProcess[bytes]:
    assert process.stdout is not None and process.stderr is not None

    capture = _Capture(PROBE_MAX_OUTPUT_BYTES, process)
    stdout_chunks: list[bytes] = []
    stderr_chunks: list[bytes] = []

    def drain(stream: object, destination: list[bytes]) -> None:
        read = getattr(stream, "read")
        while True:
            chunk = read(_READ_CHUNK_BYTES)
            if not chunk:
                return
            accepted = capture.accept(chunk)
            if accepted:
                destination.append(accepted)
            if capture.exceeded:
                return

    readers = (
        threading.Thread(target=drain, args=(process.stdout, stdout_chunks), daemon=True),
        threading.Thread(target=drain, args=(process.stderr, stderr_chunks), daemon=True),
    )
    for reader in readers:
        reader.start()
    try:
        returncode = process.wait(timeout=max(0.0, run_deadline - time.monotonic()))
    except subprocess.TimeoutExpired as error:
        _terminate_process_tree(process, deadline, kill_job)
        _join_readers(readers, deadline)
        raise _ProbeFailure("timeout") from error
    _join_readers(readers, run_deadline)
    if any(reader.is_alive() for reader in readers):
        _terminate_process_tree(process, deadline, kill_job)
        _join_readers(readers, deadline)
        raise _ProbeFailure("pipe-cleanup")
    if capture.exceeded:
        raise _ProbeFailure("output-cap")
    if returncode != 0:
        raise _ProbeFailure("nonzero-exit")
    return subprocess.CompletedProcess(
        argv, returncode, b"".join(stdout_chunks), b"".join(stderr_chunks)
    )


def _join_readers(readers: tuple[threading.Thread, ...], deadline: float) -> None:
    for reader in readers:
        reader.join(timeout=max(0.0, deadline - time.monotonic()))


def _create_windows_kill_job(
    process: subprocess.Popen[bytes],
) -> tuple[object, object] | None:
    if os.name != "nt":
        return None
    import ctypes
    from ctypes import wintypes

    class BasicLimitInformation(ctypes.Structure):
        _fields_ = [
            ("PerProcessUserTimeLimit", ctypes.c_longlong),
            ("PerJobUserTimeLimit", ctypes.c_longlong),
            ("LimitFlags", wintypes.DWORD),
            ("MinimumWorkingSetSize", ctypes.c_size_t),
            ("MaximumWorkingSetSize", ctypes.c_size_t),
            ("ActiveProcessLimit", wintypes.DWORD),
            ("Affinity", ctypes.c_size_t),
            ("PriorityClass", wintypes.DWORD),
            ("SchedulingClass", wintypes.DWORD),
        ]

    class IoCounters(ctypes.Structure):
        _fields_ = [(name, ctypes.c_ulonglong) for name in (
            "ReadOperationCount", "WriteOperationCount", "OtherOperationCount",
            "ReadTransferCount", "WriteTransferCount", "OtherTransferCount",
        )]

    class ExtendedLimitInformation(ctypes.Structure):
        _fields_ = [
            ("BasicLimitInformation", BasicLimitInformation),
            ("IoInfo", IoCounters),
            ("ProcessMemoryLimit", ctypes.c_size_t),
            ("JobMemoryLimit", ctypes.c_size_t),
            ("PeakProcessMemoryUsed", ctypes.c_size_t),
            ("PeakJobMemoryUsed", ctypes.c_size_t),
        ]

    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.CreateJobObjectW.argtypes = [wintypes.LPVOID, wintypes.LPCWSTR]
    kernel.CreateJobObjectW.restype = wintypes.HANDLE
    kernel.SetInformationJobObject.argtypes = [
        wintypes.HANDLE, ctypes.c_int, wintypes.LPVOID, wintypes.DWORD,
    ]
    kernel.SetInformationJobObject.restype = wintypes.BOOL
    kernel.AssignProcessToJobObject.argtypes = [wintypes.HANDLE, wintypes.HANDLE]
    kernel.AssignProcessToJobObject.restype = wintypes.BOOL
    kernel.TerminateJobObject.argtypes = [wintypes.HANDLE, wintypes.UINT]
    kernel.TerminateJobObject.restype = wintypes.BOOL
    kernel.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel.CloseHandle.restype = wintypes.BOOL

    handle = kernel.CreateJobObjectW(None, None)
    if not handle:
        return None
    information = ExtendedLimitInformation()
    information.BasicLimitInformation.LimitFlags = 0x2000  # KILL_ON_JOB_CLOSE
    configured = kernel.SetInformationJobObject(
        handle, 9, ctypes.byref(information), ctypes.sizeof(information)
    )
    assigned = configured and kernel.AssignProcessToJobObject(
        handle, wintypes.HANDLE(int(process._handle))
    )
    if not assigned:
        kernel.CloseHandle(handle)
        return None
    return kernel, handle


def _close_windows_kill_job(kill_job: tuple[object, object] | None) -> None:
    if kill_job is None:
        return
    kernel, handle = kill_job
    getattr(kernel, "CloseHandle")(handle)


def _terminate_process_tree(
    process: subprocess.Popen[bytes],
    deadline: float,
    kill_job: tuple[object, object] | None,
) -> None:
    if os.name == "nt":
        if kill_job is not None:
            kernel, handle = kill_job
            getattr(kernel, "TerminateJobObject")(handle, 1)
        remaining = max(0.01, deadline - time.monotonic())
        try:
            subprocess.run(
                ["taskkill", "/F", "/T", "/PID", str(process.pid)],
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=remaining,
                check=False,
            )
        except (OSError, subprocess.TimeoutExpired):
            pass
    else:
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
    if process.poll() is None:
        try:
            process.kill()
        except OSError:
            pass
        try:
            process.wait(timeout=max(0.0, deadline - time.monotonic()))
        except subprocess.TimeoutExpired:
            pass
