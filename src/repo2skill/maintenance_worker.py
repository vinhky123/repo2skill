"""OS-leased maintenance worker with a safe unavailable-agent seam."""
from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass
import os
from pathlib import Path
import sys
from typing import Iterator

from .ledger import LedgerStore, LockBusyError
from .maintenance_store import MaintenanceStore


@dataclass(frozen=True, slots=True)
class WorkerResult:
    status: str
    jobId: str | None = None
    message: str = ""


@contextmanager
def _worker_lease(root: Path, *, state_dir: Path | None = None) -> Iterator[None]:
    """Hold a persistent OS file lock; the OS releases it when an owner dies."""
    lease_dir = (Path(state_dir).absolute() if state_dir is not None else Path(root).absolute() / ".repo2skill") / "maintenance"
    lease_dir.mkdir(parents=True, exist_ok=True)
    path = lease_dir / "worker.lock"
    stream = path.open("a+b")
    try:
        if os.name == "nt":
            import msvcrt
            stream.seek(0)
            if path.stat().st_size == 0:
                stream.write(b"\0")
                stream.flush()
            stream.seek(0)
            try:
                msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
            except OSError as error:
                raise LockBusyError("maintenance worker lease is held") from error
        else:
            import fcntl
            try:
                fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError as error:
                raise LockBusyError("maintenance worker lease is held") from error
        try:
            yield
        finally:
            stream.seek(0)
            if os.name == "nt":
                import msvcrt
                msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl
                fcntl.flock(stream.fileno(), fcntl.LOCK_UN)
    finally:
        stream.close()


def run_worker(
    store: LedgerStore, *, foreground: bool = False, job_id: str | None = None,
) -> WorkerResult:
    """Claim only explicitly approved work and fail safely until Task 7/8 ships."""
    try:
        with _worker_lease(store.root, state_dir=store.state_dir):
            maintenance = MaintenanceStore(store.root, store)
            initial = maintenance.read_state()
            if not initial.enabled:
                return WorkerResult("disabled", job_id, "maintenance is disabled; run maintain setup first")
            selected = job_id
            if selected is None:
                selected = next(
                    (candidate for candidate, lifecycle in initial.jobs.items()
                     if lifecycle.status == "approved"),
                    None,
                )
            if selected is None:
                return WorkerResult("idle", message="no approved maintenance job is eligible")
            if selected not in initial.jobs:
                return WorkerResult("not-found", selected, "maintenance job does not exist")
            lifecycle = initial.jobs[selected]
            if lifecycle.status != "approved":
                return WorkerResult("approval-required", selected, "approve this exact job before running the worker")
            # Recheck under the ledger mutation lock immediately before the runner seam.
            with store.mutation():
                current = maintenance.read_state()
                current_lifecycle = current.jobs.get(selected)
                if not current.enabled:
                    return WorkerResult("disabled", selected, "maintenance was disabled before the worker could start")
                if current_lifecycle is None or current_lifecycle.status != "approved":
                    return WorkerResult("stale", selected, "job approval changed before worker start")
            # Task 7/8 supplies the runner. Never represent unavailable work as applied.
            return WorkerResult(
                "agent-unavailable", selected,
                "approved job is ready, but the maintenance agent runner is not available; job was not applied",
            )
    except LockBusyError:
        return WorkerResult("busy", job_id, "another maintenance worker currently holds the OS lease")


def _entrypoint() -> int:
    import argparse

    parser = argparse.ArgumentParser()
    parser.add_argument("--root", required=True)
    args = parser.parse_args()
    store = LedgerStore(Path(args.root), Path(args.root) / ".repo2skill")
    result = run_worker(store, foreground=True)
    sys.stderr.write(f"{result.status}: {result.message}\n")
    return 0 if result.status in {"idle", "disabled"} else 6


if __name__ == "__main__":
    raise SystemExit(_entrypoint())
