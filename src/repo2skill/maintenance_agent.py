from __future__ import annotations

from dataclasses import dataclass
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import threading
from typing import Literal


PROBE_TIMEOUT_SECONDS = 5.0
PROBE_MAX_OUTPUT_BYTES = 256 * 1024
_READ_CHUNK_BYTES = 16 * 1024
_REQUIRED_HELP_SURFACES: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("stdin-prompt", ("stdin", "prompt")),
    ("output-schema", ("--output-schema",)),
    ("output-file", ("--output-last-message",)),
    ("read-only-sandbox", ("--sandbox", "read-only")),
    ("user-config", ("--ignore-user-config",)),
    ("working-directory", ("--cd",)),
)


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
        self, reason: Literal["timeout", "output-cap", "unavailable", "nonzero-exit"]
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
    ).lower()
    for capability, markers in _REQUIRED_HELP_SURFACES:
        if not all(marker in help_text for marker in markers):
            missing.append(capability)
    return AgentCapability(resolved, version, not missing, tuple(missing))


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
    try:
        process = subprocess.Popen(
            argv,
            cwd=cwd,
            shell=False,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=environment,
        )
    except OSError as error:
        raise _ProbeFailure("unavailable") from error
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
        returncode = process.wait(timeout=PROBE_TIMEOUT_SECONDS)
    except subprocess.TimeoutExpired as error:
        process.kill()
        process.wait()
        for reader in readers:
            reader.join()
        raise _ProbeFailure("timeout") from error
    for reader in readers:
        reader.join()
    if capture.exceeded:
        raise _ProbeFailure("output-cap")
    if returncode != 0:
        raise _ProbeFailure("nonzero-exit")
    return subprocess.CompletedProcess(
        argv, returncode, b"".join(stdout_chunks), b"".join(stderr_chunks)
    )
