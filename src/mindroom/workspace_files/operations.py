"""The files of one agent's workspace, as a chat client lists, reads and changes them.

Paths are relative to the workspace root, which the caller resolved itself and trusts; code the agent runs writes
everything below it, so nothing there is trusted. Links are followed only while they stay inside, and every open walks
from the root without following a link, so a link swapped in after the check is refused. Only folders, regular files
and links are listed or read. Nothing is saved, made, moved or deleted as or below ``.git``.

The workspace is the agent's ``HOME``, so credentials are never listed, read or changed, even through a link, and a
path naming one answers 404 as an absent one does: see ``is_hidden``. A folder that keeps one of them by its place,
such as ``.config``, is never moved or deleted, and nothing moves in under its name, since renamed it would show.

Refusals raise ``HTTPException``. These calls block: run them off the event loop.
"""

from __future__ import annotations

import base64
import hashlib
import os
import re
import shutil
import stat
import threading
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path, PurePath, PurePosixPath
from typing import TYPE_CHECKING, Literal

from fastapi import HTTPException

from mindroom.path_confinement import is_git_metadata_path, open_directory_within_root, open_regular_file_at
from mindroom.path_safety import resolve_base_dir_path, resolve_tool_base_dir, write_resolved_file

if TYPE_CHECKING:
    from collections.abc import Iterator

_MAX_FILE_BYTES = 5 << 20
_MAX_FOLDER_ENTRIES = 5000

# What a shell keeps at the top of its HOME to sign in somewhere; a folder's place hides everything below it.
_HOME_CREDENTIALS = frozenset(
    {
        (".ssh",),
        (".gnupg",),
        (".aws",),
        (".azure",),
        (".kube",),
        (".docker",),
        (".password-store",),
        (".mindroom",),
        (".config", "gh"),
        (".config", "gcloud"),
        (".config", "hub"),
        (".config", "glab-cli"),
        (".config", "doctl"),
        (".config", "git", "credentials"),
        (".claude", ".credentials.json"),
        (".codex", "auth.json"),
        (".cargo", "credentials"),
        (".cargo", "credentials.toml"),
        (".terraform.d", "credentials.tfrc.json"),
        (".vault-token",),
        (".cache", "huggingface", "token"),
    },
)
# The first names of those places, so most paths are cleared by one lookup.
_HOME_CREDENTIAL_TOPS = frozenset(place[0] for place in _HOME_CREDENTIALS)
# Credential files by name, in any folder: a project keeps its own `.env` and `.npmrc`.
_CREDENTIAL_NAMES = frozenset({".netrc", ".git-credentials", ".pgpass", ".pypirc", ".npmrc"})
# `.env`, `.env.local`, `.envrc`; SSH keys such as `id_rsa`, `id_ed25519_sk`, `id_ecdsa.pub` and `id_rsa-cert.pub`.
_CREDENTIAL_NAME = re.compile(r"\.env.*|id_(?:rsa|dsa|ecdsa|ed25519|xmss)(?:_sk)?(?:\.pub|-cert\.pub)?")
# Below `.git`: its config and a submodule's, whose remotes' URLs can hold a token, and credential files beside them.
_GIT_CREDENTIAL_NAMES = frozenset({"config", "credentials"})

_KINDS: dict[int, Literal["file", "folder", "link"]] = {
    stat.S_IFREG: "file",
    stat.S_IFDIR: "folder",
    stat.S_IFLNK: "link",
}
_NOT_A_FILE = "That path is not a regular file."
# Saves and moves through this process check what is there and change it one at a time, so two saves of one version,
# or two moves onto one name, never both land.
_CHANGES = threading.Lock()


@dataclass(frozen=True)
class _Entry:
    """One entry of a folder."""

    name: str
    path: str
    kind: Literal["file", "folder", "link"]
    size: int
    """The file's size in bytes, 0 for a folder or a link."""
    mtime: int
    """When it last changed, in milliseconds since the epoch."""
    target: Literal["file", "folder"] | None
    """What a link leads to, None when it leads outside the workspace, to a credential, or nowhere."""


