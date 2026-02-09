#!/usr/bin/env python3
"""Generate llms.txt and llms-full.txt from MindRoom docs and zensical.toml nav."""

from __future__ import annotations

import re
import tomllib
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
DOCS_DIR = REPO_ROOT / "docs"
TOML_PATH = REPO_ROOT / "zensical.toml"
OUTPUT_DIR = REPO_ROOT / "skills" / "mindroom-docs" / "references"
SITE_URL = "https://docs.mindroom.chat/"


def _md_path_to_url(md_path: str) -> str:
    """Convert a docs-relative markdown path to a site URL."""
    # index.md -> parent directory, others strip .md and add /
    if md_path == "index.md":
        return SITE_URL
    if md_path.endswith("/index.md"):
        return SITE_URL + md_path.removesuffix("/index.md") + "/"
    return SITE_URL + md_path.removesuffix(".md") + "/"


def _extract_description(md_path: str) -> str:
    """Extract the first meaningful paragraph line from a doc file as description."""
    full_path = DOCS_DIR / md_path
    if not full_path.exists():
        return ""
    text = full_path.read_text()
    # Strip YAML frontmatter (handles \r\n and optional BOM)
    text = re.sub(r"^\ufeff?---\r?\n.*?\r?\n---\r?\n", "", text, flags=re.DOTALL)
    for raw_line in text.strip().splitlines():
        stripped = raw_line.strip()
        # Skip headings, blank lines, admonitions, code fences
        if not stripped or stripped.startswith(("#", ">", "```")):
            continue
        return stripped
    return ""


def _read_full_content(md_path: str) -> str:
    """Read the full markdown content of a doc file, stripping YAML frontmatter."""
    full_path = DOCS_DIR / md_path
    if not full_path.exists():
        return ""
    text = full_path.read_text()
    return re.sub(r"^\ufeff?---\r?\n.*?\r?\n---\r?\n", "", text, flags=re.DOTALL).strip()


def _is_dev_path(md_path: str) -> bool:
    """Check if a doc path is under docs/dev/ (internal, should be skipped)."""
    return md_path.startswith("dev/")


NavItem = dict[str, str | list["NavItem"]]


def _collect_entries(items: list[NavItem]) -> list[tuple[str, str]]:
    """Recursively collect all (title, md_path) entries from a nav list."""
    entries: list[tuple[str, str]] = []
    for item in items:
        for title, value in item.items():
            if isinstance(value, str):
                if not _is_dev_path(value):
                    entries.append((title, value))
            elif isinstance(value, list):
                entries.extend(_collect_entries(value))
    return entries


def _walk_nav(
    nav: list[NavItem],
) -> list[tuple[str, str, list[tuple[str, str]]]]:
    """Walk nav and return blocks in the same order as zensical.toml."""
    blocks: list[tuple[str, str, list[tuple[str, str]]]] = []
    top_entries: list[tuple[str, str]] = []

    def flush_top_entries() -> None:
        if top_entries:
            blocks.append(("", "", [*top_entries]))
            top_entries.clear()

    for item in nav:
        for title, value in item.items():
            if isinstance(value, str):
                if _is_dev_path(value):
                    continue
                top_entries.append((title, value))
            elif isinstance(value, list):
                flush_top_entries()
                entries = _collect_entries(value)
                if entries:
                    blocks.append((title, "", entries))

    flush_top_entries()
    return blocks


def generate_llms_txt(nav: list[NavItem]) -> str:
    """Generate llms.txt content (index with links)."""
    lines = [
        "# MindRoom",
        "",
        "> AI agents that live in Matrix and work everywhere via bridges.",
        "",
    ]

    sections = _walk_nav(nav)
    for section_title, _, entries in sections:
        if section_title:
            lines.append(f"## {section_title}")
            lines.append("")
        for title, md_path in entries:
            url = _md_path_to_url(md_path)
            desc = _extract_description(md_path)
            if desc:
                lines.append(f"- [{title}]({url}): {desc}")
            else:
                lines.append(f"- [{title}]({url})")
        lines.append("")

    return "\n".join(lines)


def generate_llms_full_txt(nav: list[NavItem]) -> str:
    """Generate llms-full.txt content (full inlined markdown)."""
    lines = [
        "# MindRoom",
        "",
        "> AI agents that live in Matrix and work everywhere via bridges.",
        "",
    ]

    sections = _walk_nav(nav)
    for section_title, _, entries in sections:
        if section_title:
            lines.append(f"## {section_title}")
            lines.append("")
        for title, md_path in entries:
            url = _md_path_to_url(md_path)
            lines.append(f"### [{title}]({url})")
            lines.append("")
            content = _read_full_content(md_path)
            if content:
                lines.append(content)
            lines.append("")

    return "\n".join(lines)


def main() -> None:
    """Generate llms.txt and llms-full.txt from zensical.toml nav structure."""
    with TOML_PATH.open("rb") as f:
        config = tomllib.load(f)

    try:
        nav = config["project"]["nav"]
    except KeyError:
        msg = "zensical.toml is missing 'project.nav' key"
        raise SystemExit(msg) from None
    if not isinstance(nav, list):
        msg = f"Expected 'project.nav' to be a list, got {type(nav).__name__}"
        raise SystemExit(msg)

    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

    llms_txt = generate_llms_txt(nav)
    (OUTPUT_DIR / "llms.txt").write_text(llms_txt)
    print(f"Generated {OUTPUT_DIR / 'llms.txt'} ({len(llms_txt)} bytes)")

    llms_full = generate_llms_full_txt(nav)
    (OUTPUT_DIR / "llms-full.txt").write_text(llms_full)
    print(f"Generated {OUTPUT_DIR / 'llms-full.txt'} ({len(llms_full)} bytes)")


if __name__ == "__main__":
    main()
