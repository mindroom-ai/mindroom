"""Tests for documentation metadata that the static site build consumes."""

import re
import tomllib
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
DOCS_ROOT = REPO_ROOT / "docs"


def test_lucide_icon_metadata_uses_top_level_frontmatter() -> None:
    """Zensical renders misplaced frontmatter as literal page content."""
    misplaced_icon_pages = []
    for path in sorted(DOCS_ROOT.rglob("*.md")):
        lines = path.read_text(encoding="utf-8").splitlines()
        if any(line.startswith("icon: lucide/") for line in lines[:10]) and lines[:1] != ["---"]:
            misplaced_icon_pages.append(path.relative_to(DOCS_ROOT.parent).as_posix())

    assert misplaced_icon_pages == []


def _page_icon(page: str) -> str | None:
    text = (DOCS_ROOT / page).read_text(encoding="utf-8")
    front = re.match(r"---\n(.*?)\n---\n", text, re.DOTALL)
    icon = re.search(r"^icon:\s*(\S+)", front.group(1), re.MULTILINE) if front else None
    return icon.group(1) if icon else None


def test_every_sidebar_item_has_an_icon() -> None:
    """Every sidebar page has an icon, and every section without an index page has one in extra.nav_icons."""
    project = tomllib.loads((REPO_ROOT / "zensical.toml").read_text(encoding="utf-8"))["project"]
    section_icons = project["extra"]["nav_icons"]
    missing = []

    def walk(items: list[dict[str, object]]) -> None:
        for item in items:
            for title, value in item.items():
                if isinstance(value, list):
                    first = next(iter(value[0].values()))
                    is_index = isinstance(first, str) and Path(first).name == "index.md"
                    if not is_index and title not in section_icons:
                        missing.append(f"section {title}")
                    walk(value)
                elif _page_icon(str(value)) is None:
                    missing.append(str(value))

    walk(project["nav"])
    assert missing == []