@dataclass(frozen=True)
class Folder:
    """A folder's entries, folders first, then by name."""

    path: str
    entries: list[_Entry]
    truncated: bool
    """Whether the folder holds more than ``_MAX_FOLDER_ENTRIES`` entries, which are left out."""


@dataclass(frozen=True)
class FileContent:
    """A file's content: UTF-8 text as it is, anything else as base64."""

    path: str
    encoding: Literal["utf-8", "base64"]
    content: str
    size: int
    mtime: int
    etag: str
    """The SHA-256 of the content, in hex."""


@dataclass(frozen=True)
class Saved:
    """A saved file's new version."""

    etag: str
    size: int
    mtime: int


class StaleEtagError(Exception):
    """A save whose etag the file no longer has; ``etag`` is the file's now, None once it is gone."""

    def __init__(self, etag: str | None) -> None:
        super().__init__("The file changed since it was read.")
        self.etag = etag


def is_hidden(path: PurePath) -> bool:
    """Return whether a workspace-relative path is, or lies in, a credential a chat client never sees.

    Names match in any case; docs/workspace-files.md lists them for users. A name that isn't UTF-8, which no answer
    can carry, is hidden too.
    """
    try:
        str(path).encode()
    except UnicodeEncodeError:
        return True
    parts = tuple(part.casefold() for part in path.parts)
    if parts and parts[0] in _HOME_CREDENTIAL_TOPS and any(parts[: len(place)] == place for place in _HOME_CREDENTIALS):
        return True
    if any(part in _CREDENTIAL_NAMES or _CREDENTIAL_NAME.fullmatch(part) for part in parts):
        return True
    return ".git" in parts[:-1] and (parts[-1] in _GIT_CREDENTIAL_NAMES or parts[-1].endswith(".credentials"))


def _relative(raw: str) -> PurePosixPath:
    """Return the workspace-relative path a client named; ``""`` and ``"."`` name the root."""
    if "\0" in raw or "\\" in raw:
        raise HTTPException(400, "A path may not hold NUL or backslashes.")
    if raw.startswith(("/", "~")):
        raise HTTPException(400, "Paths are relative to the workspace.")
    path = PurePosixPath(raw)
    if ".." in path.parts:
        raise HTTPException(400, "A path may not climb out with '..'.")
    return path


def _too_large() -> HTTPException:
    return HTTPException(413, f"The file is over {_MAX_FILE_BYTES >> 20} MiB.")


def _absent() -> HTTPException:
    return HTTPException(404, "No such file or folder.")


@dataclass(frozen=True)
class _Found:
    """A path inside the workspace: ``base`` the pinned root, ``relative`` the canonical path below it."""

    base: Path
    relative: Path


def _find(root: Path, path: PurePosixPath) -> _Found:
    """Follow ``path`` from the root through links that stay inside; refuse one that leaves or names a credential."""
    if is_hidden(path):
        raise _absent()
    try:
        base = resolve_tool_base_dir(root)
    except ValueError:
        raise HTTPException(409, "The workspace was moved or replaced by a link.") from None
    try:
        target = resolve_base_dir_path(base, str(path), restrict_to_base_dir=True)
    except ValueError:
        raise HTTPException(403, "That path leads outside the workspace.") from None
    except OSError as error:
        raise HTTPException(403, f"That path can't be followed: {error.strerror}.") from None
    found = _Found(base, target.relative_to(base))
    if is_hidden(found.relative):
        raise _absent()
    return found


def _leaf(root: Path, path: PurePosixPath) -> tuple[_Found, str]:
    """Return the folder holding ``path``'s last part, followed as ``_find`` does, and that part, which is not."""
    if is_hidden(path):
        raise _absent()
    if not path.parts:
        raise HTTPException(400, "The workspace itself can't be changed.")
    parent = _find(root, path.parent)
    if is_hidden(parent.relative / path.name):
        raise _absent()
    return parent, path.name


