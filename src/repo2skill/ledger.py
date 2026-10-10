from __future__ import annotations

from collections.abc import Mapping
from contextlib import contextmanager
import dataclasses
import json
import os
from pathlib import Path, PurePosixPath, PureWindowsPath
import re
import stat
import types
from typing import Any, Iterator, Literal, TypeVar, Union, get_args, get_origin, get_type_hints

from .config import PathBoundaryError, resolve_under
from .ids import CanonicalIdContext, canonical_json_bytes
from .models import (
    ExportJournal,
    JsonValue,
    LedgerStoreSnapshot,
    ManifestSet,
    ProjectDocument,
    ProjectState,
    RunLedger,
    TargetManifest,
    validate_json_value,
)
from .safe_fs import open_directory
from . import store_io


T = TypeVar("T")


class LockBusyError(RuntimeError):
    """Another process owns the project mutation lock."""


class ExportRecoveryRequired(RuntimeError):
    """An unresolved export journal must be recovered before mutation."""

    def __init__(self, journals: tuple[ExportJournal, ...]):
        self.journals = journals
        super().__init__("an incomplete export journal blocks mutation; resume it first")


def _is_redirect(path: Path) -> bool:
    path_stat = path.lstat()
    reparse_flag = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
    return path.is_symlink() or bool(getattr(path_stat, "st_file_attributes", 0) & reparse_flag)


def _reject_redirected_ancestors(path: Path) -> None:
    absolute = Path(os.path.abspath(path))
    current = Path(absolute.anchor)
    for part in absolute.parts[1:]:
        current = current / part
        if not current.exists() and not current.is_symlink():
            break
        if _is_redirect(current):
            raise ValueError(f"symlink or reparse path component is not allowed: {current}")


def _reject_case_collision(parent: Path, name: str) -> None:
    if not parent.is_dir():
        return
    folded = name.casefold()
    for entry in os.listdir(parent):
        if entry.casefold() == folded and entry != name:
            raise ValueError(f"case-colliding store path component: {name}")


def write_immutable_document(path: Path, document: JsonValue) -> None:
    """Durably publish a JSON document without replacing an existing path."""
    validate_json_value(document)
    payload = canonical_json_bytes(document) + b"\n"
    store_io.write_immutable_path(path, payload)


def write_immutable_bytes(path: Path, payload: bytes) -> None:
    """Durably publish exact immutable bytes without replacing an existing path."""
    if not isinstance(payload, bytes):
        raise TypeError("immutable payload must be bytes")
    store_io.write_immutable_path(path, payload)


def _replace_json(path: Path, document: JsonValue) -> None:
    validate_json_value(document)
    store_io.replace_json_path(path, document)


def _encode(value: object) -> JsonValue:
    if dataclasses.is_dataclass(value) and not isinstance(value, type):
        return {field.name: _encode(getattr(value, field.name)) for field in dataclasses.fields(value)}
    if isinstance(value, tuple):
        return [_encode(item) for item in value]
    if isinstance(value, list):
        return [_encode(item) for item in value]
    if isinstance(value, Mapping):
        result: dict[str, JsonValue] = {}
        for key, item in value.items():
            if not isinstance(key, str):
                raise ValueError("persisted mapping keys must be strings")
            result[key] = _encode(item)
        return result
    validate_json_value(value)
    return value  # type: ignore[return-value]


def encode_record(value: object) -> JsonValue:
    """Encode a persisted dataclass or JSON value without writing it."""
    return _encode(value)


def replace_state(path: Path, state: ProjectState) -> None:
    """Atomically replace the state pointer; caller owns the project lock."""
    _replace_json(path, _encode(state))


@contextmanager
def acquire_project_lock(state_dir: Path) -> Iterator[None]:
    """Acquire the persistent lock file without waiting or unlinking it."""
    with _acquire_project_lock_at(state_dir, ()):
        yield


