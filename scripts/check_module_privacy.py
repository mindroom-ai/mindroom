#!/usr/bin/env python3
"""Detect public top-level symbols that are never imported by other src modules.

These are candidates for being made private (prefixed with ``_``).

Usage:
    python check_module_privacy.py <project-root>

Only cross-imports within ``src/`` count. Test imports are ignored.
"""

from __future__ import annotations

import ast
import sys
from dataclasses import dataclass, field
from pathlib import Path


@dataclass
class Symbol:
    """A public top-level symbol found in a module."""

    name: str
    kind: str  # "function", "class", or "variable"
    lineno: int
    module: str
    path: Path


@dataclass
class Module:
    """A parsed Python module with its top-level symbols."""

    name: str
    path: Path
    package_parts: tuple[str, ...]
    symbols: list[Symbol] = field(default_factory=list)
    tree: ast.Module | None = None


def _find_src_dir(project_root: Path) -> Path | None:
    src = project_root / "src"
    return src if src.is_dir() else None


def _module_name_from_path(py_file: Path, src_dir: Path) -> str | None:
    """Derive dotted module name from file path relative to src/."""
    rel = py_file.relative_to(src_dir)
    parts = list(rel.with_suffix("").parts)
    if not parts:
        return None
    if parts[-1] == "__init__":
        parts = parts[:-1]
    if not parts:
        return None
    return ".".join(parts)


def _package_parts(module_name: str) -> tuple[str, ...]:
    parts = module_name.rsplit(".", 1)
    if len(parts) == 1:
        return ()
    return tuple(parts[0].split("."))


def collect_modules(src_dir: Path) -> dict[str, Module]:  # noqa: C901, PLR0912
    """Parse every .py under src/ and collect top-level public definitions."""
    modules: dict[str, Module] = {}

    for py_file in sorted(src_dir.rglob("*.py")):
        if "__pycache__" in py_file.parts:
            continue
        mod_name = _module_name_from_path(py_file, src_dir)
        if mod_name is None:
            continue

        source = py_file.read_text(encoding="utf-8")
        try:
            tree = ast.parse(source, filename=str(py_file))
        except SyntaxError:
            continue

        # Detect __all__ to skip explicitly exported names
        explicit_exports = _extract_all(tree)

        mod = Module(
            name=mod_name,
            path=py_file,
            package_parts=_package_parts(mod_name),
            tree=tree,
        )

        for node in tree.body:
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                _maybe_add(mod, node.name, "function", node.lineno, explicit_exports)
            elif isinstance(node, ast.ClassDef):
                _maybe_add(mod, node.name, "class", node.lineno, explicit_exports)
            elif isinstance(node, ast.Assign):
                for target in node.targets:
                    for name in _names_from_target(target):
                        _maybe_add(mod, name, "variable", node.lineno, explicit_exports)
            elif isinstance(node, ast.AnnAssign) and node.target:
                for name in _names_from_target(node.target):
                    _maybe_add(mod, name, "variable", node.lineno, explicit_exports)
            elif hasattr(ast, "TypeAlias") and isinstance(node, ast.TypeAlias):
                for name in _names_from_target(node.name):
                    _maybe_add(mod, name, "variable", node.lineno, explicit_exports)

        modules[mod_name] = mod

    return modules


def _extract_all(tree: ast.Module) -> set[str] | None:
    """Return the set of names in __all__, or None if not defined."""
    for node in tree.body:
        if isinstance(node, ast.Assign):
            for target in node.targets:
                if isinstance(target, ast.Name) and target.id == "__all__":
                    return _strings_from_node(node.value)
    return None


def _strings_from_node(node: ast.expr) -> set[str] | None:
    if isinstance(node, (ast.List, ast.Tuple, ast.Set)):
        names: set[str] = set()
        for elt in node.elts:
            if isinstance(elt, ast.Constant) and isinstance(elt.value, str):
                names.add(elt.value)
            else:
                return None  # non-literal element, bail
        return names
    return None


def _names_from_target(node: ast.expr) -> list[str]:
    if isinstance(node, ast.Name):
        return [node.id]
    if isinstance(node, (ast.Tuple, ast.List)):
        result: list[str] = []
        for elt in node.elts:
            result.extend(_names_from_target(elt))
        return result
    return []


