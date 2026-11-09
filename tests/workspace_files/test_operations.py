"""A workspace's files as a chat client lists, reads and changes them: paths stay inside, credentials stay hidden."""

from __future__ import annotations

import base64
import hashlib
import os
import shutil
import socket
import time
from concurrent.futures import ThreadPoolExecutor
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
    (root / "here").symlink_to(".")

    assert _status(lambda: read_file(root, "out")) == 403
    assert _status(lambda: read_file(root, "out-dir/outside.txt")) == 403
    assert _status(lambda: save_file(root, "out-dir/planted.txt", b"x", None)) == 403
    assert not (root.parent / "planted.txt").exists()
    assert read_file(root, "main").content == "print('hi')\n"
    assert read_file(root, "code/main.py").content == "print('hi')\n"
    targets = {entry.name: entry.target for entry in list_folder(root, "").entries if entry.kind == "link"}
    assert targets == {
        "out": None,
        "out-dir": None,
        "main": "file",
        "code": "folder",
        "nowhere": None,
        "here": "folder",
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


def test_a_file_swapped_for_a_pipe_while_it_is_read_answers_400(root: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Code the agent runs may swap a file for a pipe after the check; the read still refuses it without blocking."""
    (root / "notes.md").write_text("x")
    real_stat = os.stat
    swapped = []

    def stat_then_swap(path: object, *args: object, **kwargs: object) -> os.stat_result:
        status = real_stat(path, *args, **kwargs)  # type: ignore[arg-type]
        if path == "notes.md" and not swapped:
            swapped.append(path)
            (root / "notes.md").unlink()
            os.mkfifo(root / "notes.md")
        return status

    monkeypatch.setattr(operations.os, "stat", stat_then_swap)

    assert _status(lambda: read_file(root, "notes.md")) == 400
    assert swapped


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
        ".cargo/credentials.toml",
        ".config/doctl/config.yaml",
        ".config/glab-cli/config.yml",
        ".terraform.d/credentials.tfrc.json",
        ".vault-token",
        ".cache/huggingface/token",
        "keys/id_rsa",
        "keys/id_ed25519_sk",
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
    [
        "notes.md",
        "src/environment.py",
        "keys/id_utils.py",
        "src/id_token",
        "src/id_mapping/table.csv",
        ".config/app.toml",
        ".cargo/config.toml",
        "deep/.ssh/notes",
        "repo/.git/HEAD",
    ],
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
    shutil.rmtree(root / ".config" / "gh")
    assert _status(lambda: delete(root, ".config")) == 403


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
    (root / "many").mkdir()
    for name in ("a.md", "b.md", "c.md"):
        (root / "many" / name).write_text("x")
    monkeypatch.setattr(operations, "_MAX_FOLDER_ENTRIES", 2)

    listing = list_folder(root, "many")

    assert (len(listing.entries), listing.truncated) == (2, True)


def test_entries_a_listing_leaves_out_count_toward_its_cap(root: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Code the agent runs can plant countless hidden or special files; each still counts, so a listing stays bounded."""
    (root / "planted").mkdir()
    for index in range(10):
        (root / "planted" / f".env.{index}").write_text("x")
        os.mkfifo(root / "planted" / f"pipe{index}")
    monkeypatch.setattr(operations, "_MAX_FOLDER_ENTRIES", 5)
    examined = []
    real_is_hidden = operations.is_hidden
    monkeypatch.setattr(operations, "is_hidden", lambda path: examined.append(path) or real_is_hidden(path))

    listing = list_folder(root, "planted")

    assert listing.truncated
    # Each examined entry is checked by its spelled and its canonical path at most.
    assert len([path for path in examined if len(path.parts) == 2]) <= 2 * 5


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
    monkeypatch.setattr(operations, "_MAX_FILE_BYTES", 4)

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


def test_a_create_only_save_makes_a_new_file_and_the_folders_above(root: Path) -> None:
    """With ``create`` a free name gets a new file with the folders above it and mode 0o644, as a plain save makes one."""
    umask = os.umask(0o077)
    try:
        saved = save_file(root, "docs/guide/new.md", b"# New\n", None, create=True)
    finally:
        os.umask(umask)

    created = root / "docs" / "guide" / "new.md"
    assert created.read_bytes() == b"# New\n"
    assert created.stat().st_mode & 0o777 == 0o644
    assert created.stat().st_nlink == 1
    assert (saved.etag, saved.size, saved.mtime) == (_etag(b"# New\n"), 6, created.stat().st_mtime_ns // 1_000_000)
    assert not list(root.rglob(".mindroom-*"))


def _tree(root: Path) -> dict[str, bytes | str | None]:
    """Every entry below ``root``: a link's target, a file's content, None for a folder."""
    return {
        str(entry.relative_to(root)): (
            str(entry.readlink()) if entry.is_symlink() else entry.read_bytes() if entry.is_file() else None
        )
        for entry in root.rglob("*")
    }


@pytest.mark.parametrize("path", ["notes.md", "src", "notes-link", "dangling-link"])
def test_a_create_only_save_over_anything_already_there_answers_409_and_changes_nothing(root: Path, path: str) -> None:
    """A file, a folder, a link to a file and a link leading nowhere all take the name, and nothing is written."""
    (root / "notes-link").symlink_to("notes.md")
    (root / "dangling-link").symlink_to("missing.md")
    before = _tree(root)

    with pytest.raises(HTTPException) as taken:
        save_file(root, path, b"mine", None, create=True)

    assert (taken.value.status_code, taken.value.detail) == (409, "Something is already there.")
    assert _tree(root) == before


def test_a_create_only_save_never_overwrites_what_appears_after_the_path_is_followed(
    root: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Code the agent runs can make the file between the save's checks and its write: the save then answers 409."""
    leaf = operations._leaf

    def planting(*args: object) -> object:
        found = leaf(*args)
        (root / "race.md").write_text("theirs")
        return found

    monkeypatch.setattr(operations, "_leaf", planting)

    assert _status(lambda: save_file(root, "race.md", b"mine", None, create=True)) == 409
    assert (root / "race.md").read_text() == "theirs"


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
    with pytest.raises(HTTPException, match="already its name"):
        move(root, "notes.md", "notes.md")


def test_delete_removes_a_folder_with_its_content_and_a_link_but_not_its_target(root: Path) -> None:
    """Deleting a link leaves its target, a folder goes with its content, and an absent entry answers 404."""
    (root / "lib" / "deep").mkdir(parents=True)
    (root / "lib" / "deep" / "mod.py").write_text("x")
    (root / "code").symlink_to("lib")

    delete(root, "code")
    assert (root / "lib" / "deep" / "mod.py").is_file()
    delete(root, "lib")

    assert not (root / "lib").exists()
    assert not (root / "code").is_symlink()
    assert _status(lambda: delete(root, "lib")) == 404


@pytest.mark.parametrize("kept", ["src/.env.local", "repo/.git/HEAD", "keys/id_rsa"])
def test_a_folder_holding_git_s_files_or_a_credential_at_any_depth_is_never_deleted(root: Path, kept: str) -> None:
    """Neither the folder holding it nor one holding that folder goes, and the kept file stays."""
    held = root / "outer" / kept
    held.parent.mkdir(parents=True, exist_ok=True)
    held.write_text("x")

    for folder in ("outer", str(Path("outer") / Path(kept).parts[0])):
        assert _status(lambda folder=folder: delete(root, folder)) == 403
    assert held.is_file()


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


def test_two_moves_onto_one_name_never_both_land(root: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Two people move different files to the same new name at once: one lands, the other answers 409."""
    (root / "a.md").write_text("a")
    (root / "b.md").write_text("b")
    real_rename = os.rename

    def slow_rename(*args: object, **kwargs: object) -> None:
        time.sleep(0.2)
        real_rename(*args, **kwargs)

    monkeypatch.setattr(os, "rename", slow_rename)
    with ThreadPoolExecutor(2) as pool:
        statuses = sorted(pool.map(lambda name: _status(lambda: move(root, name, "report.md")), ["a.md", "b.md"]))

    assert statuses == [200, 409]
    # The file that did not move is still there under its own name.
    assert len([name for name in ("a.md", "b.md") if (root / name).exists()]) == 1
    assert (root / "report.md").read_text() in {"a", "b"}


def test_a_change_in_one_workspace_never_waits_for_another(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A long delete in one workspace leaves saves in every other workspace free to land."""
    first, second = tmp_path / "first", tmp_path / "second"
    (first / "build").mkdir(parents=True)
    second.mkdir()
    real_rmtree = shutil.rmtree
    saved = []

    with ThreadPoolExecutor(1) as pool:

        def rmtree_while_saving(*args: object, **kwargs: object) -> None:
            saving = pool.submit(save_file, second, "notes.md", b"x", None)
            saved.append(saving.result(timeout=5))
            real_rmtree(*args, **kwargs)  # type: ignore[arg-type]

        monkeypatch.setattr(operations.shutil, "rmtree", rmtree_while_saving)
        delete(first, "build")

    assert saved
    assert not (first / "build").exists()


def test_a_folder_made_during_a_delete_lands_after_it(root: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Making a folder inside one being deleted waits for the delete, which so never stops half done."""
    (root / "build" / "out").mkdir(parents=True)
    (root / "build" / "out" / "app.js").write_text("x")
    real_rmtree = shutil.rmtree
    makings = []

    with ThreadPoolExecutor(1) as pool:

        def rmtree_while_making(*args: object, **kwargs: object) -> None:
            makings.append(pool.submit(make_folder, root, "build/new"))
            with pytest.raises(TimeoutError):
                makings[0].result(timeout=0.2)
            real_rmtree(*args, **kwargs)  # type: ignore[arg-type]

        monkeypatch.setattr(operations.shutil, "rmtree", rmtree_while_making)
        delete(root, "build")
        makings[0].result(timeout=5)

    assert sorted(path.name for path in (root / "build").iterdir()) == ["new"]


def test_a_delete_during_a_save_lands_after_it(root: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A save that passed its etag check can't bring back a file deleted meanwhile: the delete waits for it."""
    etag = read_file(root, "notes.md").etag
    real_write = operations.write_resolved_file
    deletions = []

    with ThreadPoolExecutor(1) as pool:

        def write_while_deleting(*args: object, **kwargs: object) -> None:
            deletions.append(pool.submit(delete, root, "notes.md"))
            with pytest.raises(TimeoutError):
                deletions[0].result(timeout=0.2)
            real_write(*args, **kwargs)  # type: ignore[arg-type]

        monkeypatch.setattr(operations, "write_resolved_file", write_while_deleting)
        save_file(root, "notes.md", b"edited", etag)
        deletions[0].result(timeout=5)

    assert not (root / "notes.md").exists()


def test_a_name_that_is_not_utf8_is_left_out(root: Path) -> None:
    """A client can't be sent a name that isn't UTF-8, so the folder lists without it instead of failing."""
    descriptor = os.open(bytes(root) + b"/legacy-\xff.txt", os.O_CREAT | os.O_WRONLY, 0o644)
    os.close(descriptor)

    assert _names(root) == [".config", "repo", "src", "notes.md"]
    assert is_hidden(PurePosixPath(os.fsdecode(b"src/legacy-\xff.txt")))