@contextmanager
def _acquire_project_lock_at(root: Path, state_parts: tuple[str, ...]) -> Iterator[None]:
    _reject_redirected_ancestors(root.joinpath(*state_parts))
    with store_io.lock_file(root, state_parts) as stream:
        stream.seek(0, os.SEEK_END)
        if stream.tell() == 0:
            stream.write(b"\0")
            stream.flush()
            os.fsync(stream.fileno())
        stream.seek(0)
        try:
            if os.name == "nt":
                import msvcrt
                msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as error:
            raise LockBusyError("project state is busy") from error
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


def _load_json(path: Path) -> dict[str, JsonValue]:
    payload = store_io.read_file_path(path)
    value = json.loads(
        payload.decode("utf-8"),
        parse_constant=lambda value: (_ for _ in ()).throw(
            ValueError(f"non-finite JSON number: {value}")
        ),
    )
    validate_json_value(value)
    if not isinstance(value, dict):
        raise ValueError("persisted root document must be a JSON object")
    return value


def _decode_value(value: object, annotation: object, field: str) -> object:
    if annotation == JsonValue:
        validate_json_value(value, field=field)
        return value
    origin = get_origin(annotation)
    args = get_args(annotation)
    if origin is Literal:
        if value not in args:
            raise ValueError(f"invalid {field}: {value!r}")
        return value
    if origin in {tuple}:
        if not isinstance(value, list):
            raise ValueError(f"{field} must be an array")
        item_type = args[0]
        return tuple(_decode_value(item, item_type, field) for item in value)
    if origin in {dict, Mapping} or (isinstance(origin, type) and issubclass(origin, Mapping)):
        if not isinstance(value, dict):
            raise ValueError(f"{field} must be an object")
        key_type, item_type = args
        if key_type is not str:
            return value
        return {key: _decode_value(item, item_type, field) for key, item in value.items()}
    if origin in {types.UnionType, Union}:
        if value is None and type(None) in args:
            return None
        for member in args:
            if member is type(None):
                continue
            try:
                return _decode_value(value, member, field)
            except (TypeError, ValueError):
                pass
        raise ValueError(f"{field} does not match any allowed union member")
    if isinstance(annotation, type) and dataclasses.is_dataclass(annotation):
        if not isinstance(value, dict):
            raise ValueError(f"{field} must be an object")
        return _decode_dataclass(value, annotation)
    if annotation is bool:
        if type(value) is not bool: raise ValueError(f"{field} must be a boolean")
    elif annotation is int:
        if type(value) is not int: raise ValueError(f"{field} must be an integer")
    elif annotation is float:
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise ValueError(f"{field} must be a number")
    elif annotation is str:
        if not isinstance(value, str): raise ValueError(f"{field} must be a string")
    return value


def _decode_dataclass(raw: dict[str, JsonValue], record_type: type[T]) -> T:
    hints = get_type_hints(record_type)
    values: dict[str, object] = {}
    for field in dataclasses.fields(record_type):
        if field.name not in raw:
            if field.default is not dataclasses.MISSING:
                values[field.name] = field.default
                continue
            if field.default_factory is not dataclasses.MISSING:
                values[field.name] = field.default_factory()
                continue
            raise ValueError(f"missing required field: {record_type.__name__}.{field.name}")
        values[field.name] = _decode_value(raw[field.name], hints[field.name], f"{record_type.__name__}.{field.name}")
    return record_type(**values)


def decode_project_state(raw: dict[str, JsonValue]) -> ProjectState:
    schema = raw.get("schemaVersion")
    if not isinstance(schema, str) or schema.split(".", 1)[0] != "1":
        raise ValueError(f"unknown schema major: {schema!r}")
    return _decode_dataclass(raw, ProjectState)