def _refuse_git(*paths: PurePath) -> None:
    if any(is_git_metadata_path(Path(path)) for path in paths):
        raise HTTPException(403, "Git's own files (.git) can't be changed here.")


def _refuse_credential_place(*paths: PurePath) -> None:
    """Refuse a path that is a folder above a credential known by its place, which moving it would show."""
    for path in paths:
        parts = tuple(part.casefold() for part in path.parts)
        if any(place[: len(parts)] == parts for place in _HOME_CREDENTIALS):
            raise HTTPException(403, "That folder keeps credentials, so it can't be moved or deleted here.")


@contextmanager
def _refusals() -> Iterator[None]:
    """Answer a filesystem error, or a link swapped in meanwhile, with its HTTP status."""
    try:
        yield
    except FileNotFoundError:
        raise _absent() from None
    except NotADirectoryError:
        raise HTTPException(400, "That path is not a folder.") from None
    except FileExistsError:
        raise HTTPException(409, "Something is already there.") from None
    except ValueError:
        raise HTTPException(400, _NOT_A_FILE) from None
    except OSError as error:
        raise HTTPException(403, f"That can't be done here: {error.strerror}.") from None


def _mtime(status: os.stat_result) -> int:
    return status.st_mtime_ns // 1_000_000


def _etag(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def _link_target(root: Path, path: PurePosixPath) -> Literal["file", "folder"] | None:
    try:
        found = _find(root, path)
        if not found.relative.parts:
            return "folder"
        # Through its folder's descriptor, so a folder swapped for a link meanwhile is not followed.
        with open_directory_within_root(found.base, found.relative.parent) as directory:
            status = os.stat(found.relative.name, dir_fd=directory, follow_symlinks=False)
    except (HTTPException, OSError):
        return None
    kind = _KINDS.get(stat.S_IFMT(status.st_mode))
    return kind if kind != "link" else None


def list_folder(root: Path, raw_path: str) -> Folder:
    """Return one folder's entries; the root of a workspace not made yet lists empty."""
    path = _relative(raw_path)
    shown = str(path) if path.parts else ""
    if not path.parts and not root.exists():
        return Folder(shown, [], truncated=False)
    found = _find(root, path)
    entries: list[_Entry] = []
    truncated = False
    with (
        _refusals(),
        open_directory_within_root(found.base, found.relative) as directory,
        os.scandir(directory) as items,
    ):
        for item in items:
            child = path / item.name
            if is_hidden(child) or is_hidden(found.relative / item.name):
                continue
            try:
                status = item.stat(follow_symlinks=False)
            except OSError:
                continue
            kind = _KINDS.get(stat.S_IFMT(status.st_mode))
            if kind is None:
                continue
            if len(entries) == _MAX_FOLDER_ENTRIES:
                truncated = True
                break
            entries.append(
                _Entry(
                    name=item.name,
                    path=str(child),
                    kind=kind,
                    size=status.st_size if kind == "file" else 0,
                    mtime=_mtime(status),
                    target=_link_target(root, child) if kind == "link" else None,
                ),
            )
    entries.sort(key=lambda entry: ("folder" not in {entry.kind, entry.target}, entry.name.casefold(), entry.name))
    return Folder(shown, entries, truncated)


def _read(found: _Found) -> tuple[bytes, os.stat_result]:
    """Read a regular file, refusing anything else before opening it, and one over ``_MAX_FILE_BYTES`` with 413."""
    if not found.relative.parts:
        raise HTTPException(400, _NOT_A_FILE)
    with _refusals(), open_directory_within_root(found.base, found.relative.parent) as directory:
        if not stat.S_ISREG(os.stat(found.relative.name, dir_fd=directory, follow_symlinks=False).st_mode):
            raise HTTPException(400, _NOT_A_FILE)
        descriptor = open_regular_file_at(directory, found.relative.name)
    with os.fdopen(descriptor, "rb") as file:
        status = os.fstat(file.fileno())
        payload = b"" if status.st_size > _MAX_FILE_BYTES else file.read(_MAX_FILE_BYTES + 1)
    if status.st_size > _MAX_FILE_BYTES or len(payload) > _MAX_FILE_BYTES:
        raise _too_large()
    return payload, status


def read_file(root: Path, raw_path: str) -> FileContent:
    """Return a file's content, size, time and etag."""
    path = _relative(raw_path)
    payload, status = _read(_find(root, path))
    try:
        encoding, content = "utf-8", payload.decode("utf-8")
    except UnicodeDecodeError:
        encoding, content = "base64", base64.b64encode(payload).decode("ascii")
    return FileContent(str(path), encoding, content, len(payload), _mtime(status), _etag(payload))


def save_file(root: Path, raw_path: str, payload: bytes, etag: str | None) -> Saved:
    """Save a file atomically, keeping an existing one's mode and making the folders above a new one.

    Given ``etag``, it saves only while the file still has that version, else raises ``StaleEtagError``.
    """
    if len(payload) > _MAX_FILE_BYTES:
        raise _too_large()
    path = _relative(raw_path)
    _refuse_git(path)
    found = _find(root, path)
    _refuse_git(found.relative)
    with _CHANGES:
        try:
            current = _etag(_read(found)[0])
        except HTTPException as error:
            if error.status_code != 404:
                raise
            current = None
        if etag is not None and etag != current:
            raise StaleEtagError(current)
        with _refusals():
            write_resolved_file(found.base, found.base / found.relative, payload)
            with open_directory_within_root(found.base, found.relative.parent) as directory:
                status = os.stat(found.relative.name, dir_fd=directory, follow_symlinks=False)
    return Saved(_etag(payload), status.st_size, _mtime(status))


def make_folder(root: Path, raw_path: str) -> None:
    """Make a folder and the folders above it, answering 409 when something is already there."""
    path = _relative(raw_path)
    _refuse_git(path)
    parent, name = _leaf(root, path)
    _refuse_git(parent.relative / name)
    with _refusals(), open_directory_within_root(parent.base, parent.relative, create=True) as directory:
        os.mkdir(name, dir_fd=directory)


def move(root: Path, raw_path: str, raw_to: str) -> None:
    """Move or rename a file, folder or link, never what a link leads to; 409 when ``raw_to`` is taken."""
    path, to = _relative(raw_path), _relative(raw_to)
    _refuse_git(path, to)
    source, source_name = _leaf(root, path)
    target, target_name = _leaf(root, to)
    moved, destination = source.relative / source_name, target.relative / target_name
    _refuse_git(moved, destination)
    _refuse_credential_place(moved, destination)
    if destination == moved:
        raise HTTPException(400, "That is already its name.")
    if destination.is_relative_to(moved):
        raise HTTPException(400, "A folder can't move into itself.")
    with _CHANGES, _refusals(), open_directory_within_root(source.base, source.relative) as source_directory:
        os.stat(source_name, dir_fd=source_directory, follow_symlinks=False)
        with open_directory_within_root(target.base, target.relative, create=True) as target_directory:
            try:
                os.stat(target_name, dir_fd=target_directory, follow_symlinks=False)
            except FileNotFoundError:
                os.rename(source_name, target_name, src_dir_fd=source_directory, dst_dir_fd=target_directory)
            else:
                raise FileExistsError


def delete(root: Path, raw_path: str) -> None:
    """Delete a file, a link but not what it leads to, or a folder with everything in it."""
    path = _relative(raw_path)
    _refuse_git(path)
    parent, name = _leaf(root, path)
    removed = parent.relative / name
    _refuse_git(removed)
    _refuse_credential_place(removed)
    with _refusals(), open_directory_within_root(parent.base, parent.relative) as directory:
        if stat.S_ISDIR(os.stat(name, dir_fd=directory, follow_symlinks=False).st_mode):
            # Walks every level by descriptor, never following a link.
            shutil.rmtree(name, dir_fd=directory)
        else:
            os.unlink(name, dir_fd=directory)
