"""Bounded, descriptor-confined reads from explicitly selected local folders."""

from __future__ import annotations

import hashlib
import heapq
import json
import os
import stat
from pathlib import Path

from mindroom.desktop.protocol import MAX_INLINE_RESPONSE_BYTES
from mindroom.path_confinement import open_directory_within_root, open_regular_file_within_root

_MAX_ENTRIES = 200
_MAX_READ_BYTES = 16_384


def _serialized_size(value: object) -> int:
    """Measure ``value`` the same way ``DesktopResponse.content_bytes`` measures a reply."""
    return len(json.dumps(value, separators=(",", ":")).encode())


def _fit_entries(entries: list[dict[str, str]], *, key: str, truncated: bool) -> dict[str, object]:
    """Keep the fitting prefix of ``entries``, in their existing deterministic order, under the inline reply budget."""
    total = len(entries)

    def reply(count: int) -> dict[str, object]:
        return {key: entries[:count], "truncated": truncated or count < total}

    if _serialized_size(reply(total)) <= MAX_INLINE_RESPONSE_BYTES:
        return reply(total)
    low, high = 0, total - 1
    while low < high:
        middle = (low + high + 1) // 2
        if _serialized_size(reply(middle)) <= MAX_INLINE_RESPONSE_BYTES:
            low = middle
        else:
            high = middle - 1
    return reply(low)


class DesktopFilesystemError(ValueError):
    """Invalid or unavailable local file access."""


class DesktopFilesystem:
    """Read only below pinned, caller-authorized folder descriptors."""

    def __init__(self, roots: tuple[Path, ...]) -> None:
        if not all(hasattr(os, flag) for flag in ("O_DIRECTORY", "O_NOFOLLOW", "O_NONBLOCK")):
            message = "This platform cannot confine local file access safely."
            raise DesktopFilesystemError(message)
        self._roots: dict[str, tuple[Path, int]] = {}
        self._closed = False
        try:
            for root in roots:
                canonical = root.expanduser().resolve(strict=True)
                root_id = hashlib.sha256(os.fsencode(canonical)).hexdigest()
                if root_id in self._roots:
                    continue
                descriptor = os.open(canonical, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
                self._roots[root_id] = (canonical, descriptor)
        except (OSError, ValueError, RuntimeError) as exc:
            # A symlink loop raises RuntimeError from Path.resolve(strict=True) on Python 3.12
            # and OSError on 3.13; both must still close every descriptor opened so far.
            self.close()
            message = f"Cannot open local folder: {exc}"
            raise DesktopFilesystemError(message) from exc

    def _root(self, root_id: str) -> tuple[Path, int]:
        if self._closed:
            message = "Local file access is closed."
            raise DesktopFilesystemError(message)
        try:
            return self._roots[root_id]
        except (KeyError, TypeError) as exc:
            message = "Unknown local folder."
            raise DesktopFilesystemError(message) from exc

    @staticmethod
    def _relative(path: str) -> Path:
        if not isinstance(path, str) or "\x00" in path:
            message = "Invalid local path."
            raise DesktopFilesystemError(message)
        relative = Path(path)
        if relative.is_absolute() or ".." in relative.parts:
            message = "Path must stay within its local folder."
            raise DesktopFilesystemError(message)
        return relative

    def list_folders(self) -> dict[str, object]:
        """Return stable IDs and display paths for pinned folders, trimmed to fit the inline reply budget."""
        if self._closed:
            message = "Local file access is closed."
            raise DesktopFilesystemError(message)
        folders = [{"id": root_id, "name": path.name, "path": str(path)} for root_id, (path, _) in self._roots.items()]
        return _fit_entries(folders, key="folders", truncated=False)

    def list_directory(self, root_id: str, path: str = ".") -> dict[str, object]:
        """List at most 200 direct entries without following links, trimmed to fit the inline reply budget."""
        _, root_fd = self._root(root_id)
        relative = self._relative(path)
        try:
            with open_directory_within_root(root_fd, relative) as directory:
                with os.scandir(directory) as scan:
                    names = heapq.nsmallest(_MAX_ENTRIES + 1, scan, key=lambda entry: entry.name)
                entries: list[dict[str, str]] = []
                for entry in names[:_MAX_ENTRIES]:
                    try:
                        mode = entry.stat(follow_symlinks=False).st_mode
                    except FileNotFoundError:
                        # The entry vanished between the scan and this stat; skip it, not the rest.
                        continue
                    kind = (
                        "directory"
                        if stat.S_ISDIR(mode)
                        else "file"
                        if stat.S_ISREG(mode)
                        else "symlink"
                        if stat.S_ISLNK(mode)
                        else "other"
                    )
                    entries.append({"name": entry.name, "type": kind})
                return _fit_entries(entries, key="entries", truncated=len(names) > _MAX_ENTRIES)
        except (OSError, ValueError) as exc:
            message = f"Cannot list local directory: {exc}"
            raise DesktopFilesystemError(message) from exc

    def read_file(self, root_id: str, path: str, offset: int = 0) -> dict[str, object]:
        """Read one bounded UTF-8 chunk from a regular file."""
        _, root_fd = self._root(root_id)
        relative = self._relative(path)
        if not isinstance(offset, int) or isinstance(offset, bool) or offset < 0:
            message = "File offset must be a nonnegative integer."
            raise DesktopFilesystemError(message)
        try:
            with open_regular_file_within_root(root_fd, relative) as descriptor:
                data = os.pread(descriptor, _MAX_READ_BYTES + 1, offset)
                chunk = data[:_MAX_READ_BYTES]
                while True:
                    try:
                        decoded = chunk.decode("utf-8")
                        break
                    except UnicodeDecodeError as exc:
                        if exc.reason != "unexpected end of data" or len(data) <= len(chunk):
                            message = "File must contain valid UTF-8 text."
                            raise DesktopFilesystemError(message) from exc
                        chunk = chunk[: exc.start]
                if any((byte < 32 and byte not in (9, 10, 13)) or byte == 127 for byte in chunk):
                    message = "Binary files cannot be read."
                    raise DesktopFilesystemError(message)
                next_offset = offset + len(chunk)
                eof = len(data) <= _MAX_READ_BYTES and next_offset == offset + len(data)
                return {
                    "text": decoded,
                    "offset": offset,
                    "next_offset": next_offset,
                    "eof": eof,
                    "truncated": not eof,
                }
        except (OSError, ValueError, OverflowError) as exc:
            if isinstance(exc, DesktopFilesystemError):
                raise
            message = f"Cannot read local file: {exc}"
            raise DesktopFilesystemError(message) from exc

    def close(self) -> None:
        """Release every pinned folder descriptor."""
        if self._closed:
            return
        self._closed = True
        for _, descriptor in self._roots.values():
            os.close(descriptor)
        self._roots.clear()