def decode_root_document(raw: dict[str, JsonValue], record_type: type[T]) -> T:
    schema = raw.get("schemaVersion")
    if not isinstance(schema, str) or schema.split(".", 1)[0] != "1":
        raise ValueError(f"unknown schema major: {schema!r}")
    return _decode_dataclass(raw, record_type)


def _merge_additive(
    old: JsonValue, new: JsonValue, *, field_name: str | None = None
) -> JsonValue:
    # This is a complete claimId -> accepted revision projection, so omitted
    # entries express revocation (for example when an accepted claim is contested).
    if isinstance(old, dict) and isinstance(new, dict) and field_name == "acceptedClaimRevisions":
        return new
    if isinstance(old, dict) and isinstance(new, dict):
        merged = dict(old)
        for key, value in new.items():
            merged[key] = (
                _merge_additive(old[key], value, field_name=key)
                if key in old else value
            )
        return merged
    if isinstance(old, list) and isinstance(new, list) and field_name in {"reviewEvents", "deferredEvents"}:
        old_by_id = {
            item["eventId"]: item
            for item in old
            if isinstance(item, dict) and isinstance(item.get("eventId"), str)
        }
        result: list[JsonValue] = []
        for item in new:
            event_id = item.get("eventId") if isinstance(item, dict) else None
            previous = old_by_id.get(event_id) if isinstance(event_id, str) else None
            result.append(
                _merge_additive(previous, item) if previous is not None else item
            )
        return result
    return new


