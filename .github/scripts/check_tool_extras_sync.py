"""Pre-commit hook: verify pyproject.toml optional-dependency groups and registered tools stay in sync.

Checks:
1. Every registered tool has a matching optional-dependency group (and vice-versa).
2. The ``dependencies=[...]`` declared in each tool's decorator/metadata match the
   packages listed in the corresponding pyproject.toml optional-dependency group.
"""

from __future__ import annotations

import ast
import tomllib
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
PYPROJECT = REPO_ROOT / "pyproject.toml"
TOOLS_DIR = REPO_ROOT / "src" / "mindroom" / "tools"

# Groups that are not tool registrations (meta-groups, aggregates, etc.)
IGNORED_GROUPS: set[str] = {"supabase"}

# Tools whose declared dependencies conflict with the project's pinned versions
# and therefore cannot be added to pyproject.toml optional-dependencies.
# brave-search: requires tenacity<9 but project pins tenacity>=9.1.2
KNOWN_CONFLICTS: set[str] = {"brave-search"}


def _normalize_dep(spec: str) -> str:
    """Strip version specifiers, extras, and env markers to get the bare package name."""
    # Remove environment markers  (e.g. "; python_version < '3.12'")
    name = spec.split(";", 1)[0].strip()
    # Remove extras  (e.g. "package[extra]")
    if "[" in name:
        name = name.split("[", 1)[0].strip()
    # Remove version specifiers
    for sep in (">=", "<=", "==", ">", "<", "~=", "!="):
        if sep in name:
            name = name.split(sep, 1)[0].strip()
            break
    return name.lower().replace("_", "-")


def _get_pyproject_data() -> dict:
    """Parse and return the full pyproject.toml data."""
    return tomllib.loads(PYPROJECT.read_text(encoding="utf-8"))


def _get_optional_groups(data: dict) -> set[str]:
    """Return optional-dependency group names from pyproject.toml."""
    return set(data.get("project", {}).get("optional-dependencies", {}).keys())


def _get_optional_dep_packages(data: dict) -> dict[str, set[str]]:
    """Return {group_name: {normalized_package_names}} from pyproject.toml."""
    groups = data.get("project", {}).get("optional-dependencies", {})
    return {name: {_normalize_dep(s) for s in specs} for name, specs in groups.items()}


def _get_base_dep_packages(data: dict) -> set[str]:
    """Return normalized base dependency package names from pyproject.toml."""
    return {_normalize_dep(s) for s in data.get("project", {}).get("dependencies", [])}


def _is_registration_call(func: ast.expr) -> bool:
    """Check if an AST node is a register_tool_with_metadata or ToolMetadata call."""
    if isinstance(func, ast.Name):
        return func.id in ("register_tool_with_metadata", "ToolMetadata")
    if isinstance(func, ast.Attribute):
        return func.attr in ("register_tool_with_metadata", "ToolMetadata")
    return False


def _extract_string_list(node: ast.expr) -> list[str] | None:
    """Extract a list of string constants from an AST List node."""
    if not isinstance(node, ast.List):
        return None
    result = []
    for elt in node.elts:
        if isinstance(elt, ast.Constant) and isinstance(elt.value, str):
            result.append(elt.value)
        else:
            return None  # non-constant element, bail out
    return result


def _extract_tool_registrations() -> dict[str, list[str]]:
    """Scan tools/ .py files and return {tool_name: [declared_dependencies]}.

    Detects both patterns:
      - @register_tool_with_metadata(name="x", dependencies=[...])
      - TOOL_METADATA["x"] = ToolMetadata(name="x", dependencies=[...])
    """
    tools: dict[str, list[str]] = {}
    for py_file in sorted(TOOLS_DIR.glob("*.py")):
        try:
            tree = ast.parse(py_file.read_text(encoding="utf-8"), filename=str(py_file))
        except SyntaxError:
            continue
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call) or not _is_registration_call(node.func):
                continue
            name: str | None = None
            deps: list[str] = []
            for kw in node.keywords:
                if kw.arg == "name" and isinstance(kw.value, ast.Constant) and isinstance(kw.value.value, str):
                    name = kw.value.value
                elif kw.arg == "dependencies":
                    extracted = _extract_string_list(kw.value)
                    if extracted is not None:
                        deps = extracted
            if name is not None:
                tools[name] = deps
    return tools


def _check_groups_sync(tools: dict[str, list[str]], groups: set[str]) -> bool:
    """Check 1: tool names <-> pyproject groups are in sync."""
    tool_names = set(tools)
    missing_groups = sorted(tool_names - groups)
    unused_groups = sorted(groups - tool_names)
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

    return ok


def _check_deps_complete(
    tools: dict[str, list[str]],
    group_packages: dict[str, set[str]],
    base_packages: set[str],
) -> bool:
    """Check 2: declared dependencies are present in pyproject optional group (or base deps)."""
    ok = True
    for tool_name, declared_deps in sorted(tools.items()):
        if not declared_deps:
            continue
        pyproject_pkgs = group_packages.get(tool_name, set())
        for dep in declared_deps:
            normalized = _normalize_dep(dep)
            if normalized in KNOWN_CONFLICTS:
                continue
            if normalized not in pyproject_pkgs and normalized not in base_packages:
                ok = False
                print(
                    f"Tool '{tool_name}' declares dependency '{dep}' "
                    f"not found in pyproject.toml optional-dependencies.{tool_name} or base deps",
                )
    return ok


def main() -> int:
    """Check that registered tools and pyproject.toml optional-dependency groups are in sync."""
    data = _get_pyproject_data()
    groups = _get_optional_groups(data) - IGNORED_GROUPS
    group_packages = _get_optional_dep_packages(data)
    base_packages = _get_base_dep_packages(data)
    tools = _extract_tool_registrations()

    ok = _check_groups_sync(tools, groups)
    ok = _check_deps_complete(tools, group_packages, base_packages) and ok

    if ok:
        n_tools = len(tools)
        n_groups = len(groups)
        print(f"OK: {n_tools} tools and {n_groups} groups in sync, all declared dependencies covered.")

    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
