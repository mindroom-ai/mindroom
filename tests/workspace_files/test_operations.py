"""A workspace's files as a chat client lists, reads and changes them: paths stay inside, credentials stay hidden."""

from __future__ import annotations

import base64
import hashlib
import os
import socket
from pathlib import Path, PurePosixPath
from typing import TYPE_CHECKING

import pytest
from fastapi import HTTPException

from mindroom.workspace_files import operations
from mindroom.workspace_files.operations import (
    StaleEtagError,
    delete,
    is_hidden,
    list_folder,
    make_folder,
    move,
    read_file,
    save_file,
)

if TYPE_CHECKING:
    from collections.abc import Callable


def _etag(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def _status(call: Callable[[], object]) -> int:
    """The HTTP status a call answers: 200 when it returns."""
    try:
        call()
    except HTTPException as error:
        return error.status_code
    return 200


@pytest.fixture
def root(tmp_path: Path) -> Path:
    """A workspace with a project, a Git checkout, credentials in the agent's home, and a secret just outside."""
    workspace = tmp_path / "workspace"
    (workspace / "src").mkdir(parents=True)
    (workspace / "src" / "main.py").write_text("print('hi')\n")
    (workspace / "notes.md").write_text("# Notes\n")
    (workspace / "repo" / ".git").mkdir(parents=True)
    (workspace / "repo" / ".git" / "config").write_text("[remote]\nurl = https://token@example.org\n")
    (workspace / "repo" / ".git" / "HEAD").write_text("ref: refs/heads/main\n")
    (workspace / ".ssh").mkdir()
    (workspace / ".ssh" / "id_ed25519").write_text("key")
    (workspace / ".config" / "gh").mkdir(parents=True)
    (workspace / ".config" / "gh" / "hosts.yml").write_text("token")
    (workspace / ".config" / "app.toml").write_text("theme = 'dark'\n")
    (workspace / ".env").write_text("TOKEN=1\n")
    (workspace / "src" / ".env.local").write_text("TOKEN=2\n")
    (tmp_path / "outside.txt").write_text("secret")
    return workspace


def _names(root: Path, path: str = "") -> list[str]:
    return [entry.name for entry in list_folder(root, path).entries]


@pytest.mark.parametrize(
    "path",
    ["../outside.txt", "/etc/passwd", "~/notes.md", "src/../notes.md", "a\0b", "src\\main.py"],
)
def test_a_path_that_could_leave_the_workspace_is_refused(root: Path, path: str) -> None:
    """Absolute paths, ``~``, ``..``, NUL and backslashes answer 400 before anything is opened."""
    assert _status(lambda: read_file(root, path)) == 400
    assert _status(lambda: save_file(root, path, b"x", None)) == 400


def test_a_link_is_followed_only_while_it_stays_inside(root: Path) -> None:
    """A link out answers 403, read or written through; a link inside reads its target, and lists by what it reaches."""
    (root / "out").symlink_to(root.parent / "outside.txt")
    (root / "out-dir").symlink_to(root.parent)
    (root / "main").symlink_to(root / "src" / "main.py")
    (root / "code").symlink_to("src")
    (root / "nowhere").symlink_to("missing")

    assert _status(lambda: read_file(root, "out")) == 403
    assert _status(lambda: read_file(root, "out-dir/outside.txt")) == 403
    assert _status(lambda: save_file(root, "out-dir/planted.txt", b"x", None)) == 403
    assert not (root.parent / "planted.txt").exists()
    assert read_file(root, "main").content == "print('hi')\n"
    assert read_file(root, "code/main.py").content == "print('hi')\n"
    targets = {
        entry.name: (entry.kind, entry.target) for entry in list_folder(root, "").entries if entry.kind == "link"
    }
    assert targets == {
        "out": ("link", None),
        "out-dir": ("link", None),
        "main": ("link", "file"),
        "code": ("link", "folder"),
        "nowhere": ("link", None),
    }


def test_fifos_and_sockets_are_neither_listed_nor_read(root: Path) -> None:
    """Special files are left out of a listing, and reading one answers 400 without blocking."""
    os.mkfifo(root / "pipe")
    with socket.socket(socket.AF_UNIX) as server:
        server.bind(str(root / "sock"))

        assert "pipe" not in _names(root)
        assert "sock" not in _names(root)
        assert _status(lambda: read_file(root, "pipe")) == 400
        assert _status(lambda: read_file(root, "sock")) == 400


def test_nothing_below_git_is_changed(root: Path) -> None:
    """Saving, making, moving and deleting in or as ``.git`` answer 403, and Git's config stays as it was."""
    assert _status(lambda: save_file(root, "repo/.git/config", b"[core]\nfsmonitor = x\n", None)) == 403
    assert _status(lambda: save_file(root, "repo/.git/hooks/pre-commit", b"x", None)) == 403
    assert _status(lambda: make_folder(root, "repo/.git/hooks")) == 403
    assert _status(lambda: move(root, "repo/.git", "repo/git")) == 403
    assert _status(lambda: move(root, "notes.md", "repo/.git/notes.md")) == 403
    assert _status(lambda: delete(root, "repo/.git")) == 403
    assert _status(lambda: delete(root, "repo/.git/HEAD")) == 403
    assert _status(lambda: delete(root, "repo/.git/config")) == 403
    assert _status(lambda: move(root, "repo/.git/config", "config")) == 403
    (root / "g").symlink_to("repo/.git")
    assert _status(lambda: save_file(root, "g/hooks/pre-commit", b"x", None)) == 403
    assert _status(lambda: make_folder(root, "g/hooks")) == 403
    assert _status(lambda: move(root, "notes.md", "g/notes.md")) == 403
    assert _status(lambda: delete(root, "g/HEAD")) == 403
    assert (root / "repo" / ".git" / "config").read_text().startswith("[remote]")
    assert read_file(root, "repo/.git/HEAD").content == "ref: refs/heads/main\n"


@pytest.mark.parametrize(
    "path",
    [
        ".env",
        "src/.env.local",
        ".envrc",
        ".ssh",
        ".ssh/id_ed25519",
        ".AWS/credentials",
        ".config/gh/hosts.yml",
        ".gnupg/secring.gpg",
        ".aws/credentials",
        ".mindroom/grant",
        ".claude/.credentials.json",
        ".codex/auth.json",
        ".config/git/credentials",
        "deep/.netrc",
        "deep/.npmrc",
        "deep/.pypirc",
        "deep/.pgpass",
        "deep/.git-credentials",
        "keys/id_rsa",
        "keys/id_ecdsa_sk.pub",
        "keys/id_rsa-cert.pub",
        "repo/.git/config",
        "repo/.git/modules/lib/config",
        "repo/.git/credentials",
        "repo/.git/store.credentials",
    ],
)
def test_credentials_are_hidden_wherever_their_rule_places_them(path: str) -> None:
    """The workspace is the agent's ``HOME``: its sign-in folders, credential files anywhere, and Git's config."""
    assert is_hidden(PurePosixPath(path))


@pytest.mark.parametrize(
    "path",
    ["notes.md", "src/environment.py", "keys/id_utils.py", ".config/app.toml", "deep/.ssh/notes", "repo/.git/HEAD"],
)
def test_ordinary_files_are_not_hidden(path: str) -> None:
    """Names that only resemble a credential, or a home folder below the top, stay visible."""
    assert not is_hidden(PurePosixPath(path))


def test_a_hidden_credential_is_never_listed_read_or_changed_even_through_a_link(root: Path) -> None:
    """Credentials answer 404 as though absent, and a link to one reaches nothing."""
    (root / "env-link").symlink_to(".env")
    (root / "keys").symlink_to(".ssh")

    assert ".env" not in _names(root)
    assert ".ssh" not in _names(root)
    assert ".env.local" not in _names(root, "src")
    assert "config" not in _names(root, "repo/.git")
    assert _names(root, ".config") == ["app.toml"]
    for path in (".env", "env-link", "keys/id_ed25519", ".ssh", "repo/.git/config", ".config/gh/hosts.yml"):
        assert _status(lambda path=path: read_file(root, path)) == 404, path
    assert _status(lambda: list_folder(root, ".ssh")) == 404
    assert _status(lambda: save_file(root, ".env", b"TOKEN=evil\n", None)) == 404
    assert _status(lambda: save_file(root, "env-link", b"TOKEN=evil\n", None)) == 404
    assert _status(lambda: delete(root, "src/.env.local")) == 404
    assert _status(lambda: move(root, ".env", "env.txt")) == 404
    assert _status(lambda: move(root, "notes.md", ".npmrc")) == 404
    assert (root / ".env").read_text() == "TOKEN=1\n"


def test_a_folder_that_keeps_a_credential_by_its_place_is_never_moved_or_deleted(root: Path) -> None:
    """``.config`` holds ``gh``'s token: renaming or deleting it, or moving something in under its name, answers 403."""
    (root / "settings").mkdir()

    assert _status(lambda: move(root, ".config", "config")) == 403
    assert _status(lambda: delete(root, ".config")) == 403
    assert _status(lambda: move(root, "settings", ".claude")) == 403
    assert _status(lambda: move(root, "settings", ".config")) == 403
    assert (root / ".config" / "gh" / "hosts.yml").read_text() == "token"
    (root / "here").symlink_to(".")
    assert _status(lambda: move(root, "here/.config", "config")) == 403
    assert _status(lambda: move(root, "settings", "here/.config")) == 403
    assert _status(lambda: delete(root, "here/.config")) == 403
    move(root, ".config/app.toml", "app.toml")
    assert (root / "app.toml").is_file()


def test_a_folder_lists_folders_first_then_by_name_with_sizes_and_times(root: Path) -> None:
    """Entries carry their path, kind, size and modification time in milliseconds; folders come first."""
    (root / "B.txt").write_text("bb")
    (root / "a.txt").write_text("a")
    os.utime(root / "a.txt", ns=(1_700_000_000_123_456_789, 1_700_000_000_123_456_789))

    listing = list_folder(root, "")

    assert listing.path == ""
    assert not listing.truncated
    assert [(entry.name, entry.kind) for entry in listing.entries] == [
        (".config", "folder"),
        ("repo", "folder"),
        ("src", "folder"),
        ("a.txt", "file"),
        ("B.txt", "file"),
        ("notes.md", "file"),
    ]
    first_file = listing.entries[3]
    assert (first_file.path, first_file.size, first_file.mtime, first_file.target) == (
        "a.txt",
        1,
        1_700_000_000_123,
        None,
    )
    assert listing.entries[0].size == 0
    assert [entry.path for entry in list_folder(root, "src").entries] == ["src/main.py"]


def test_a_long_folder_is_cut_short_and_says_so(root: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """At most the cap's entries come back, with ``truncated``."""
    monkeypatch.setattr(operations, "_MAX_FOLDER_ENTRIES", 2)

    listing = list_folder(root, "")

    assert (len(listing.entries), listing.truncated) == (2, True)


def test_a_workspace_not_made_yet_lists_empty(tmp_path: Path) -> None:
    """Before the agent's first turn its workspace has no folder: the root lists empty, and anything below is absent."""
    missing = tmp_path / "workspace"

    assert list_folder(missing, "").entries == []
    assert _status(lambda: list_folder(missing, "src")) == 404
    assert _status(lambda: read_file(missing, "notes.md")) == 404


def test_listing_a_file_or_reading_a_folder_answers_400(root: Path) -> None:
    """A file is no folder to list, and a folder or the root no file to read."""
    assert _status(lambda: list_folder(root, "notes.md")) == 400
    assert _status(lambda: read_file(root, "src")) == 400
    assert _status(lambda: read_file(root, "")) == 400
    assert _status(lambda: save_file(root, "src", b"x", None)) == 400


def test_text_reads_as_it_is_and_anything_else_as_base64_with_an_etag(root: Path) -> None:
    """UTF-8 comes as text; other bytes come as base64; both carry size, time and the content's etag."""
    binary = bytes(range(256))
    (root / "image.bin").write_bytes(binary)

    text = read_file(root, "notes.md")
    data = read_file(root, "image.bin")

    assert (text.path, text.encoding, text.content, text.size, text.etag) == (
        "notes.md",
        "utf-8",
        "# Notes\n",
        8,
        _etag(b"# Notes\n"),
    )
    assert text.mtime == (root / "notes.md").stat().st_mtime_ns // 1_000_000
    assert (data.encoding, base64.b64decode(data.content), data.size, data.etag) == (
        "base64",
        binary,
        256,
        _etag(binary),
    )


def test_a_file_over_the_limit_answers_413(root: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A file over the size limit answers 413; one at the limit reads."""
    monkeypatch.setattr(operations, "MAX_FILE_BYTES", 4)

    (root / "four.txt").write_text("1234")

    assert _status(lambda: read_file(root, "notes.md")) == 413
    assert read_file(root, "four.txt").size == 4


def test_a_save_makes_the_folders_above_and_keeps_an_existing_file_s_mode(root: Path) -> None:
    """A new file gets its folders; a rewrite keeps the mode, and both answer the new etag, size and time."""
    script = root / "run.sh"
    script.write_text("echo old\n")
    script.chmod(0o750)

    created = save_file(root, "docs/guide/intro.md", b"# Intro\n", None)
    rewritten = save_file(root, "run.sh", b"echo new\n", _etag(b"echo old\n"))

    assert (root / "docs" / "guide" / "intro.md").read_bytes() == b"# Intro\n"
    assert (created.etag, created.size) == (_etag(b"# Intro\n"), 8)
    assert script.read_text() == "echo new\n"
    assert script.stat().st_mode & 0o777 == 0o750
    assert (rewritten.etag, rewritten.size, rewritten.mtime) == (
        _etag(b"echo new\n"),
        9,
        script.stat().st_mtime_ns // 1_000_000,
    )
    assert not list(root.rglob(".mindroom-*"))


def test_a_save_with_a_stale_etag_is_refused_with_the_current_one(root: Path) -> None:
    """Someone saved meanwhile: the save changes nothing and names the file's etag, or None once it is gone."""
    (root / "notes.md").write_text("# Theirs\n")

    with pytest.raises(StaleEtagError) as stale:
        save_file(root, "notes.md", b"# Mine\n", _etag(b"# Notes\n"))
    assert stale.value.etag == _etag(b"# Theirs\n")
    assert (root / "notes.md").read_text() == "# Theirs\n"

    (root / "notes.md").unlink()
    with pytest.raises(StaleEtagError) as gone:
        save_file(root, "notes.md", b"# Mine\n", _etag(b"# Theirs\n"))
    assert gone.value.etag is None
    assert not (root / "notes.md").exists()


def test_a_save_without_an_etag_overwrites(root: Path) -> None:
    """Without an etag a save replaces whatever version the file has."""
    save_file(root, "notes.md", b"# Mine\n", None)

    assert (root / "notes.md").read_text() == "# Mine\n"


def test_make_folder_makes_the_folders_above_and_refuses_a_taken_name(root: Path) -> None:
    """Missing folders above are made too, and a folder or file already there answers 409."""
    make_folder(root, "a/b/c")

    assert (root / "a" / "b" / "c").is_dir()
    assert _status(lambda: make_folder(root, "a/b/c")) == 409
    assert _status(lambda: make_folder(root, "notes.md")) == 409


def test_move_renames_an_entry_and_moves_a_link_as_a_link(root: Path) -> None:
    """A rename moves files and folders; a link keeps pointing where it did; a taken name answers 409."""
    (root / "code").symlink_to("src")

    move(root, "src", "lib/src")
    move(root, "code", "lib/code")

    assert (root / "lib" / "src" / "main.py").is_file()
    assert (root / "lib" / "code").readlink() == Path("src")
    assert _status(lambda: move(root, "notes.md", "lib/src")) == 409
    assert _status(lambda: move(root, "missing.md", "new/found.md")) == 404
    assert not (root / "new").exists()


def test_a_folder_cannot_move_into_itself(root: Path) -> None:
    """Moving a folder below itself, or onto itself, answers 400."""
    assert _status(lambda: move(root, "src", "src/inner")) == 400
    assert _status(lambda: move(root, "src", "src")) == 400


def test_delete_removes_a_folder_with_its_content_and_a_link_but_not_its_target(root: Path) -> None:
    """Deleting a link leaves its target, a folder goes with its content, and an absent entry answers 404."""
    (root / "code").symlink_to("src")

    delete(root, "code")
    assert (root / "src" / "main.py").is_file()
    delete(root, "src")

    assert not (root / "src").exists()
    assert not (root / "code").is_symlink()
    assert _status(lambda: delete(root, "src")) == 404


@pytest.mark.parametrize("path", ["", ".", "./"])
def test_the_workspace_itself_cannot_be_changed(root: Path, path: str) -> None:
    """The root is never deleted, renamed or replaced, and never made again over itself."""
    assert _status(lambda: delete(root, path)) == 400
    assert _status(lambda: move(root, path, "elsewhere")) == 400
    assert _status(lambda: move(root, "notes.md", path)) == 400
    assert _status(lambda: make_folder(root, path)) == 400
    assert _status(lambda: save_file(root, path, b"x", None)) == 400
    assert root.is_dir()


def test_a_workspace_replaced_by_a_link_is_refused(tmp_path: Path, root: Path) -> None:
    """Code the agent runs could swap its workspace for a link; nothing is read or written through it."""
    moved = tmp_path / "moved"
    root.rename(moved)
    root.symlink_to(tmp_path)

    assert _status(lambda: read_file(root, "outside.txt")) == 409
    assert _status(lambda: save_file(root, "planted.txt", b"x", None)) == 409
    assert not (tmp_path / "planted.txt").exists()
    assert _status(lambda: list_folder(root, "")) == 409


def test_reading_below_a_file_answers_400(root: Path) -> None:
    """A path through a file names no folder."""
    assert _status(lambda: read_file(root, "notes.md/inner")) == 400


def test_dot_parts_name_the_same_entry(root: Path) -> None:
    """``.`` parts drop out of the path a read answers with."""
    assert read_file(root, "./src/./main.py").path == "src/main.py"


def test_a_path_that_names_a_credential_answers_404_whatever_it_leads_to(root: Path) -> None:
    """A link named like a credential, or a credential's place reached through a linked folder, is hidden by its name."""
    (root / ".env").unlink()
    (root / ".env").symlink_to("notes.md")
    (root / "settings").symlink_to(".config")
    assert "gh" not in _names(root, "settings")
    assert _status(lambda: delete(root, "settings/gh")) == 404
    (root / "settings").unlink()
    (root / ".config").rename(root / "cfg")
    (root / ".config").symlink_to("cfg")

    assert "gh" not in _names(root, ".config")
    assert ".env" not in _names(root)
    assert _status(lambda: read_file(root, ".env")) == 404
    assert _status(lambda: delete(root, ".env")) == 404
    assert _status(lambda: read_file(root, ".config/gh/hosts.yml")) == 404
    assert _status(lambda: delete(root, ".config/gh")) == 404
    assert (root / "cfg" / "gh" / "hosts.yml").is_file()
