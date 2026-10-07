"""Hold module attribute access to the Tach interfaces that `from` imports already follow.

`tach check --interfaces` checks `from mindroom.x import name`, but not `x.name` after
`from mindroom import x` or `import mindroom.x as x`, so an interface's `expose` list can
silently fall behind code that reaches the module through an attribute.
"""

from __future__ import annotations

import ast
import re
import tomllib
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
SOURCE_ROOT = REPO_ROOT / "src"


def _module_name(path: Path) -> str:
    parts = list(path.relative_to(SOURCE_ROOT).with_suffix("").parts)
    if parts[-1] == "__init__":
        parts.pop()
    return ".".join(parts)


def _declared_modules(config: dict[str, object]) -> set[str]:
    declared: set[str] = set()
    for module in config.get("modules", []):
        if "path" in module:
            declared.add(module["path"])
        declared.update(module.get("paths", []))
    return declared


def _owning_module(name: str, declared: set[str]) -> str | None:
    """Return the declared Tach module a source module belongs to, as Tach assigns it."""
    candidates = [module for module in declared if name == module or name.startswith(f"{module}.")]
    return max(candidates, key=len) if candidates else None


def _type_checking_imports(tree: ast.Module) -> set[ast.AST]:
    """Return imports under `if TYPE_CHECKING:`, which this repository's Tach setup ignores."""
    return {
        child
        for node in ast.walk(tree)
        if isinstance(node, ast.If) and isinstance(node.test, ast.Name) and node.test.id == "TYPE_CHECKING"
        for child in ast.walk(node)
        if isinstance(child, (ast.Import, ast.ImportFrom))
    }


def _module_aliases(tree: ast.Module, interfaced: set[str]) -> dict[str, str]:
    """Map local names bound to interfaced modules at runtime to those modules."""
    aliases: dict[str, str] = {}
    type_only = _type_checking_imports(tree)
    for node in ast.walk(tree):
        if node in type_only:
            continue
        if isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
            for alias in node.names:
                target = f"{node.module}.{alias.name}"
                if target in interfaced:
                    aliases[alias.asname or alias.name] = target
        elif isinstance(node, ast.Import):
            for alias in node.names:
                if alias.asname and alias.name in interfaced:
                    aliases[alias.asname] = alias.name
    return aliases


def _attribute_violations() -> list[str]:
    config = tomllib.loads((REPO_ROOT / "tach.toml").read_text(encoding="utf-8"))
    declared = _declared_modules(config)
    interfaces: dict[str, list[tuple[list[str], list[str] | None]]] = {}
    for interface in config.get("interfaces", []):
        for module in interface.get("from", []):
            interfaces.setdefault(module, []).append((interface.get("expose", []), interface.get("visibility")))
    # Tach only checks imports between declared modules, so hold attribute access to the same scope.
    interfaced = {module for module in interfaces if module in declared}

    violations: list[str] = []
    for path in sorted((SOURCE_ROOT / "mindroom").rglob("*.py")):
        importer = _owning_module(_module_name(path), declared)
        if importer is None:
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"))
        aliases = _module_aliases(tree, interfaced)
        for node in ast.walk(tree):
            if not (isinstance(node, ast.Attribute) and isinstance(node.value, ast.Name)):
                continue
            target = aliases.get(node.value.id)
            if target is None or target == importer:
                continue
            # An interface with a visibility list exposes its members only to the modules it lists.
            patterns = [
                pattern
                for expose, visibility in interfaces[target]
                if visibility is None or importer in visibility
                for pattern in expose
            ]
            if not any(re.fullmatch(pattern, node.attr) for pattern in patterns):
                violations.append(f"{path.relative_to(REPO_ROOT)}:{node.lineno}: {target}.{node.attr} from {importer}")
    return sorted(set(violations))


def test_module_attribute_access_uses_exposed_interface_members() -> None:
    """Every `module.attr` reach into an interfaced Tach module names a member its interface exposes."""
    assert _attribute_violations() == []


def test_type_checking_imports_are_not_tracked() -> None:
    """Type-only imports stay unchecked, as Tach in this repository ignores them."""
    tree = ast.parse(
        "from typing import TYPE_CHECKING\n"
        "from mindroom import ai\n"
        "if TYPE_CHECKING:\n"
        "    from mindroom import model_loading\n",
    )
    assert _module_aliases(tree, {"mindroom.ai", "mindroom.model_loading"}) == {"ai": "mindroom.ai"}
