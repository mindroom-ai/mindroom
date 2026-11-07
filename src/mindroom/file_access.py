"""Resolve model-supplied file paths under an agent's ``file_access`` setting.

``workspace`` confines paths to the agent workspace; ``unrestricted`` allows any
regular file the process can read. Tools write such paths here too, and tools that
upload such a file check and read it here.
"""

from __future__ import annotations

import os
from contextlib import ExitStack, contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, BinaryIO

from mindroom.config.resolution import file_access as resolved_file_access
from mindroom.path_confinement import open_regular_file_within_root, resolve_path_within_root
from mindroom.path_safety import resolve_base_dir_path, resolve_tool_base_dir, write_resolved_file

if TYPE_CHECKING:
    from collections.abc import Iterator

    from mindroom.config.main import Config
    from mindroom.config.models import FileAccess


@dataclass(frozen=True)
class AuthorizedFile:
    """An existing regular file whose authorization only survives a no-follow open below ``root``.

    ``root`` is the caller's workspace spelling, so a workspace root replaced by a link
    is refused, or the filesystem anchor in unrestricted mode; ``relative`` is the
    canonical path below it. Read the file through :meth:`open` or ``read_regular_file_within_root(root, relative)``. The object carries no
    reopenable full path on purpose: ``display_path`` is for messages and receipts only.
    """

    root: Path
    relative: Path
    display_path: str

    @property
    def name(self) -> str:
        """Return the file name for staging copies, MIME guessing, and default titles."""
        return self.relative.name

    @contextmanager
    def open(self) -> Iterator[BinaryIO]:
        """Open the file below its authorizing root without following links or blocking on a FIFO."""
        with (
            open_regular_file_within_root(self.root, self.relative) as descriptor,
            os.fdopen(descriptor, "rb", closefd=False) as file,
        ):
            yield file


def agent_file_access(config: Config | None, agent_name: str | None) -> FileAccess:
    """Resolve one agent's file_access; without an agent the configured default applies, without config ``workspace``."""
    if config is None:
        return "workspace"
    return resolved_file_access(config, agent_name)


def _requested_path(raw_path: str) -> Path:
    """Return one model-supplied path with ``~`` expanded, so reads and writes name the same file.

    A name such as ``~draft.md`` that names no user stays as written.
    """
    return Path(os.path.expanduser(raw_path))  # noqa: PTH111 - Path.expanduser raises for an unknown user.


def resolve_agent_file(
    raw_path: str,
    *,
    workspace_root: Path | None,
    file_access: FileAccess,
    field_name: str,
) -> AuthorizedFile:
    """Return the authorized file for one model-supplied path, or raise ``ValueError``."""
    requested = _requested_path(raw_path)
    if file_access == "unrestricted":
        base = workspace_root if workspace_root is not None else Path.cwd()
        candidate = requested if requested.is_absolute() else base / requested
        try:
            resolved = candidate.resolve(strict=True)
        except OSError as exc:
            detail = exc.strerror or exc
            msg = f"{field_name} '{raw_path}' does not exist or cannot be read: {detail}"
            raise ValueError(msg) from exc
        root = Path(resolved.anchor)
        canonical_root = root
    else:
        if workspace_root is None:
            msg = f"{field_name} '{raw_path}' requires an agent workspace; file_access is 'workspace'."
            raise ValueError(msg)
        root = workspace_root
        try:
            canonical_root = workspace_root.resolve()
            resolved = resolve_path_within_root(canonical_root, requested, symlinks="internal", strict=True)
        except (ValueError, OSError) as exc:
            msg = f"{field_name} '{raw_path}' must be an existing file inside the agent workspace (file_access is 'workspace'): {exc}"
            raise ValueError(msg) from exc
    if not resolved.is_file():
        msg = f"{field_name} '{raw_path}' is not a regular file."
        raise ValueError(msg)
    return AuthorizedFile(root=root, relative=resolved.relative_to(canonical_root), display_path=str(resolved))


def write_agent_file(
    raw_path: str,
    payload: bytes | BinaryIO,
    *,
    workspace_root: Path | None,
    file_access: FileAccess,
) -> Path:
    """Publish one model-supplied path where the agent's ``file_access`` allows and return the resolved path.

    Paths resolve as for reads; any setting but ``unrestricted`` refuses paths outside the workspace and agents
    without one.
    """
    requested = _requested_path(raw_path)
    restrict = file_access != "unrestricted"
    if restrict and workspace_root is None:
        msg = f"Path '{raw_path}' requires an agent workspace; file_access is 'workspace'."
        raise ValueError(msg)
    base_dir = resolve_tool_base_dir(workspace_root)
    resolved = resolve_base_dir_path(base_dir, str(requested), restrict)
    write_resolved_file(base_dir, resolved, payload)
    return resolved


class UploadSourceError(Exception):
    """A file refused as an upload; `code` is `invalid_local_path` or `too_large`, and `str()` is the agent's message."""

    def __init__(self, *, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


def checked_upload_source(
    local_path: str,
    *,
    workspace_root: Path | None,
    file_access: FileAccess,
    max_bytes: int,
    field_name: str = "local_path",
) -> AuthorizedFile:
    """Return the authorized file to upload, refusing it before any request when it is too large."""
    try:
        source = resolve_agent_file(
            local_path,
            workspace_root=workspace_root,
            file_access=file_access,
            field_name=field_name,
        )
    except ValueError as exc:
        raise UploadSourceError(code="invalid_local_path", message=str(exc)) from None
    with open_upload_source(source, max_bytes):
        return source


@contextmanager
def open_upload_source(source: AuthorizedFile, max_bytes: int) -> Iterator[tuple[BinaryIO, int]]:
    """Open a checked upload and yield it with its size, refusing it when it became unreadable or is too large.

    The size is the open file's own, so a request that streams it can send an exact length.
    """
    with ExitStack() as stack:
        try:
            handle = stack.enter_context(source.open())
        except OSError as exc:
            raise _unreadable(source, exc) from None
        size = os.fstat(handle.fileno()).st_size
        if size > max_bytes:
            msg = f"{source.display_path} is {size} bytes; the limit is {max_bytes} bytes."
            raise UploadSourceError(code="too_large", message=msg)
        yield handle, size


def read_upload_source(source: AuthorizedFile, max_bytes: int) -> bytes:
    """Return a checked upload's bytes, refusing it when it became unreadable or grew past `max_bytes`."""
    try:
        with source.open() as handle:
            content = handle.read(max_bytes + 1)
    except OSError as exc:
        raise _unreadable(source, exc) from None
    if len(content) > max_bytes:
        msg = f"{source.display_path} grew past the {max_bytes}-byte limit."
        raise UploadSourceError(code="too_large", message=msg)
    return content


def _unreadable(source: AuthorizedFile, exc: OSError) -> UploadSourceError:
    # A file or workspace replaced by a link after the check is refused here, without following it.
    reason = exc.strerror or type(exc).__name__
    return UploadSourceError(code="invalid_local_path", message=f"{source.display_path} cannot be read: {reason}.")
