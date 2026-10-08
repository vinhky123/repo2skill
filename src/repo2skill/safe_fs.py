from __future__ import annotations

import os
from pathlib import Path
import stat
import unicodedata


class UnsafePathError(OSError):
    """A filesystem object changed identity or is a redirect/boundary."""

    def __init__(self, message: str, *, handle_opened: bool = False):
        self.handle_opened = handle_opened
        super().__init__(message)


def _validate_name(name: str) -> str:
    if (
        not isinstance(name, str)
        or not name
        or name in {".", ".."}
        or "\x00" in name
        or "/" in name
        or "\\" in name
    ):
        raise ValueError("child name must be one normalized path component")
    return name


def _same_identity(first: os.stat_result, second: os.stat_result) -> bool:
    return (
        first.st_dev == second.st_dev
        and first.st_ino == second.st_ino
        and stat.S_IFMT(first.st_mode) == stat.S_IFMT(second.st_mode)
    )


def _same_stable_file(first: os.stat_result, second: os.stat_result) -> bool:
    return _same_identity(first, second) and (
        first.st_size == second.st_size
        and first.st_mtime_ns == second.st_mtime_ns
    )


class GuardedFile:
    """An already-open regular file that cannot be replaced through its path."""

    def __init__(self, descriptor: int, stat_before: os.stat_result):
        self._descriptor = descriptor
        self.stat_before = stat_before
        self._closed = False

    def __enter__(self) -> GuardedFile:
        return self

    def __exit__(self, *_exc: object) -> None:
        self.close()

    def close(self) -> None:
        if not self._closed:
            os.close(self._descriptor)
            self._closed = True

    def read_bounded(self, limit: int) -> bytes:
        if self._closed or limit < 0:
            raise ValueError("open file and nonnegative byte limit are required")
        chunks: list[bytes] = []
        remaining = limit + 1
        while remaining:
            chunk = os.read(self._descriptor, min(64 * 1024, remaining))
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
        return b"".join(chunks)

    def stable_after_read(self) -> bool:
        return not self._closed and _same_stable_file(
            self.stat_before, os.fstat(self._descriptor)
        )


class GuardedDirectory:
    """A retained directory guard used for component-relative safe access."""

    def __init__(
        self,
        path: Path,
        stat_result: os.stat_result,
        *,
        descriptor: int | None = None,
        windows_handle: int | None = None,
        final_path: str | None = None,
    ):
        self.path = path
        self.stat = stat_result
        self._descriptor = descriptor
        self._windows_handle = windows_handle
        self._final_path = final_path
        self._closed = False

    def __enter__(self) -> GuardedDirectory:
        return self

    def __exit__(self, *_exc: object) -> None:
        self.close()

    def close(self) -> None:
        if self._closed:
            return
        if self._descriptor is not None:
            os.close(self._descriptor)
        if self._windows_handle is not None:
            _close_windows_handle(self._windows_handle)
        self._closed = True

    def names(self) -> tuple[str, ...]:
        self._require_open()
        scan_target: int | Path = (
            self._descriptor if self._descriptor is not None else self.path
        )
        with os.scandir(scan_target) as iterator:
            return tuple(entry.name for entry in iterator)

    def lstat(self, name: str) -> os.stat_result:
        self._require_open()
        child = _validate_name(name)
        if self._descriptor is not None:
            return os.stat(child, dir_fd=self._descriptor, follow_symlinks=False)
        return os.stat(self.path / child, follow_symlinks=False)

    def open_directory(self, name: str) -> GuardedDirectory:
        self._require_open()
        child = _validate_name(name)
        before = self.lstat(child)
        if _is_redirect(before) or not stat.S_ISDIR(before.st_mode):
            raise UnsafePathError(f"directory child is a redirect or wrong type: {child}")
        if self._descriptor is not None:
            flags = (
                os.O_RDONLY
                | getattr(os, "O_DIRECTORY", 0)
                | getattr(os, "O_NOFOLLOW", 0)
                | getattr(os, "O_CLOEXEC", 0)
            )
            try:
                descriptor = os.open(child, flags, dir_fd=self._descriptor)
            except OSError as error:
                raise UnsafePathError(f"directory child could not be guarded: {child}") from error
            after = os.fstat(descriptor)
            if not stat.S_ISDIR(after.st_mode) or not _same_identity(before, after):
                os.close(descriptor)
                raise UnsafePathError(f"directory child changed before guard: {child}")
            return GuardedDirectory(self.path / child, after, descriptor=descriptor)
        return _open_windows_directory(
            self.path / child,
            before=before,
            expected_parent=self._final_path,
        )

    def open_file(self, name: str) -> GuardedFile:
        self._require_open()
        child = _validate_name(name)
        before = self.lstat(child)
        if _is_redirect(before) or not stat.S_ISREG(before.st_mode):
            raise UnsafePathError(f"file child is a redirect or wrong type: {child}")
        if self._descriptor is not None:
            flags = (
                os.O_RDONLY
                | getattr(os, "O_BINARY", 0)
                | getattr(os, "O_NOFOLLOW", 0)
                | getattr(os, "O_CLOEXEC", 0)
            )
            try:
                descriptor = os.open(child, flags, dir_fd=self._descriptor)
            except OSError as error:
                raise UnsafePathError(f"file child could not be guarded: {child}") from error
            try:
                after = os.fstat(descriptor)
            except OSError as error:
                os.close(descriptor)
                raise UnsafePathError(
                    f"file child metadata unavailable after open: {child}",
                    handle_opened=True,
                ) from error
            if not stat.S_ISREG(after.st_mode) or not _same_identity(before, after):
                os.close(descriptor)
                raise UnsafePathError(
                    f"file child changed before guard: {child}", handle_opened=True
                )
            return GuardedFile(descriptor, after)
        return _open_windows_file(
            self.path / child,
            before=before,
            expected_parent=self._final_path,
        )

    def readlink(self, name: str) -> str:
        self._require_open()
        child = _validate_name(name)
        if self._descriptor is not None:
            return os.readlink(child, dir_fd=self._descriptor)
        return os.readlink(self.path / child)

    def _require_open(self) -> None:
        if self._closed:
            raise ValueError("directory guard is closed")


