"""Tests for the coding toolkit's apply_patch, including the Codex apply-patch scenario suite."""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest

from mindroom.custom_tools import coding as coding_module
from mindroom.custom_tools.apply_patch import AddFile, DeleteFile, PatchError, _UpdateChunk, _UpdateFile, parse_patch
from mindroom.custom_tools.coding import CodingTools

_SCENARIOS = Path(__file__).parent / "fixtures" / "apply_patch_scenarios"
# The Codex tool verifies a whole patch before writing; its standalone CLI applies hunk by hunk.
_NOTHING_WRITTEN_ON_FAILURE = {"015_failure_after_partial_success_leaves_changes"}


def _tree(root: Path) -> dict[str, bytes]:
    if not root.exists():
        return {}
    return {str(path.relative_to(root)): path.read_bytes() for path in sorted(root.rglob("*")) if path.is_file()}


@pytest.mark.parametrize("scenario", sorted(path.name for path in _SCENARIOS.iterdir() if path.is_dir()))
def test_codex_scenario(scenario: str, tmp_path: Path) -> None:
    """Every Codex apply-patch scenario leaves the expected tree."""
    source = _SCENARIOS / scenario
    workspace = tmp_path / "workspace"
    if (source / "input").exists():
        shutil.copytree(source / "input", workspace)
    workspace.mkdir(exist_ok=True)
    expected = source / ("input" if scenario in _NOTHING_WRITTEN_ON_FAILURE else "expected")

    CodingTools(base_dir=str(workspace)).apply_patch((source / "patch.txt").read_text())

    assert _tree(workspace) == _tree(expected)


def test_parse_patch_builds_every_hunk_kind() -> None:
    """Add, delete, update, and move hunks parse into typed hunks with context lines marked."""
    hunks = parse_patch(
        "*** Begin Patch\n"
        "*** Add File: new.txt\n+one\n+two\n"
        "*** Delete File: old.txt\n"
        "*** Update File: src.py\n*** Move to: dst.py\n@@ def f():\n a\n-b\n+c\n*** End of File\n"
        "*** End Patch\n",
    )

    assert hunks == [
        AddFile(path="new.txt", contents="one\ntwo\n"),
        DeleteFile(path="old.txt"),
        _UpdateFile(
            path="src.py",
            move_to="dst.py",
            chunks=(
                _UpdateChunk(
                    change_context="def f():",
                    old_lines=("a", "b"),
                    new_lines=("a", "c"),
                    context_line_indices=((0, 0),),
                    is_end_of_file=True,
                ),
            ),
        ),
    ]


@pytest.mark.parametrize(
    ("patch", "message"),
    [
        ("*** Update File: a\n*** End Patch", "invalid patch: The first line of the patch must be '*** Begin Patch'"),
        ("*** Begin Patch\n*** Delete File: a", "invalid patch: The last line of the patch must be '*** End Patch'"),
        (
            "*** Begin Patch\n*** Frobnicate: a\n*** End Patch",
            "invalid hunk at line 2, '*** Frobnicate: a' is not a valid hunk header. Valid hunk headers: "
            "'*** Add File: {path}', '*** Delete File: {path}', '*** Update File: {path}'",
        ),
        (
            "*** Begin Patch\n*** Update File: a\n*** End Patch",
            "invalid hunk at line 2, Update file hunk for path 'a' is empty",
        ),
    ],
)
def test_parse_errors_use_codex_wording(patch: str, message: str) -> None:
    """Parse failures report Codex's messages."""
    with pytest.raises(PatchError) as error:
        parse_patch(patch)

    assert str(error.value) == message


def test_parse_error_reaches_the_model_as_verification_failure(tmp_path: Path) -> None:
    """Like Codex's tool, a patch that does not parse reports a verification failure."""
    result = CodingTools(base_dir=str(tmp_path)).apply_patch("*** Begin Patch\n*** Frobnicate: a\n*** End Patch")

    assert result.startswith("apply_patch verification failed: invalid hunk at line 2, ")


