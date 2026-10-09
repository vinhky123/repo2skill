from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
import os
from pathlib import Path
import secrets
import stat
import tempfile
from typing import Any

from .ids import canonical_json_bytes
from .models import JsonValue, validate_json_value
from .safe_fs import GuardedDirectory, UnsafePathError, open_directory


def _validate_parts(parts: tuple[str, ...]) -> None:
    if any(not part or part in {".", ".."} or "/" in part or "\\" in part or "\x00" in part for part in parts):
        raise ValueError("store directory must contain normalized path components")


def _child(
    parent: GuardedDirectory, name: str, *, check_case: bool = True
) -> os.stat_result | None:
    if check_case:
        folded = name.casefold()
        for existing in parent.names():
            if existing.casefold() == folded and existing != name:
                raise ValueError(f"case-colliding store path component: {name}")
    try:
        return parent.lstat(name)
    except FileNotFoundError:
        return None


def _redirect_or_wrong_directory(info: os.stat_result) -> bool:
    return (
        stat.S_ISLNK(info.st_mode)
        or bool(
            getattr(info, "st_file_attributes", 0)
            & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
        )
        or not stat.S_ISDIR(info.st_mode)
    )


class _Parents:
    """Retain every store directory from root through a destination parent."""

    def __init__(self, root: Path, parts: tuple[str, ...], *, create: bool = False):
        _validate_parts(parts)
        absolute_root = Path(os.path.abspath(root))
        self.guards: list[GuardedDirectory] = [open_directory(Path(absolute_root.anchor))]
        try:
            root_parts = absolute_root.parts[1:]
            for index, part in enumerate(root_parts + parts):
                parent = self.guards[-1]
                info = _child(parent, part, check_case=index >= len(root_parts))
                if info is None:
                    if not create:
                        raise FileNotFoundError(part)
                    try:
                        if parent._descriptor is not None:
                            os.mkdir(part, dir_fd=parent._descriptor)
                        else:
                            os.mkdir(parent.path / part)
                    except FileExistsError:
                        # Revalidate a concurrent creator through the held parent.
                        info = _child(
                            parent, part, check_case=index >= len(root_parts)
                        )
                        if info is None:
                            raise
                    if parent._descriptor is not None:
                        os.fsync(parent._descriptor)
                    info = _child(
                        parent, part, check_case=index >= len(root_parts)
                    )
                    if info is None:
                        raise UnsafePathError("created store directory disappeared")
                if _redirect_or_wrong_directory(info):
                    raise UnsafePathError(f"store ancestor is redirected or not a directory: {part}")
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

    def __enter__(self) -> _Parents:
        return self

    def __exit__(self, *_exc: object) -> None:
        self.close()


@contextmanager
def guarded_parent(root: Path, directory_parts: tuple[str, ...], *, create: bool = False) -> Iterator[GuardedDirectory]:
    with _Parents(root, directory_parts, create=create) as parents:
        yield parents.parent


@contextmanager
def guarded_directory(root: Path, directory_parts: tuple[str, ...]) -> Iterator[GuardedDirectory]:
    with _Parents(root, directory_parts) as parents:
        yield parents.parent


def ensure_directories(root: Path, directory_parts: tuple[str, ...]) -> None:
    with _Parents(root, directory_parts, create=True):
        pass


def _leaf_info(parent: GuardedDirectory, leaf: str) -> os.stat_result | None:
    if not leaf or leaf in {".", ".."} or "/" in leaf or "\\" in leaf or "\x00" in leaf:
        raise ValueError("store leaf must be one normalized path component")
    info = _child(parent, leaf)
    if info is not None and (
        stat.S_ISLNK(info.st_mode)
        or bool(
            getattr(info, "st_file_attributes", 0)
            & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
        )
    ):
        raise UnsafePathError("store document must not be redirected")
    return info


def _new_temporary(parent: GuardedDirectory, leaf: str) -> tuple[int, str]:
    if parent._descriptor is None:
        descriptor, path = tempfile.mkstemp(
            prefix=f".{leaf}.", suffix=".tmp", dir=parent.path
        )
        return descriptor, Path(path).name
    for _attempt in range(100):
        name = f".{leaf}.{secrets.token_hex(8)}.tmp"
        try:
            descriptor = os.open(
                name,
                os.O_WRONLY
                | os.O_CREAT
                | os.O_EXCL
                | getattr(os, "O_NOFOLLOW", 0)
                | getattr(os, "O_CLOEXEC", 0),
                0o600,
                dir_fd=parent._descriptor,
            )
        except FileExistsError:
            continue
        return descriptor, name
    raise FileExistsError("could not allocate private store temporary")