def open_directory(path: Path) -> GuardedDirectory:
    """Open and retain a non-redirected directory guard until explicitly closed."""
    absolute = Path(os.path.abspath(path))
    before = os.stat(absolute, follow_symlinks=False)
    if _is_redirect(before) or not stat.S_ISDIR(before.st_mode):
        raise UnsafePathError("directory root is a redirect or wrong type")
    if os.name == "nt":
        return _open_windows_directory(absolute, before=before, expected_parent=None)
    flags = (
        os.O_RDONLY
        | getattr(os, "O_DIRECTORY", 0)
        | getattr(os, "O_NOFOLLOW", 0)
        | getattr(os, "O_CLOEXEC", 0)
    )
    try:
        descriptor = os.open(absolute, flags)
    except OSError as error:
        raise UnsafePathError("directory root could not be guarded") from error
    after = os.fstat(descriptor)
    if not stat.S_ISDIR(after.st_mode) or not _same_identity(before, after):
        os.close(descriptor)
        raise UnsafePathError("directory root changed before guard")
    return GuardedDirectory(absolute, after, descriptor=descriptor)


def _is_redirect(value: os.stat_result) -> bool:
    attributes = getattr(value, "st_file_attributes", 0)
    reparse = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
    return stat.S_ISLNK(value.st_mode) or bool(attributes & reparse)


if os.name == "nt":
    import ctypes
    from ctypes import wintypes
    import msvcrt

    _GENERIC_READ = 0x80000000
    _FILE_SHARE_READ = 0x00000001
    _FILE_SHARE_WRITE = 0x00000002
    _OPEN_EXISTING = 3
    _FILE_ATTRIBUTE_DIRECTORY = 0x00000010
    _FILE_ATTRIBUTE_REPARSE_POINT = 0x00000400
    _FILE_FLAG_OPEN_REPARSE_POINT = 0x00200000
    _FILE_FLAG_BACKUP_SEMANTICS = 0x02000000
    _INVALID_HANDLE_VALUE = ctypes.c_void_p(-1).value

    class _BY_HANDLE_FILE_INFORMATION(ctypes.Structure):
        _fields_ = [
            ("dwFileAttributes", wintypes.DWORD),
            ("ftCreationTime", wintypes.FILETIME),
            ("ftLastAccessTime", wintypes.FILETIME),
            ("ftLastWriteTime", wintypes.FILETIME),
            ("dwVolumeSerialNumber", wintypes.DWORD),
            ("nFileSizeHigh", wintypes.DWORD),
            ("nFileSizeLow", wintypes.DWORD),
            ("nNumberOfLinks", wintypes.DWORD),
            ("nFileIndexHigh", wintypes.DWORD),
            ("nFileIndexLow", wintypes.DWORD),
        ]

    _kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    _create_file = _kernel32.CreateFileW
    _create_file.argtypes = [
        wintypes.LPCWSTR,
        wintypes.DWORD,
        wintypes.DWORD,
        wintypes.LPVOID,
        wintypes.DWORD,
        wintypes.DWORD,
        wintypes.HANDLE,
    ]
    _create_file.restype = wintypes.HANDLE
    _get_file_info = _kernel32.GetFileInformationByHandle
    _get_file_info.argtypes = [wintypes.HANDLE, ctypes.POINTER(_BY_HANDLE_FILE_INFORMATION)]
    _get_file_info.restype = wintypes.BOOL
    _get_final_path = _kernel32.GetFinalPathNameByHandleW
    _get_final_path.argtypes = [wintypes.HANDLE, wintypes.LPWSTR, wintypes.DWORD, wintypes.DWORD]
    _get_final_path.restype = wintypes.DWORD
    _close_handle = _kernel32.CloseHandle
    _close_handle.argtypes = [wintypes.HANDLE]
    _close_handle.restype = wintypes.BOOL


