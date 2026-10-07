"""Hold module attribute access to the Tach interfaces that `from` imports already follow.

`tach check --interfaces` checks `from mindroom.x import name`, but not `x.name` after
`from mindroom import x` or `import mindroom.x as x`, so an interface's `expose` list can
silently fall behind code that reaches the module through an attribute.
"""

from __future__ import annotations

import ast
import re
import textwrap
import tomllib
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]

# One interface of a module: (expose patterns, visibility or None, exclusive).
_Interface = tuple[list[str], list[str] | None, bool]


def _module_name(path: Path, source_root: Path) -> str:
    parts = list(path.relative_to(source_root).with_suffix("").parts)
    if parts[-1] == "__init__":
        parts.pop()
    return ".".join(parts)


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


def _module_aliases(tree: ast.Module, package: str, interfaced: set[str]) -> dict[str, str]:
    """Map local names bound to interfaced modules at runtime to those modules."""
    aliases: dict[str, str] = {}
    type_only = _type_checking_imports(tree)
    for node in ast.walk(tree):
        if node in type_only:
            continue
        if isinstance(node, ast.ImportFrom):
            origin = node.module
            if node.level:
                package_parts = package.split(".")
                origin = ".".join([*package_parts[: len(package_parts) - node.level + 1], *filter(None, [node.module])])
            for alias in node.names:
                target = f"{origin}.{alias.name}"
                if target in interfaced:
                    aliases[alias.asname or alias.name] = target
        elif isinstance(node, ast.Import):
            for alias in node.names:
                if alias.asname and alias.name in interfaced:
                    aliases[alias.asname] = alias.name
    return aliases


def _visible_patterns(interfaces: list[_Interface], importer: str) -> list[str] | None:
    """Return the expose patterns Tach applies to one importer, or None when it checks nothing."""
    visible = [interface for interface in interfaces if interface[1] is None or importer in interface[1]]
    applied = [interface for interface in visible if interface[2]] or visible
    if not applied:
        return None
    return [pattern for expose, _, _ in applied for pattern in expose]


def _attribute_violations(config: dict[str, list[dict]], source_root: Path) -> list[str]:
    declared = {module["path"] for module in config["modules"]}
    interfaces: dict[str, list[_Interface]] = {}
    for interface in config["interfaces"]:
        for module in interface["from"]:
            interfaces.setdefault(module, []).append(
                (interface["expose"], interface.get("visibility"), interface.get("exclusive", False)),
            )
    # Tach only checks imports between declared modules, so hold attribute access to the same scope.
    interfaced = {module for module in interfaces if module in declared}

    violations: list[str] = []
    for path in sorted(source_root.rglob("*.py")):
        name = _module_name(path, source_root)
        importer = _owning_module(name, declared)
        if importer is None:
            continue
        package = name if path.name == "__init__.py" else name.rpartition(".")[0]
        tree = ast.parse(path.read_text(encoding="utf-8"))
        aliases = _module_aliases(tree, package, interfaced)
        for node in ast.walk(tree):
            if not (isinstance(node, ast.Attribute) and isinstance(node.value, ast.Name)):
                continue
            target = aliases.get(node.value.id)
            if target is None or target == importer:
                continue
            patterns = _visible_patterns(interfaces[target], importer)
            if patterns is not None and not any(re.fullmatch(pattern, node.attr) for pattern in patterns):
                violations.append(
                    f"{path.relative_to(source_root)}:{node.lineno}: {target}.{node.attr} from {importer}",
                )
    return sorted(set(violations))


def test_module_attribute_access_uses_exposed_interface_members() -> None:
    """Every `module.attr` reach into an interfaced Tach module names a member its interface exposes."""
    config = tomllib.loads((REPO_ROOT / "tach.toml").read_text(encoding="utf-8"))
    assert _attribute_violations(config, REPO_ROOT / "src") == []


def test_checker_applies_tach_interface_rules(tmp_path: Path) -> None:
    """The checker reports what Tach would reject for the equivalent `from` import, and nothing else."""
    sources = {
        "__init__.py": "",
        "lib.py": "public = shared = hidden = 1\n",
        "private_lib.py": "anything = 1\n",
        # Public interface; a type-only alias stays unchecked; a module only friend sees is unchecked here.
        "user.py": """
            from typing import TYPE_CHECKING

            from pkg import lib, private_lib

            if TYPE_CHECKING:
                from pkg import lib as typed_lib

            lib.public
            lib.hidden
            typed_lib.hidden
            private_lib.anything
        """,
        # An exclusive interface replaces the public one for the modules it lists.
        "friend.py": """
            from . import lib

            lib.shared
            lib.public
        """,
        "sub/__init__.py": "",
        # Relative imports resolve against the importer's package.
        "sub/user.py": """
            from .. import lib

            lib.hidden
        """,
    }
    for relative, source in sources.items():
        path = tmp_path / "pkg" / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(textwrap.dedent(source), encoding="utf-8")
    config = {
        "modules": [
            {"path": "pkg.lib"},
            {"path": "pkg.private_lib"},
            {"path": "pkg.user"},
            {"path": "pkg.friend"},
            {"path": "pkg.sub.user"},
        ],
        "interfaces": [
            {"from": ["pkg.lib"], "expose": ["public"]},
            {"from": ["pkg.lib"], "expose": ["shared"], "visibility": ["pkg.friend"], "exclusive": True},
            {"from": ["pkg.private_lib"], "expose": ["nothing"], "visibility": ["pkg.friend"]},
        ],
    }

    assert _attribute_violations(config, tmp_path) == [
        "pkg/friend.py:5: pkg.lib.public from pkg.friend",
        "pkg/sub/user.py:4: pkg.lib.hidden from pkg.sub.user",
        "pkg/user.py:10: pkg.lib.hidden from pkg.user",
    ]