def _sync_parent(parent: GuardedDirectory) -> None:
    if os.name == "nt":
        return
    if parent._descriptor is not None:
        os.fsync(parent._descriptor)
    else:
        descriptor = os.open(parent.path, os.O_RDONLY)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)


def _temp_path(parent: GuardedDirectory, name: str) -> Path:
    return parent.path / name


def _path_parent(path: Path) -> tuple[Path, tuple[str, ...], str]:
    absolute = Path(os.path.abspath(path))
    anchor = Path(absolute.anchor)
    return anchor, absolute.parent.relative_to(anchor).parts, absolute.name


def _publish(
    parent: GuardedDirectory,
    leaf: str,
    payload: bytes,
    *,
    replace: bool,
) -> None:
    current = _leaf_info(parent, leaf)
    if current is not None and not replace:
        raise FileExistsError(parent.path / leaf)
    descriptor, temporary_name = _new_temporary(parent, leaf)
    try:
        with os.fdopen(descriptor, "wb") as stream:
            descriptor = -1
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        if parent._descriptor is not None:
            if replace:
                os.replace(
                    temporary_name,
                    leaf,
                    src_dir_fd=parent._descriptor,
                    dst_dir_fd=parent._descriptor,
                )
            else:
                os.link(
                    temporary_name,
                    leaf,
                    src_dir_fd=parent._descriptor,
                    dst_dir_fd=parent._descriptor,
                    follow_symlinks=False,
                )
            os.fsync(parent._descriptor)
        else:
            temporary = _temp_path(parent, temporary_name)
            destination = parent.path / leaf
            if replace:
                os.replace(temporary, destination)
            else:
                os.link(temporary, destination, follow_symlinks=False)
            _sync_parent(parent)
    finally:
        if descriptor >= 0:
            os.close(descriptor)
        try:
            if parent._descriptor is not None:
                os.unlink(temporary_name, dir_fd=parent._descriptor)
            else:
                _temp_path(parent, temporary_name).unlink()
        except FileNotFoundError:
            pass


def write_immutable(root: Path, directory_parts: tuple[str, ...], leaf: str, payload: bytes, *, create: bool = False) -> None:
    with guarded_parent(root, directory_parts, create=create) as parent:
        _publish(parent, leaf, payload, replace=False)


def write_immutable_path(path: Path, payload: bytes) -> None:
    root, parts, leaf = _path_parent(path)
    write_immutable(root, parts, leaf, payload)


def replace_json(root: Path, directory_parts: tuple[str, ...], leaf: str, document: JsonValue, *, create: bool = False) -> None:
    validate_json_value(document)
    payload = canonical_json_bytes(document) + b"\n"
    with guarded_parent(root, directory_parts, create=create) as parent:
        _publish(parent, leaf, payload, replace=True)


def replace_json_path(path: Path, document: JsonValue) -> None:
    root, parts, leaf = _path_parent(path)
    replace_json(root, parts, leaf, document)


def read_file_path(path: Path) -> bytes:
    """Read a file through retained guards from its filesystem anchor to its parent."""
    root, parts, leaf = _path_parent(path)
    with guarded_parent(root, parts) as parent:
        with parent.open_file(leaf) as opened:
            size = opened.stat_before.st_size
            payload = opened.read_bounded(size)
            if len(payload) != size or not opened.stable_after_read():
                raise UnsafePathError("guarded file changed while being read")
            return payload


@contextmanager
def lock_file(root: Path, directory_parts: tuple[str, ...]) -> Iterator[Any]:
    with guarded_parent(root, directory_parts) as parent:
        info = _leaf_info(parent, ".lock")
        if info is not None and not stat.S_ISREG(info.st_mode):
            raise UnsafePathError("lock file must be a regular file")
        if parent._descriptor is not None:
            descriptor = os.open(
                ".lock",
                os.O_RDWR
                | os.O_CREAT
                | getattr(os, "O_NOFOLLOW", 0)
                | getattr(os, "O_CLOEXEC", 0),
                0o600,
                dir_fd=parent._descriptor,
            )
            stream = os.fdopen(descriptor, "r+b")
        else:
            stream = (parent.path / ".lock").open("a+b")
        try:
            yield stream
        finally:
            stream.close()