def _open_windows_directory(
    path: Path,
    *,
    before: os.stat_result,
    expected_parent: str | None,
) -> GuardedDirectory:
    handle, final_path, attributes = _open_windows_guard(
        path, _GENERIC_READ, expected_parent
    )
    if not attributes & _FILE_ATTRIBUTE_DIRECTORY:
        _close_windows_handle(handle)
        raise UnsafePathError("guarded directory has the wrong type")
    after = os.stat(path, follow_symlinks=False)
    if not _same_identity(before, after):
        _close_windows_handle(handle)
        raise UnsafePathError("directory changed before guard", handle_opened=True)
    return GuardedDirectory(
        path,
        after,
        windows_handle=handle,
        final_path=final_path,
    )


def _open_windows_file(
    path: Path,
    *,
    before: os.stat_result,
    expected_parent: str | None,
) -> GuardedFile:
    handle, _final_path, attributes = _open_windows_guard(
        path, _GENERIC_READ, expected_parent
    )
    if attributes & _FILE_ATTRIBUTE_DIRECTORY:
        _close_windows_handle(handle)
        raise UnsafePathError("guarded file has the wrong type", handle_opened=True)
    try:
        after = os.stat(path, follow_symlinks=False)
    except OSError as error:
        _close_windows_handle(handle)
        raise UnsafePathError("file path changed after handle open", handle_opened=True) from error
    if not _same_identity(before, after):
        _close_windows_handle(handle)
        raise UnsafePathError("file changed before guard", handle_opened=True)
    try:
        descriptor = msvcrt.open_osfhandle(
            handle, os.O_RDONLY | getattr(os, "O_BINARY", 0)
        )
    except OSError:
        _close_windows_handle(handle)
        raise UnsafePathError("file handle could not be wrapped", handle_opened=True)
    try:
        stat_before = os.fstat(descriptor)
    except OSError as error:
        os.close(descriptor)
        raise UnsafePathError(
            "file handle metadata is unavailable", handle_opened=True
        ) from error
    return GuardedFile(descriptor, stat_before)


def _open_windows_guard(
    path: Path,
    access: int,
    expected_parent: str | None,
) -> tuple[int, str, int]:
    handle = _create_file(
        str(path),
        access,
        _FILE_SHARE_READ | _FILE_SHARE_WRITE,
        None,
        _OPEN_EXISTING,
        _FILE_FLAG_OPEN_REPARSE_POINT | _FILE_FLAG_BACKUP_SEMANTICS,
        None,
    )
    if handle == _INVALID_HANDLE_VALUE:
        raise UnsafePathError("path could not be guarded without delete sharing")
    numeric_handle = int(handle)
    try:
        information = _BY_HANDLE_FILE_INFORMATION()
        if not _get_file_info(handle, ctypes.byref(information)):
            raise UnsafePathError("guarded path metadata is unavailable")
        if information.dwFileAttributes & _FILE_ATTRIBUTE_REPARSE_POINT:
            raise UnsafePathError("guarded path is a redirect")
        final_path = _windows_final_path(handle)
        expected = _normalized_windows_path(path)
        if final_path != expected:
            raise UnsafePathError("guarded path resolves outside its lexical location")
        if expected_parent is not None:
            parent = _normalized_windows_path(Path(expected_parent))
            if os.path.dirname(final_path) != parent:
                raise UnsafePathError("guarded child resolves outside its parent")
        return numeric_handle, final_path, information.dwFileAttributes
    except BaseException as error:
        _close_windows_handle(numeric_handle)
        if isinstance(error, UnsafePathError) and not error.handle_opened:
            raise UnsafePathError(str(error), handle_opened=True) from error
        raise


def _windows_final_path(handle: int) -> str:
    required = _get_final_path(handle, None, 0, 0)
    if not required:
        raise UnsafePathError("guarded final path is unavailable")
    buffer = ctypes.create_unicode_buffer(required + 1)
    written = _get_final_path(handle, buffer, len(buffer), 0)
    if not written or written >= len(buffer):
        raise UnsafePathError("guarded final path could not be captured")
    value = buffer.value
    if value.startswith("\\\\?\\UNC\\"):
        value = "\\\\" + value[8:]
    elif value.startswith("\\\\?\\"):
        value = value[4:]
    return _normalized_windows_path(Path(value))


def _normalized_windows_path(path: Path) -> str:
    return unicodedata.normalize("NFC", os.path.normcase(os.path.abspath(path)))


def _close_windows_handle(handle: int) -> None:
    if os.name == "nt":
        _close_handle(handle)
