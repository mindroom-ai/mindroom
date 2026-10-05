"""The staged copy a curation pass edits: refusals, measurement, and compare-and-swap publication."""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

from mindroom.config.prompt_curation import PromptCurationConfig
from mindroom.prompt_curation.staging import StagedWorkspace

if TYPE_CHECKING:
    from pathlib import Path

SETTINGS = PromptCurationConfig(files=["MEMORY.md", "USER.md"])
MEMORY = "# Memory\n- Prefers terse replies.\n- Project Atlas history: kickoff, three reviews, launch.\n"


def _workspace(root: Path) -> StagedWorkspace:
    (root / "MEMORY.md").write_text(MEMORY, encoding="utf-8")
    (root / "USER.md").write_text("Name: Sam\n", encoding="utf-8")
    (root / "SOUL.md").write_text("Be kind.\n", encoding="utf-8")
    (root / "memory").mkdir()
    (root / "memory" / "2026-10-01.md").write_text("Daily note.\n", encoding="utf-8")
    return StagedWorkspace.load(root, SETTINGS)


def _snapshot(root: Path) -> dict[str, bytes]:
    return {str(path.relative_to(root)): path.read_bytes() for path in sorted(root.rglob("*")) if path.is_file()}


def test_edits_stay_staged_until_published(tmp_path: Path) -> None:
    """Edits and appends change nothing on disk until the pass is published."""
    staged = _workspace(tmp_path)
    before = _snapshot(tmp_path)

    staged.append("memory/projects.md", "Project Atlas history: kickoff, three reviews, launch.")
    staged.edit(
        "MEMORY.md",
        "- Project Atlas history: kickoff, three reviews, launch.\n",
        "- Atlas: see memory/projects.md\n",
    )

    assert _snapshot(tmp_path) == before
    assert staged.read("MEMORY.md") == "# Memory\n- Prefers terse replies.\n- Atlas: see memory/projects.md\n"
    assert staged.changed_paths() == frozenset({"MEMORY.md", "memory/projects.md"})
    assert staged.publish() == "published"
    assert (
        tmp_path / "memory" / "projects.md"
    ).read_text() == "Project Atlas history: kickoff, three reviews, launch.\n"
    assert (tmp_path / "MEMORY.md").read_text() == staged.read("MEMORY.md")


