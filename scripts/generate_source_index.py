#!/usr/bin/env python3
"""Generate source-index.txt and source-map.md from MindRoom Python source files."""

from __future__ import annotations

import ast
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
SRC_DIR = REPO_ROOT / "src" / "mindroom"
OUTPUT_DIR = REPO_ROOT / "skills" / "mindroom-self-debug" / "references"

# Directories and patterns to skip
SKIP_DIRS = {"__pycache__"}
SKIP_SUFFIXES = {".pyc", ".pyo"}


def _collect_python_files(root: Path) -> list[Path]:
    """Collect all .py files under root, skipping __pycache__ and compiled files."""
    files: list[Path] = []
    for path in sorted(root.rglob("*.py")):
        if any(part in SKIP_DIRS for part in path.parts):
            continue
        if path.suffix in SKIP_SUFFIXES:
            continue
        files.append(path)
    return files


def _extract_module_docstring(path: Path) -> str:
    """Extract the module-level docstring from a Python file."""
    try:
        source = path.read_text(encoding="utf-8")
        tree = ast.parse(source)
        docstring = ast.get_docstring(tree)
        if docstring:
            # Return just the first line of the docstring
            return docstring.strip().split("\n")[0]
    except (SyntaxError, UnicodeDecodeError, OSError):
        pass
    return ""


def generate_source_index(files: list[Path]) -> str:
    """Generate source-index.txt: full concatenation of all source files with headers."""
    parts: list[str] = []
    parts.append("# MindRoom Source Code Index")
    parts.append(f"# Generated from src/mindroom/ ({len(files)} files)")
    parts.append("")

    for path in files:
        relative = path.relative_to(REPO_ROOT)
        parts.append("=" * 80)
        parts.append(f"# FILE: {relative}")
        parts.append("=" * 80)
        parts.append("")
        try:
            content = path.read_text(encoding="utf-8")
            parts.append(content)
        except (OSError, UnicodeDecodeError) as exc:
            parts.append(f"# ERROR reading file: {exc}")
        parts.append("")

    return "\n".join(parts)


def generate_source_map(files: list[Path]) -> str:
    """Generate source-map.md: table of contents with one-line descriptions."""
    lines: list[str] = []
    lines.append("# MindRoom Source Map")
    lines.append("")
    lines.append("Table of contents for all Python source files in `src/mindroom/`.")
    lines.append("")
    lines.append("| File | Description |")
    lines.append("|------|-------------|")

    for path in files:
        relative = path.relative_to(REPO_ROOT)
        description = _extract_module_docstring(path)
        if not description:
            description = "-"
        # Escape pipe characters in descriptions to avoid breaking markdown tables
        safe_description = description.replace("|", "\\|")
        lines.append(f"| `{relative}` | {safe_description} |")

    lines.append("")
    lines.append(f"**Total: {len(files)} files**")
    lines.append("")
    return "\n".join(lines)


def main() -> None:
    """Generate source-index.txt and source-map.md from MindRoom source files."""
    if not SRC_DIR.exists():
        print(f"Error: source directory not found: {SRC_DIR}", file=sys.stderr)
        sys.exit(1)

    files = _collect_python_files(SRC_DIR)
    if not files:
        print(f"Error: no Python files found in {SRC_DIR}", file=sys.stderr)
        sys.exit(1)

    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

    source_index = generate_source_index(files)
    index_path = OUTPUT_DIR / "source-index.txt"
    index_path.write_text(source_index, encoding="utf-8")
    print(f"Generated {index_path} ({len(source_index):,} bytes, {len(files)} files)")

    source_map = generate_source_map(files)
    map_path = OUTPUT_DIR / "source-map.md"
    map_path.write_text(source_map, encoding="utf-8")
    print(f"Generated {map_path} ({len(source_map):,} bytes)")


if __name__ == "__main__":
    main()
