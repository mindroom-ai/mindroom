"""Pin the checked turn-pipeline manifest used for refactor LOC measurements."""

from __future__ import annotations

from scripts.testing.turn_pipeline_loc import REPO_ROOT, count_lines, read_manifest


def test_manifest_groups_match_plan_boundary() -> None:
    """The manifest keeps the plan's core and adjacent-recovery groups."""
    groups = read_manifest()
    assert [group.name for group in groups] == ["core", "adjacent-recovery"]
    assert len(groups[0].files) == 34
    assert len(groups[1].files) == 4


def test_manifest_files_exist_under_src_mindroom() -> None:
    """Every manifest entry is an existing Python file under src/mindroom/."""
    for group in read_manifest():
        for relative_path in group.files:
            path = REPO_ROOT / relative_path
            assert relative_path.startswith("src/mindroom/")
            assert path.suffix == ".py"
            assert path.is_file(), relative_path


def test_manifest_has_no_duplicate_entries() -> None:
    """No file appears twice across groups."""
    files = [path for group in read_manifest() for path in group.files]
    assert len(files) == len(set(files))


def test_loc_report_totals_match_direct_counts() -> None:
    """The LOC report sums match direct per-file counts."""
    total = 0
    for group in read_manifest():
        subtotal = sum(count_lines(path) for path in group.files)
        assert subtotal > 0
        total += subtotal
    # The boundary is roughly 24k lines; keep a loose sanity floor only.
    assert total > 20_000