def test_header_and_marker_trailing_whitespace_is_ignored() -> None:
    """Trailing spaces after Move to and End of File markers do not change their meaning."""
    [hunk] = parse_patch(
        "*** Begin Patch\n*** Update File: a.txt\n*** Move to: b.txt  \n@@\n-x\n+y\n*** End of File \n*** End Patch",
    )

    assert isinstance(hunk, _UpdateFile)
    assert hunk.move_to == "b.txt"
    assert hunk.chunks[0].is_end_of_file


def test_blank_line_after_update_header_is_context() -> None:
    """A blank line that opens an update is an empty context line, as in Codex."""
    [hunk] = parse_patch("*** Begin Patch\n*** Update File: a.txt\n\n-x\n+y\n*** End Patch")

    assert isinstance(hunk, _UpdateFile)
    assert hunk.chunks[0].old_lines == ("", "x")


def test_heredoc_wrapped_patch_is_accepted() -> None:
    """A patch wrapped in a shell heredoc, as GPT models sometimes send it, still parses."""
    assert parse_patch("<<'EOF'\n*** Begin Patch\n*** Delete File: a\n*** End Patch\nEOF") == [DeleteFile(path="a")]


def test_success_lists_added_modified_and_deleted_files(tmp_path: Path) -> None:
    """A successful patch reports Codex's summary with added, then modified, then deleted paths."""
    (tmp_path / "keep.txt").write_text("a\n")
    (tmp_path / "gone.txt").write_text("x\n")

    result = CodingTools(base_dir=str(tmp_path)).apply_patch(
        "*** Begin Patch\n*** Delete File: gone.txt\n*** Update File: keep.txt\n@@\n-a\n+b\n"
        "*** Add File: new.txt\n+n\n*** End Patch",
    )

    assert result == "Success. Updated the following files:\nA new.txt\nM keep.txt\nD gone.txt"


def test_failure_writes_nothing(tmp_path: Path) -> None:
    """A hunk that does not apply leaves every file, including earlier hunks' targets, untouched."""
    (tmp_path / "a.txt").write_text("one\n")

    result = CodingTools(base_dir=str(tmp_path)).apply_patch(
        "*** Begin Patch\n*** Add File: b.txt\n+new\n*** Update File: a.txt\n@@\n-missing\n+x\n*** End Patch",
    )

    assert result == "apply_patch verification failed: Failed to find expected lines in a.txt:\nmissing"
    assert _tree(tmp_path) == {"a.txt": b"one\n"}


@pytest.mark.parametrize(
    "hunks",
    [
        "*** Add File: a.txt\n+one\n*** Update File: a.txt\n@@\n-one\n+two\n",
        "*** Update File: a.txt\n@@\n-a\n+b\n*** Update File: a.txt\n@@\n-b\n+c\n",
        "*** Delete File: current.txt\n*** Add File: current.txt\n+new\n",
        "*** Add File: new/item.txt\n+x\n*** Delete File: new/item.txt\n*** Add File: new\n+y\n",
        "*** Delete File: link\n*** Update File: sub/../link/config.txt\n@@\n-keep\n+changed\n",
        "*** Update File: a.txt\n*** Move to: moved.txt\n@@\n-a\n+b\n*** Add File: moved.txt\n+x\n",
    ],
    ids=[
        "add-then-update",
        "update-twice",
        "replace-link",
        "child-then-file",
        "dotdot-through-deleted-link",
        "move-target",
    ],
)
def test_hunks_touching_one_path_are_refused(tmp_path: Path, hunks: str) -> None:
    """Every hunk applies to the files as they were before the patch, so two hunks may not touch one path."""
    (tmp_path / "a.txt").write_text("a\n")
    (tmp_path / "original.txt").write_text("keep\n")
    (tmp_path / "current.txt").symlink_to("original.txt")
    (tmp_path / "sub").mkdir()
    (tmp_path / "real").mkdir()
    (tmp_path / "real" / "config.txt").write_text("keep\n")
    (tmp_path / "link").symlink_to("real")
    before = _tree(tmp_path)

    result = CodingTools(base_dir=str(tmp_path)).apply_patch(f"*** Begin Patch\n{hunks}*** End Patch")

    assert result.startswith("apply_patch verification failed: invalid patch: ")
    assert "touch the same file or directory" in result
    assert _tree(tmp_path) == before
    assert (tmp_path / "current.txt").is_symlink()
    assert (tmp_path / "link").is_symlink()


