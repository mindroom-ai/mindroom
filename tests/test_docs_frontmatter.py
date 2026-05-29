"""Tests for documentation metadata that the static site build consumes."""

from pathlib import Path

DOCS_ROOT = Path(__file__).resolve().parents[1] / "docs"


def test_lucide_icon_metadata_uses_top_level_frontmatter() -> None:
    """Zensical renders misplaced frontmatter as literal page content."""
    misplaced_icon_pages = []
    for path in sorted(DOCS_ROOT.rglob("*.md")):
        lines = path.read_text(encoding="utf-8").splitlines()
        if any(line.startswith("icon: lucide/") for line in lines[:10]) and lines[:1] != ["---"]:
            misplaced_icon_pages.append(path.relative_to(DOCS_ROOT.parent).as_posix())

    assert misplaced_icon_pages == []
