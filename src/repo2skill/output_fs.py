"""Narrow guarded filesystem operations for publishing managed output."""
from __future__ import annotations

import hashlib
import os
from pathlib import Path
import secrets
import stat
import unicodedata

from .safe_fs import GuardedDirectory, UnsafePathError, open_directory


class OutputConflictError(RuntimeError):
    """The live path no longer has the state expected by an export operation."""


def _parts(path: str) -> tuple[str, ...]:
    if (not isinstance(path, str) or not path or "\x00" in path or "\\" in path
            or path.startswith("/") or unicodedata.normalize("NFC", path) != path):
        raise ValueError("live path must be canonical NFC repository-relative syntax")
    parts = tuple(path.split("/"))
    if any(part in {"", ".", ".."} or ":" in part for part in parts):
        raise ValueError("live path must be canonical NFC repository-relative syntax")
    return parts


def _child(parent: GuardedDirectory, name: str) -> os.stat_result | None:
    folded = name.casefold()
    for existing in parent.names():
        if existing.casefold() == folded and existing != name:
            raise UnsafePathError(f"case-colliding output component: {name}")
    try:
        return parent.lstat(name)
    except FileNotFoundError:
        return None


class _ParentGuards:
    def __init__(self, root: Path, parts: tuple[str, ...], *, create: bool = False):
        self.guards: list[GuardedDirectory] = [open_directory(root)]
        self.leaf = parts[-1]
        try:
            for part in parts[:-1]:
                parent = self.guards[-1]
                info = _child(parent, part)
                if info is None:
                    if not create:
                        raise FileNotFoundError(part)
                    if parent._descriptor is not None:
                        os.mkdir(part, dir_fd=parent._descriptor)
                    else:
                        os.mkdir(parent.path / part)
                    info = _child(parent, part)
                    if info is None:
                        raise UnsafePathError("created output directory disappeared")
                if _redirect_or_wrong_directory(info):
                    raise UnsafePathError(f"output ancestor is redirected or not a directory: {part}")
                self.guards.append(parent.open_directory(part))
        except BaseException:
            self.close()
            raise

    @property
    def parent(self) -> GuardedDirectory:
        return self.guards[-1]

    def close(self) -> None:
        for guard in reversed(self.guards):
            guard.close()
        self.guards.clear()

    def __enter__(self) -> _ParentGuards:
        return self

    def __exit__(self, *_exc: object) -> None:
        self.close()


def _redirect_or_wrong_directory(info: os.stat_result) -> bool:
    return (stat.S_ISLNK(info.st_mode)
            or bool(getattr(info, "st_file_attributes", 0) & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400))
            or not stat.S_ISDIR(info.st_mode))


def _hash(payload: bytes) -> str:
    return "sha256:" + hashlib.sha256(payload).hexdigest()


def _read_leaf(parent: GuardedDirectory, name: str, *, limit: int) -> bytes | None:
    info = _child(parent, name)
    if info is None:
        return None
    if stat.S_ISLNK(info.st_mode) or bool(
        getattr(info, "st_file_attributes", 0) & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
    ) or not stat.S_ISREG(info.st_mode):
        raise UnsafePathError("output leaf is a redirect or not a regular file")
    with parent.open_file(name) as opened:
        payload = opened.read_bounded(limit)
        if len(payload) > limit:
            raise ValueError("live output exceeds the size limit")
        if not opened.stable_after_read():
            raise UnsafePathError("live output changed while being read")
        return payload


def read_live_file(root: Path, path: str, *, limit: int) -> bytes | None:
    if limit < 0:
        raise ValueError("limit must be nonnegative")
    parts = _parts(path)
    try:
        with _ParentGuards(Path(root), parts) as guards:
            return _read_leaf(guards.parent, guards.leaf, limit=limit)
    except FileNotFoundError:
        return None


