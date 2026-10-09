"""Tests for the coding toolkit's apply_patch, including the Codex apply-patch scenario suite."""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest

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
        ("*** Update File: a\n*** End Patch", "Invalid patch: The first line of the patch must be '*** Begin Patch'"),
        ("*** Begin Patch\n*** Delete File: a", "Invalid patch: The last line of the patch must be '*** End Patch'"),
        (
            "*** Begin Patch\n*** Frobnicate: a\n*** End Patch",
            "Invalid patch hunk on line 2: '*** Frobnicate: a' is not a valid hunk header. Valid hunk headers: "
            "'*** Add File: {path}', '*** Delete File: {path}', '*** Update File: {path}'",
        ),
        (
            "*** Begin Patch\n*** Update File: a\n*** End Patch",
            "Invalid patch hunk on line 2: Update file hunk for path 'a' is empty",
        ),
    ],
)
def test_parse_errors_use_codex_wording(patch: str, message: str) -> None:
    """Parse failures report Codex's messages."""
    with pytest.raises(PatchError) as error:
        parse_patch(patch)

    assert str(error.value) == message


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


def test_later_hunks_see_earlier_hunks(tmp_path: Path) -> None:
    """Hunks apply in order, so an update can follow an add of the same file."""
    result = CodingTools(base_dir=str(tmp_path)).apply_patch(
        "*** Begin Patch\n*** Add File: a.txt\n+one\n*** Update File: a.txt\n@@\n-one\n+two\n*** End Patch",
    )

    assert result.startswith("Success.")
    assert (tmp_path / "a.txt").read_text() == "two\n"


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