class LedgerStore:
    """Explicit project-local persistence and mutation-lock context."""

    def __init__(self, root: Path, state_dir: Path) -> None:
        self.root = Path(os.path.abspath(root))
        if not self.root.is_dir() or _is_redirect(self.root):
            raise ValueError("root must be an existing real directory")
        candidate = state_dir if state_dir.is_absolute() else self.root / state_dir
        self.state_dir = Path(os.path.abspath(candidate))
        try:
            relative = self.state_dir.relative_to(self.root)
        except ValueError as error:
            raise ValueError("state_dir must be confined under root") from error
        if not relative.parts:
            raise ValueError("state_dir must be below root")
        try:
            resolve_under(self.root, relative)
        except PathBoundaryError as error:
            raise ValueError(str(error)) from error
        self._mutation_active = False
        self._state_loaded = False
        self._raw_state: dict[str, JsonValue] | None = None
        self.id_context = CanonicalIdContext()

    def _ensure_state_dir(self) -> None:
        relative = self.state_dir.relative_to(self.root)
        _reject_redirected_ancestors(self.root)
        store_io.ensure_directories(self.root, relative.parts)

    @contextmanager
    def mutation(self) -> Iterator[None]:
        if self._mutation_active:
            raise RuntimeError("nested mutation scopes are not allowed")
        self._ensure_state_dir()
        with _acquire_project_lock_at(
            self.root, self.state_dir.relative_to(self.root).parts
        ):
            self._mutation_active = True
            self._state_loaded = False
            self._raw_state = None
            try:
                yield
            finally:
                self._mutation_active = False
                self._state_loaded = False
                self._raw_state = None

    def read_state(self) -> ProjectState | None:
        path = self._store_path("state.json", create_parents=False)
        if not path.exists():
            if self._mutation_active:
                self._state_loaded = True
                self._raw_state = None
            return None
        raw = _load_json(path)
        state = decode_project_state(raw)
        if self._mutation_active:
            self._state_loaded = True
            self._raw_state = raw
        return state

    def read_project(self) -> ProjectDocument | None:
        path = self._store_path("project.json", create_parents=False)
        if not path.exists():
            return None
        return decode_root_document(_load_json(path), ProjectDocument)

    def read_run(self, run_id: str) -> RunLedger | None:
        path = self._store_path(f"runs/{run_id}.json", create_parents=False)
        if not path.exists():
            return None
        run = decode_root_document(_load_json(path), RunLedger)
        if run.runId != run_id:
            raise ValueError("run document ID does not match its path")
        return run

    def list_run_ids(self) -> tuple[str, ...]:
        """Enumerate immutable runs without creating state or acquiring a lock."""
        state_parts = self.state_dir.relative_to(self.root).parts
        try:
            with store_io.guarded_directory(self.root, state_parts) as state_guard:
                with state_guard.open_directory("runs") as runs_guard:
                    names = runs_guard.names()
                    run_ids: list[str] = []
                    for name in sorted(names):
                        if not name.endswith(".json"):
                            raise ValueError("unexpected file in run ledger directory")
                        run_id = name[:-5]
                        if not re.fullmatch(r"[a-z][a-z0-9-]*_[0-9a-f]{24}", run_id):
                            raise ValueError("run ledger path contains an invalid run ID")
                        try:
                            with runs_guard.open_file(name) as opened:
                                size = opened.stat_before.st_size
                                payload = opened.read_bounded(size)
                                if len(payload) != size or not opened.stable_after_read():
                                    raise ValueError(
                                        f"run ledger changed while being read: {name}"
                                    )
                            raw = json.loads(
                                payload.decode("utf-8"),
                                parse_constant=lambda value: (_ for _ in ()).throw(
                                    ValueError(f"non-finite JSON number: {value}")
                                ),
                            )
                        except FileNotFoundError as error:
                            raise ValueError(
                                f"run ledger disappeared while enumerating: {name}"
                            ) from error
                        validate_json_value(raw)
                        run = decode_root_document(raw, RunLedger)
                        if run.runId != run_id:
                            raise ValueError("run document ID does not match its path")
                        run_ids.append(run_id)
                    return tuple(run_ids)
        except FileNotFoundError:
            return ()

    def read_manifest_set(self, relative_path: str) -> ManifestSet | None:
        path = self._store_path(relative_path, create_parents=False)
        if not path.exists():
            return None
        return decode_root_document(_load_json(path), ManifestSet)

    def read_target_manifest(self, relative_path: str) -> TargetManifest | None:
        path = self._store_path(relative_path, create_parents=False)
        if not path.exists():
            return None
        return decode_root_document(_load_json(path), TargetManifest)

    def read_export_journal(self, relative_path: str) -> ExportJournal | None:
        path = self._store_path(relative_path, create_parents=False)
        if not path.exists():
            return None
        return decode_root_document(_load_json(path), ExportJournal)

    def list_export_journals(self) -> tuple[ExportJournal, ...]:
        """Enumerate journals without creating the state directory or lock."""
        state_parts = self.state_dir.relative_to(self.root).parts
        try:
            with store_io.guarded_directory(self.root, state_parts) as state_guard:
                exports_guard = state_guard.open_directory("exports")
                with exports_guard:
                    names = exports_guard.names()
        except FileNotFoundError:
            return ()
        journals: list[ExportJournal] = []
        for name in sorted(names):
            if not name.endswith(".json"):
                raise ValueError("unexpected file in export journal directory")
            relative = f"exports/{name}"
            journal = self.read_export_journal(relative)
            if journal is None:
                raise ValueError(f"export journal disappeared while enumerating: {name}")
            if name != f"{journal.exportId}.json":
                raise ValueError("export journal ID does not match its path")
            journals.append(journal)
        return tuple(journals)

    def write_export_journal(self, journal: ExportJournal) -> None:
        """Atomically create or update a journal under the active mutation lock."""
        self._require_mutation()
        self._store_path(f"exports/{journal.exportId}.json", create_parents=False)
        state_parts = self.state_dir.relative_to(self.root).parts
        store_io.replace_json(
            self.root,
            state_parts + ("exports",),
            f"{journal.exportId}.json",
            encode_record(journal),
            create=True,
        )

    def require_no_unresolved_export(self, *, resume_export_id: str | None = None) -> None:
        """Require all prior exports committed, allowing one explicit recovery."""
        self._require_mutation()
        unresolved = tuple(
            journal for journal in self.list_export_journals()
            if journal.status != "committed" and journal.exportId != resume_export_id
        )
        if unresolved:
            raise ExportRecoveryRequired(unresolved)

    def snapshot(self) -> LedgerStoreSnapshot:
        """Read a coherent active view without creating or rewriting state."""
        for _attempt in range(3):
            first = self.read_state()
            project = self.read_project()
            if first is None or project is None:
                raise FileNotFoundError("project and state documents are required")
            if project.projectId != first.projectId:
                raise ValueError("project and state project IDs do not match")
            runs: dict[str, RunLedger] = {}
            if first.currentRunId is not None:
                run = self.read_run(first.currentRunId)
                if run is None:
                    raise ValueError("active run document is missing")
                runs[first.currentRunId] = run
            manifest_sets: dict[str, ManifestSet] = {}
            if first.activeExportId is not None and first.activeManifestSetId is not None:
                manifest = self.read_manifest_set(
                    f"manifests/{first.activeExportId}/{first.activeManifestSetId}.json"
                )
                if manifest is None:
                    raise ValueError("active manifest set document is missing")
                if (
                    manifest.exportId != first.activeExportId
                    or manifest.manifestSetId != first.activeManifestSetId
                ):
                    raise ValueError("active manifest set IDs do not match the state pointer")
                manifest_sets[first.activeExportId] = manifest
            stage_locations: dict[tuple[str, int], str] = {}
            if first.currentRunId is not None and first.activeStageRevision is not None:
                prefix = self.state_dir.relative_to(self.root).as_posix()
                location = f"{prefix}/staging/{first.currentRunId}/{first.activeStageRevision}"
                stage_locations[(first.currentRunId, first.activeStageRevision)] = location
            second = self.read_state()
            if second is not None and canonical_json_bytes(_encode(first)) == canonical_json_bytes(_encode(second)):
                return LedgerStoreSnapshot(project, second, runs, manifest_sets, stage_locations)
        raise RuntimeError("state changed repeatedly while reading snapshot")

    def replace_state(self, state: ProjectState) -> None:
        self._require_mutation()
        if not self._state_loaded:
            raise RuntimeError("authoritative state must be read inside the mutation before commit")
        encoded = _encode(state)
        if self._raw_state is not None:
            encoded = _merge_additive(self._raw_state, encoded)
        store_io.replace_json(
            self.root,
            self.state_dir.relative_to(self.root).parts,
            "state.json",
            encoded,
        )
        self._raw_state = encoded if isinstance(encoded, dict) else None

    def write_immutable(self, relative_path: str, document: JsonValue) -> None:
        self._require_mutation()
        validate_json_value(document)
        path = self._store_path(relative_path, create_parents=False)
        parts = path.relative_to(self.root).parts
        store_io.write_immutable(
            self.root,
            parts[:-1],
            parts[-1],
            canonical_json_bytes(document) + b"\n",
            create=True,
        )

    def write_immutable_bytes(self, relative_path: str, payload: bytes) -> None:
        """Publish exact bytes below state_dir using an immutable temp-and-link write."""
        self._require_mutation()
        path = self._store_path(relative_path, create_parents=False)
        parts = path.relative_to(self.root).parts
        store_io.write_immutable(self.root, parts[:-1], parts[-1], payload, create=True)

    def read_immutable_bytes(self, relative_path: str, *, max_bytes: int | None = None) -> bytes | None:
        """Read an immutable byte artifact through guarded handles with an optional open-handle cap."""
        if max_bytes is not None and (isinstance(max_bytes, bool) or not isinstance(max_bytes, int) or max_bytes < 0):
            raise ValueError("max_bytes must be a nonnegative integer or None")
        path = self._store_path(relative_path, create_parents=False)
        if not path.exists():
            if path.is_symlink():
                raise ValueError(f"immutable artifact must not be redirected: {path}")
            return None
        relative = path.relative_to(self.state_dir)
        with open_directory(self.state_dir) as guard:
            for component in relative.parts[:-1]:
                guard_next = guard.open_directory(component)
                guard.close()
                guard = guard_next
            with guard.open_file(relative.parts[-1]) as opened:
                size = opened.stat_before.st_size
                if max_bytes is not None and size > max_bytes:
                    raise ValueError(f"immutable artifact exceeds byte limit: {relative_path}")
                payload = opened.read_bounded(size)
                if len(payload) != size or not opened.stable_after_read():
                    raise ValueError(f"immutable artifact changed while reading: {relative_path}")
                return payload

    def read_maintenance_state_document(self) -> dict[str, JsonValue] | None:
        """Read the separate maintenance pointer without creating store paths."""
        path = self._store_path("maintenance/state.json", create_parents=False)
        if not path.exists():
            return None
        return _load_json(path)

    def replace_maintenance_state_document(self, document: dict[str, JsonValue]) -> None:
        """Atomically replace the typed maintenance pointer under the mutation lock."""
        self._require_mutation()
        validate_json_value(document)
        path = self._store_path("maintenance/state.json", create_parents=False)
        try:
            old = _load_json(path)
        except FileNotFoundError:
            old = None
        encoded: JsonValue = document
        if old is not None:
            encoded = _merge_additive(old, document)
        if not isinstance(encoded, dict):
            raise ValueError("maintenance state document must be an object")
        parts = self.state_dir.relative_to(self.root).parts
        store_io.replace_json(
            self.root, parts + ("maintenance",), "state.json", encoded, create=True
        )

    def write_maintenance_job_bytes(self, job_id: str, payload: bytes) -> None:
        """Create an immutable maintenance job, accepting only exact retries."""
        self._require_mutation()
        if not isinstance(job_id, str) or not re.fullmatch(
            r"[a-z][a-z0-9-]*_[0-9a-f]{24}", job_id
        ):
            raise ValueError("job ID must be a canonical lowercase ID")
        relative_path = f"maintenance/jobs/{job_id}.json"
        existing = self.read_immutable_bytes(relative_path)
        if existing is not None:
            if existing == payload:
                return
            raise FileExistsError(relative_path)
        self.write_immutable_bytes(relative_path, payload)

    def immutable_path_exists(self, relative_path: str) -> bool:
        """Check whether an immutable artifact path already exists below state_dir."""
        return self._store_path(relative_path, create_parents=False).exists()

    def _store_path(self, relative_path: str, *, create_parents: bool) -> Path:
        if not isinstance(relative_path, str) or not relative_path or "\x00" in relative_path:
            raise ValueError("store path must be normalized and relative")
        posix = PurePosixPath(relative_path)
        windows = PureWindowsPath(relative_path)
        if (
            posix.is_absolute()
            or windows.is_absolute()
            or bool(windows.drive)
            or "\\" in relative_path
            or posix.as_posix() != relative_path
            or any(part in {"", ".", ".."} for part in posix.parts)
        ):
            raise ValueError("store path must be normalized and relative")
        candidate = Path(*posix.parts)
        _reject_redirected_ancestors(self.state_dir)
        if not self.state_dir.exists():
            if create_parents:
                raise RuntimeError("state directory must be created by mutation scope")
            return self.state_dir / candidate
        destination = resolve_under(self.state_dir, candidate)
        current = self.state_dir
        for index, part in enumerate(candidate.parts):
            _reject_case_collision(current, part)
            current = current / part
            if index < len(candidate.parts) - 1:
                if current.exists() or current.is_symlink():
                    if not current.is_dir() or _is_redirect(current):
                        raise ValueError(f"store path is redirected: {current}")
        return destination

    def _require_mutation(self) -> None:
        if not self._mutation_active:
            raise RuntimeError("stateful write requires an active mutation scope")