def test_deleting_a_link_and_updating_its_target_both_apply(tmp_path: Path) -> None:
    """A patch may remove a link and change the file it pointed to, since the two hunks touch different paths."""
    (tmp_path / "real").mkdir()
    (tmp_path / "real" / "config.txt").write_text("keep\n")
    (tmp_path / "current").symlink_to("real")

    result = CodingTools(base_dir=str(tmp_path)).apply_patch(
        "*** Begin Patch\n*** Delete File: current\n*** Update File: real/config.txt\n@@\n-keep\n+changed\n"
        "*** End Patch",
    )

    assert result == "Success. Updated the following files:\nM real/config.txt\nD current"
    assert not (tmp_path / "current").is_symlink()
    assert (tmp_path / "real" / "config.txt").read_text() == "changed\n"


def test_moving_a_link_onto_its_target_removes_the_link(tmp_path: Path) -> None:
    """Moving a link onto the file it points to writes that file and removes the link, like any move."""
    (tmp_path / "real.txt").write_text("x\n")
    (tmp_path / "link.txt").symlink_to("real.txt")

    result = CodingTools(base_dir=str(tmp_path)).apply_patch(
        "*** Begin Patch\n*** Update File: link.txt\n*** Move to: real.txt\n@@\n-x\n+y\n*** End Patch",
    )

    assert result == "Success. Updated the following files:\nM real.txt"
    assert (tmp_path / "real.txt").read_text() == "y\n"
    assert not (tmp_path / "link.txt").is_symlink()


