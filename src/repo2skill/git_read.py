from __future__ import annotations

from collections.abc import Sequence
import os
from pathlib import Path
import subprocess
import threading
from typing import Literal


DEFAULT_GIT_TIMEOUT_SECONDS = 10.0
DEFAULT_GIT_OUTPUT_LIMIT = 16 * 1024 * 1024
_READ_CHUNK = 64 * 1024


class GitReadError(RuntimeError):
    """A bounded read-only Git invocation did not produce a usable result."""

    def __init__(
        self,
        reason: Literal["timeout", "output-cap", "unavailable", "return-code"],
        *,
        returncode: int | None = None,
    ):
        self.reason = reason
        self.returncode = returncode
        super().__init__(f"bounded Git read failed: {reason}")


class _CombinedCapture:
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


def run_git_bounded(
    root: Path,
    arguments: Sequence[str],
    *,
    allowed_returncodes: Sequence[int] = (0,),
    timeout_seconds: float = DEFAULT_GIT_TIMEOUT_SECONDS,
    max_output_bytes: int = DEFAULT_GIT_OUTPUT_LIMIT,
) -> subprocess.CompletedProcess[bytes]:
    """Run a fixed Git built-in with bounded combined stdout/stderr capture."""
    if not arguments or max_output_bytes < 1 or timeout_seconds <= 0:
        raise ValueError("Git arguments and positive time/output bounds are required")
    argv = [
        "git",
        "-c",
        "core.fsmonitor=false",
        "--no-pager",
        "-C",
        str(root),
        *arguments,
    ]
    environment = os.environ.copy()
    environment.update({"GIT_OPTIONAL_LOCKS": "0", "GIT_TERMINAL_PROMPT": "0"})
    try:
        process = subprocess.Popen(
            argv,
            shell=False,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=environment,
        )
    except OSError as error:
        raise GitReadError("unavailable") from error
    assert process.stdout is not None and process.stderr is not None

    capture = _CombinedCapture(max_output_bytes, process)
    stdout_chunks: list[bytes] = []
    stderr_chunks: list[bytes] = []

    def drain(stream: object, destination: list[bytes]) -> None:
        read = getattr(stream, "read")
        while True:
            chunk = read(_READ_CHUNK)
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
        returncode = process.wait(timeout=timeout_seconds)
    except subprocess.TimeoutExpired as error:
        process.kill()
        process.wait()
        for reader in readers:
            reader.join()
        raise GitReadError("timeout") from error
    for reader in readers:
        reader.join()
    if capture.exceeded:
        raise GitReadError("output-cap")
    stdout = b"".join(stdout_chunks)
    stderr = b"".join(stderr_chunks)
    if returncode not in allowed_returncodes:
        raise GitReadError("return-code", returncode=returncode)
    return subprocess.CompletedProcess(argv, returncode, stdout, stderr)