def _maybe_add(
    mod: Module,
    name: str,
    kind: str,
    lineno: int,
    explicit_exports: set[str] | None,
) -> None:
    if name.startswith("_"):
        return
    if explicit_exports is not None and name in explicit_exports:
        return
    mod.symbols.append(Symbol(name=name, kind=kind, lineno=lineno, module=mod.name, path=mod.path))


def _resolve_relative_import(
    importer_package: tuple[str, ...],
    level: int,
    module_attr: str | None,
) -> str | None:
    """Resolve a relative import to an absolute dotted module name."""
    if level == 0:
        return module_attr

    # level=1 means current package, level=2 means parent, etc.
    up = level - 1
    if up > len(importer_package):
        return None
    base = list(importer_package[: len(importer_package) - up])
    if module_attr:
        base.extend(module_attr.split("."))
    return ".".join(base) if base else None


def find_cross_imports(modules: dict[str, Module]) -> set[tuple[str, str]]:  # noqa: C901, PLR0912
    """Return (module_name, symbol_name) pairs that are imported by another src module."""
    known = set(modules)
    used: set[tuple[str, str]] = set()

    # Build a quick lookup: module -> set of defined public symbol names
    defined: dict[str, set[str]] = {}
    for mod_name, mod in modules.items():
        defined[mod_name] = {s.name for s in mod.symbols}

    for consumer_name, consumer in modules.items():
        if consumer.tree is None:
            continue

        # Track `import X` / `import X as Y` so we can resolve `X.foo`
        import_aliases: dict[str, str] = {}  # local_name -> module_name

        for node in ast.walk(consumer.tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    local = alias.asname or alias.name.split(".")[0]
                    import_aliases[local] = alias.name

            elif isinstance(node, ast.ImportFrom):
                source = _resolve_relative_import(
                    consumer.package_parts,
                    node.level or 0,
                    node.module,
                )
                if source is None:
                    continue

                for alias in node.names:
                    sym = alias.name
                    if sym == "*":
                        # `from mod import *` marks everything as used
                        if source in defined and source != consumer_name:
                            for s in defined[source]:
                                used.add((source, s))
                        continue

                    # Could be importing a submodule (e.g. `from pkg import submod`)
                    sub = f"{source}.{sym}"
                    if sub in known:
                        local = alias.asname or sym
                        import_aliases[local] = sub
                        continue

                    if source != consumer_name and source in defined and sym in defined[source]:
                        used.add((source, sym))

        # Second pass: resolve attribute access like `module.symbol`
        for node in ast.walk(consumer.tree):
            if not isinstance(node, ast.Attribute):
                continue
            if not isinstance(node.value, ast.Name):
                continue
            obj_name = node.value.id
            attr = node.attr
            mod_name = import_aliases.get(obj_name)
            if mod_name and mod_name != consumer_name and mod_name in defined and attr in defined[mod_name]:
                used.add((mod_name, attr))

    return used


def main() -> int:
    """Entry point: scan project and report module-local public symbols."""
    if len(sys.argv) < 2:
        print(f"Usage: {sys.argv[0]} <project-root>", file=sys.stderr)
        return 2

    project_root = Path(sys.argv[1]).resolve()
    src_dir = _find_src_dir(project_root)
    if src_dir is None:
        print(f"No src/ directory found in {project_root}", file=sys.stderr)
        return 1

    modules = collect_modules(src_dir)
    cross_imports = find_cross_imports(modules)

    candidates: list[Symbol] = [
        sym for mod in modules.values() for sym in mod.symbols if (sym.module, sym.name) not in cross_imports
    ]

    candidates.sort(key=lambda s: (str(s.path), s.lineno))

    if not candidates:
        print("All public symbols are used across modules.")
        return 0

    print(f"Found {len(candidates)} symbols that could be made private:\n")
    for sym in candidates:
        rel = sym.path.relative_to(project_root)
        print(f"  {rel}:{sym.lineno}: {sym.kind} `{sym.name}`")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