def test_write_failure_names_the_files_already_changed(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A write that fails after verification reports which files the patch already changed."""
    (tmp_path / "a.txt").write_text("one\n")
    (tmp_path / "b.txt").write_text("two\n")
    write = coding_module.write_resolved_file

    def refuse_b(base_dir: Path, resolved: Path, payload: bytes) -> None:
        if resolved.name == "b.txt":
            raise PermissionError(13, "Permission denied")
        write(base_dir, resolved, payload)

    monkeypatch.setattr(coding_module, "write_resolved_file", refuse_b)

    result = CodingTools(base_dir=str(tmp_path)).apply_patch(
        "*** Begin Patch\n*** Update File: a.txt\n@@\n-one\n+ONE\n*** Update File: b.txt\n@@\n-two\n+TWO\n"
        "*** End Patch",
    )

    assert result == (
        "Error applying patch to b.txt: [Errno 13] Permission denied\n"
        "The patch already changed these files before the error:\na.txt"
    )
    assert (tmp_path / "a.txt").read_text() == "ONE\n"


def test_path_outside_workspace_rejected(tmp_path: Path) -> None:
    """In workspace mode a patch cannot touch files outside the workspace."""
    workspace = tmp_path / "workspace"
    workspace.mkdir()

    result = CodingTools(base_dir=str(workspace)).apply_patch(
        "*** Begin Patch\n*** Add File: ../x.txt\n+x\n*** End Patch",
    )

    assert result.startswith("apply_patch verification failed: ")
    assert not (tmp_path / "x.txt").exists()


def test_git_metadata_path_rejected(tmp_path: Path) -> None:
    """A patch cannot write inside a .git directory."""
    (tmp_path / ".git").mkdir()

    result = CodingTools(base_dir=str(tmp_path)).apply_patch(
        "*** Begin Patch\n*** Add File: .git/hooks/pre-commit\n+x\n*** End Patch",
    )

    assert "inside Git metadata" in result
    assert not (tmp_path / ".git" / "hooks").exists()


def test_move_to_renames(tmp_path: Path) -> None:
    """Move to writes the updated file at its new path and removes the old one."""
    (tmp_path / "old.py").write_text("x = 1\n")

    result = CodingTools(base_dir=str(tmp_path)).apply_patch(
        "*** Begin Patch\n*** Update File: old.py\n*** Move to: pkg/new.py\n@@\n-x = 1\n+x = 2\n*** End Patch",
    )

    assert result == "Success. Updated the following files:\nM pkg/new.py"
    assert _tree(tmp_path) == {"pkg/new.py": b"x = 2\n"}


def test_crlf_preserved_and_final_newline_added(tmp_path: Path) -> None:
    """Updates keep CRLF endings and, like Codex, end the file with a newline."""
    (tmp_path / "win.txt").write_bytes(b"a\r\nb\r\nc")

    CodingTools(base_dir=str(tmp_path)).apply_patch(
        "*** Begin Patch\n*** Update File: win.txt\n@@\n a\n-b\n+B\n*** End Patch",
    )

    assert (tmp_path / "win.txt").read_bytes() == b"a\r\nB\r\nc\r\n"


def test_typographic_punctuation_matches_ascii_patch_lines(tmp_path: Path) -> None:
    """Context written with ASCII dashes, quotes, and spaces still finds typographic source lines."""
    (tmp_path / "doc.txt").write_text(f"say {chr(0x201C)}hi{chr(0x201D)} {chr(0x2013)} now{chr(0xA0)}ok\n")

    result = CodingTools(base_dir=str(tmp_path)).apply_patch(
        '*** Begin Patch\n*** Update File: doc.txt\n@@\n-say "hi" - now ok\n+bye\n*** End Patch',
    )

    assert result.startswith("Success.")
    assert (tmp_path / "doc.txt").read_text() == "bye\n"


@pytest.mark.parametrize(
    ("setup", "hunk"),
    [
        ("dir", "*** Add File: pkg\n+x\n"),
        ("file", "*** Add File: blocker/inner.txt\n+x\n"),
        ("dir", "*** Update File: b.txt\n*** Move to: pkg\n@@\n-b\n+B\n"),
        ("none", "*** Add File: new/inner.txt\n+x\n*** Add File: new\n+y\n"),
    ],
    ids=["add-over-directory", "parent-is-a-file", "move-onto-directory", "file-over-planned-directory"],
)
def test_predictable_write_failures_change_nothing(tmp_path: Path, setup: str, hunk: str) -> None:
    """A patch whose later write must fail is refused before an earlier hunk changes anything."""
    (tmp_path / "a.txt").write_text("a\n")
    (tmp_path / "b.txt").write_text("b\n")
    if setup == "dir":
        (tmp_path / "pkg").mkdir()
        (tmp_path / "pkg" / "keep.txt").write_text("k\n")
    elif setup == "file":
        (tmp_path / "blocker").write_text("f\n")
    before = _tree(tmp_path)

    result = CodingTools(base_dir=str(tmp_path)).apply_patch(
        f"*** Begin Patch\n*** Update File: a.txt\n@@\n-a\n+A\n{hunk}*** End Patch",
    )

    assert result.startswith("apply_patch verification failed: ")
    assert _tree(tmp_path) == before


def test_delete_binary_file(tmp_path: Path) -> None:
    """Deleting needs no text, so a binary file deletes."""
    (tmp_path / "logo.png").write_bytes(b"\x89PNG\r\n\x1a\n\xff\xfe")

    result = CodingTools(base_dir=str(tmp_path)).apply_patch(
        "*** Begin Patch\n*** Delete File: logo.png\n*** End Patch",
    )

    assert result == "Success. Updated the following files:\nD logo.png"
    assert not (tmp_path / "logo.png").exists()


def test_delete_through_symlink_removes_the_link(tmp_path: Path) -> None:
    """Deleting a link inside the workspace removes the link and keeps its target, as Codex does."""
    (tmp_path / "logs").mkdir()
    (tmp_path / "logs" / "real.log").write_text("keep\n")
    (tmp_path / "current.log").symlink_to("logs/real.log")

    result = CodingTools(base_dir=str(tmp_path)).apply_patch(
        "*** Begin Patch\n*** Delete File: current.log\n*** End Patch",
    )

    assert result == "Success. Updated the following files:\nD current.log"
    assert not (tmp_path / "current.log").is_symlink()
    assert (tmp_path / "logs" / "real.log").read_text() == "keep\n"


def test_moving_a_symlink_removes_the_link_and_keeps_its_target(tmp_path: Path) -> None:
    """A move reads through a link, writes the new file, and removes the link itself, as Codex does."""
    (tmp_path / "a.txt").write_text("x\n")
    (tmp_path / "link.txt").symlink_to("a.txt")

    result = CodingTools(base_dir=str(tmp_path)).apply_patch(
        "*** Begin Patch\n*** Update File: link.txt\n*** Move to: b.txt\n@@\n-x\n+y\n*** End Patch",
    )

    assert result == "Success. Updated the following files:\nM b.txt"
    assert (tmp_path / "b.txt").read_text() == "y\n"
    assert (tmp_path / "a.txt").read_text() == "x\n"
    assert not (tmp_path / "link.txt").is_symlink()


def test_writing_below_a_link_deleted_earlier_is_refused(tmp_path: Path) -> None:
    """A patch that deletes a directory link and then writes below that path changes nothing."""
    (tmp_path / "real").mkdir()
    (tmp_path / "real" / "config.txt").write_text("keep\n")
    (tmp_path / "link").symlink_to("real")
    before = _tree(tmp_path)

    result = CodingTools(base_dir=str(tmp_path)).apply_patch(
        "*** Begin Patch\n*** Delete File: link\n*** Add File: link/config.txt\n+new\n*** End Patch",
    )

    assert result.startswith("apply_patch verification failed: ")
    assert _tree(tmp_path) == before
    assert (tmp_path / "link").is_symlink()


@pytest.mark.parametrize("header", ["@@ ", "@@\t"])
def test_hunk_header_trailing_whitespace_is_ignored(tmp_path: Path, header: str) -> None:
    """A bare @@ header followed by whitespace still opens a hunk, as in Codex."""
    (tmp_path / "a.txt").write_text("a\nb\nc\n")

    result = CodingTools(base_dir=str(tmp_path)).apply_patch(
        f"*** Begin Patch\n*** Update File: a.txt\n{header}\n a\n-b\n+B\n*** End Patch",
    )

    assert result.startswith("Success.")
    assert (tmp_path / "a.txt").read_text() == "a\nB\nc\n"


def test_blank_line_after_end_of_file_is_ignored() -> None:
    """A blank line between an end-of-file hunk and the end of the patch is skipped, as in Codex."""
    [hunk] = parse_patch("*** Begin Patch\n*** Update File: a.txt\n@@\n-c\n+C\n*** End of File\n\n*** End Patch")

    assert isinstance(hunk, _UpdateFile)
    assert [chunk.is_end_of_file for chunk in hunk.chunks] == [True]


@pytest.mark.parametrize(
    "second",
    [
        "*** Delete File: link/config.txt\n",
        "*** Update File: link/config.txt\n*** Move to: moved.txt\n@@\n-keep\n+kept\n",
        "*** Update File: link/config.txt\n@@\n-keep\n+kept\n",
    ],
    ids=["delete", "move", "update"],
)
def test_paths_through_a_link_deleted_earlier_are_refused(tmp_path: Path, second: str) -> None:
    """No hunk reaches through a link the same patch deletes earlier, so the old target stays untouched."""
    (tmp_path / "real").mkdir()
    (tmp_path / "real" / "config.txt").write_text("keep\n")
    (tmp_path / "link").symlink_to("real")
    before = _tree(tmp_path)

    result = CodingTools(base_dir=str(tmp_path)).apply_patch(
        f"*** Begin Patch\n*** Delete File: link\n{second}*** End Patch",
    )

    assert result.startswith("apply_patch verification failed: ")
    assert _tree(tmp_path) == before
    assert (tmp_path / "link").is_symlink()