def test_measurement_counts_curated_files_and_all_memory_content(tmp_path: Path) -> None:
    """Measurement covers each curatable file and all memory content, with no changed paths at load."""
    staged = _workspace(tmp_path)

    measurement = staged.measurement()

    assert measurement.curated == {"MEMORY.md": len(MEMORY) // 4, "USER.md": len("Name: Sam\n") // 4}
    assert measurement.total_memory_tokens == len(MEMORY) // 4 + len("Name: Sam\n") // 4 + len("Daily note.\n") // 4
    assert measurement.changed_paths == frozenset()


@pytest.mark.parametrize(
    ("operation", "path", "message"),
    [
        ("edit", "SOUL.md", "SOUL.md is protected"),
        ("edit", "NOTES.md", "only edit the curatable files"),
        ("edit", "memory/2026-10-01.md", "only edit the curatable files"),
        ("append", "MEMORY.md", "only append to Markdown files under memory/"),
        ("append", "memory/../SOUL.md", "only append to Markdown files under memory/"),
        ("append", "memory/notes.txt", "only append to Markdown files under memory/"),
        ("append", "memory/notes.MD", "only append to Markdown files under memory/"),
        ("append", "memory/.git/x.md", "only append to Markdown files under memory/"),
        ("read", "../outside.md", "can only read the curatable files and memory/"),
    ],
)
def test_paths_outside_the_pass_are_refused(tmp_path: Path, operation: str, path: str, message: str) -> None:
    """The pass may only edit curatable files, append under memory/, and read those."""
    staged = _workspace(tmp_path)
    operations = {
        "edit": lambda: staged.edit(path, "x", "y"),
        "append": lambda: staged.append(path, "text"),
        "read": lambda: staged.read(path),
    }

    with pytest.raises(ValueError, match=message):
        operations[operation]()


def test_edit_requires_one_exact_match(tmp_path: Path) -> None:
    """An edit needs exactly one exact match of its old text."""
    staged = _workspace(tmp_path)

    with pytest.raises(ValueError, match="not found"):
        staged.edit("MEMORY.md", "missing text", "")
    with pytest.raises(ValueError, match="matches 2 places"):
        staged.edit("MEMORY.md", "- ", "* ")


def test_an_append_during_the_pass_is_kept_on_publication(tmp_path: Path) -> None:
    """Lines a live turn appended during the pass survive publication."""
    staged = _workspace(tmp_path)
    staged.edit("MEMORY.md", "- Project Atlas history: kickoff, three reviews, launch.\n", "")
    with (tmp_path / "MEMORY.md").open("a", encoding="utf-8") as memory_file:
        memory_file.write("- New fact from a live turn.\n")
    with (tmp_path / "memory" / "2026-10-01.md").open("a", encoding="utf-8") as daily_file:
        daily_file.write("Live daily note.\n")
    staged.append("memory/2026-10-01.md", "Moved note.")

    assert staged.publish() == "published"
    assert (tmp_path / "MEMORY.md").read_text() == "# Memory\n- Prefers terse replies.\n- New fact from a live turn.\n"
    assert (tmp_path / "memory" / "2026-10-01.md").read_text() == "Daily note.\nLive daily note.\nMoved note.\n"


def test_a_rewrite_during_the_pass_aborts_publication(tmp_path: Path) -> None:
    """A curatable file rewritten during the pass aborts publication without writing anything."""
    staged = _workspace(tmp_path)
    staged.append("memory/projects.md", "Moved.")
    staged.edit("MEMORY.md", "- Prefers terse replies.\n", "")
    (tmp_path / "MEMORY.md").write_text("# Memory\nRewritten by a live turn.\n", encoding="utf-8")
    before = _snapshot(tmp_path)

    assert staged.publish() == "conflict"
    assert _snapshot(tmp_path) == before


def test_a_curatable_file_deleted_during_the_pass_aborts_publication(tmp_path: Path) -> None:
    """A curatable file removed while the pass runs is not recreated from the staged copy."""
    staged = _workspace(tmp_path)
    staged.append("memory/projects.md", "Moved.")
    staged.edit("MEMORY.md", "- Prefers terse replies.\n", "")
    (tmp_path / "MEMORY.md").unlink()
    before = _snapshot(tmp_path)

    assert staged.publish() == "conflict"
    assert _snapshot(tmp_path) == before


def test_a_curatable_file_that_cannot_be_rewritten_stops_the_pass(tmp_path: Path) -> None:
    """A curatable file that is not valid UTF-8 stops the pass before it starts."""
    (tmp_path / "MEMORY.md").write_bytes(b"caf\xe9\n")

    with pytest.raises(ValueError, match=r"MEMORY\.md"):
        StagedWorkspace.load(tmp_path, SETTINGS)


def test_a_planted_link_is_refused(tmp_path: Path) -> None:
    """A symlinked curatable file is refused instead of followed."""
    outside = tmp_path / "outside.md"
    outside.write_text("secret\n", encoding="utf-8")
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / "MEMORY.md").write_text(MEMORY, encoding="utf-8")
    (workspace / "USER.md").symlink_to(outside)

    with pytest.raises((OSError, ValueError)):
        StagedWorkspace.load(workspace, SETTINGS)


def test_missing_curatable_files_are_skipped(tmp_path: Path) -> None:
    """Absent curatable files are not measured and cannot be edited."""
    (tmp_path / "MEMORY.md").write_text(MEMORY, encoding="utf-8")

    staged = StagedWorkspace.load(tmp_path, SETTINGS)

    assert set(staged.measurement().curated) == {"MEMORY.md"}
    with pytest.raises(ValueError, match=r"USER\.md does not exist"):
        staged.edit("USER.md", "x", "y")
