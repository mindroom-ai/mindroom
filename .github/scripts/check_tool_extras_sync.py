"""Pre-commit hook: verify pyproject.toml optional-dependency groups and registered tools stay in sync."""

from __future__ import annotations

import ast
import tomllib
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
PYPROJECT = REPO_ROOT / "pyproject.toml"
TOOLS_DIR = REPO_ROOT / "src" / "mindroom" / "tools"

# Groups that are not tool registrations (meta-groups, aggregates, etc.)
IGNORED_GROUPS: set[str] = set()


def _get_optional_groups() -> set[str]:
    """Return optional-dependency group names from pyproject.toml."""
    data = tomllib.loads(PYPROJECT.read_text(encoding="utf-8"))
    return set(data.get("project", {}).get("optional-dependencies", {}).keys())


def _extract_registered_tool_names() -> set[str]:
    """Scan tools/ .py files for tool registrations.

    Detects both patterns:
      - @register_tool_with_metadata(name="x", ...)
      - TOOL_METADATA["x"] = ToolMetadata(name="x", ...)
    """
    names: set[str] = set()
    for py_file in sorted(TOOLS_DIR.glob("*.py")):
        try:
            tree = ast.parse(py_file.read_text(encoding="utf-8"), filename=str(py_file))
        except SyntaxError:
            continue
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            func = node.func
            # Pattern 1: register_tool_with_metadata(name=...)
            is_register = (isinstance(func, ast.Name) and func.id == "register_tool_with_metadata") or (
                isinstance(func, ast.Attribute) and func.attr == "register_tool_with_metadata"
            )
            # Pattern 2: ToolMetadata(name=...)
            is_metadata = (isinstance(func, ast.Name) and func.id == "ToolMetadata") or (
                isinstance(func, ast.Attribute) and func.attr == "ToolMetadata"
            )
            if not (is_register or is_metadata):
                continue
            for kw in node.keywords:
                if kw.arg == "name" and isinstance(kw.value, ast.Constant) and isinstance(kw.value.value, str):
                    names.add(kw.value.value)
    return names


def main() -> int:
    """Check that registered tools and pyproject.toml optional-dependency groups are in sync."""
    groups = _get_optional_groups() - IGNORED_GROUPS
    tools = _extract_registered_tool_names()

    missing_groups = sorted(tools - groups)
    unused_groups = sorted(groups - tools)

    ok = True

    if missing_groups:
        ok = False
        print("Tools registered but missing optional-dependency group in pyproject.toml:")
        for name in missing_groups:
            print(f"  - {name}")

    if unused_groups:
        ok = False
        print("Optional-dependency groups in pyproject.toml with no matching registered tool:")
        for name in unused_groups:
            print(f"  - {name}")

    if ok:
        print(f"OK: {len(tools)} tools and {len(groups)} optional-dependency groups are in sync.")

    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