def missing_parent_directories(root: Path, path: str) -> tuple[str, ...]:
    """Return missing parent directories after validating every existing ancestor."""
    parts = _parts(path)
    root_path = Path(root)
    missing: list[str] = []
    guards: list[GuardedDirectory] = [open_directory(root_path)]
    try:
        for index, part in enumerate(parts[:-1]):
            parent = guards[-1]
            if missing:
                missing.append("/".join(parts[:index + 1]))
                continue
            info = _child(parent, part)
            if info is None:
                missing.append("/".join(parts[:index + 1]))
                continue
            if _redirect_or_wrong_directory(info):
                raise UnsafePathError("output ancestor is redirected or not a directory")
            guards.append(parent.open_directory(part))
        return tuple(missing)
    finally:
        for guard in reversed(guards):
            guard.close()


def create_live_directory(root: Path, path: str) -> bool:
    parts = _parts(path)
    # A planned mkdir is a single component operation; parents must already exist.
    with _ParentGuards(Path(root), parts) as guards:
        existing = _child(guards.parent, guards.leaf)
        if existing is not None:
            if _redirect_or_wrong_directory(existing):
                raise OutputConflictError("planned output directory path is occupied by an unsafe object")
            return False
        if guards.parent._descriptor is not None:
            os.mkdir(guards.leaf, dir_fd=guards.parent._descriptor)
            os.fsync(guards.parent._descriptor)
        else:
            os.mkdir(guards.parent.path / guards.leaf)
        return True


def _check_expected(current: bytes | None, expected_old_hash: str | None) -> None:
    if (None if current is None else _hash(current)) != expected_old_hash:
        raise OutputConflictError("live output differs from its expected old hash")


def replace_live_file(root: Path, path: str, payload: bytes, *, expected_old_hash: str | None) -> None:
    if not isinstance(payload, bytes):
        raise TypeError("live output payload must be bytes")
    parts = _parts(path)
    with _ParentGuards(Path(root), parts) as guards:
        parent = guards.parent
        current = _read_leaf(parent, guards.leaf, limit=max(2 * 1024 * 1024, len(payload)))
        _check_expected(current, expected_old_hash)
        if parent._descriptor is not None:
            temporary = f".repo2skill-{secrets.token_hex(12)}.tmp"
            descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0), 0o600, dir_fd=parent._descriptor)
            try:
                with os.fdopen(descriptor, "wb", closefd=False) as stream:
                    stream.write(payload)
                    stream.flush()
                    os.fsync(descriptor)
                os.replace(temporary, guards.leaf, src_dir_fd=parent._descriptor, dst_dir_fd=parent._descriptor)
                os.fsync(parent._descriptor)
            finally:
                os.close(descriptor)
                try:
                    os.unlink(temporary, dir_fd=parent._descriptor)
                except FileNotFoundError:
                    pass
        else:
            temporary_path = parent.path / f".repo2skill-{secrets.token_hex(12)}.tmp"
            info = _child(parent, guards.leaf)
            if info is not None and (stat.S_ISLNK(info.st_mode) or bool(getattr(info, "st_file_attributes", 0) & 0x400)):
                raise UnsafePathError("output leaf became redirected")
            descriptor = os.open(temporary_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            try:
                with os.fdopen(descriptor, "wb") as stream:
                    stream.write(payload)
                    stream.flush()
                    os.fsync(stream.fileno())
                os.replace(temporary_path, parent.path / guards.leaf)
            finally:
                try:
                    temporary_path.unlink()
                except FileNotFoundError:
                    pass


def unlink_owned_file(root: Path, path: str, *, expected_old_hash: str) -> None:
    if expected_old_hash is None:
        raise ValueError("owned-file deletion requires an expected hash")
    parts = _parts(path)
    with _ParentGuards(Path(root), parts) as guards:
        parent = guards.parent
        current = _read_leaf(parent, guards.leaf, limit=2 * 1024 * 1024)
        _check_expected(current, expected_old_hash)
        if current is None:
            raise OutputConflictError("owned output is already absent")
        if parent._descriptor is not None:
            os.unlink(guards.leaf, dir_fd=parent._descriptor)
            os.fsync(parent._descriptor)
        else:
            info = _child(parent, guards.leaf)
            if info is None or stat.S_ISLNK(info.st_mode) or bool(getattr(info, "st_file_attributes", 0) & 0x400):
                raise UnsafePathError("output leaf became redirected")
            os.unlink(parent.path / guards.leaf)
