from __future__ import annotations

from dataclasses import dataclass
import os
from pathlib import Path
import re
import signal
import shutil
import subprocess
import sys
import tempfile
import threading
import time
from typing import Literal


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


def probe_agent(executable: Path | None) -> AgentCapability:
    """Check local Codex CLI help for the explicit maintenance invocation surface.

    This only inspects ``--version`` and ``exec --help``. It does not authenticate,
    select a model, contact a provider, or prove sandbox behavior.
    """
    resolved = _resolve_executable(executable)
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


def _resolve_executable(executable: Path | None) -> Path | None:
    if executable is None:
        found = shutil.which("codex")
        return Path(found).resolve() if found else None
    try:
        path = executable.expanduser().resolve(strict=True)
    except OSError:
        return None
    return path if path.is_file() else None


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
