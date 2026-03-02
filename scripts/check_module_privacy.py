#!/usr/bin/env python3
"""Detect public top-level symbols that are never imported by other src modules.

These are candidates for being made private (prefixed with ``_``).

Usage:
    python check_module_privacy.py <project-root>

Only cross-imports within ``src/`` count. Test imports are ignored.
"""

from __future__ import annotations

import ast
import re
import sys
import tomllib
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


_ROUTE_DECORATORS = {
    "api_route",
    "delete",
    "get",
    "head",
    "options",
    "patch",
    "post",
    "put",
    "trace",
    "websocket",
    "websocket_route",
}
_CLI_DECORATORS = {"callback", "command"}
_FRAMEWORK_CONSTRUCTORS = {"APIRouter", "FastAPI", "Typer"}
_FRAMEWORK_REGISTRATION_CALLS = {"add_api_route", "add_api_websocket_route", "include_router"}
_ALLOWED_PUBLIC_NAMES = {"logger"}
_ENTRYPOINT_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_\\.]*:[A-Za-z_][A-Za-z0-9_]*$")
_UVICORN_RE = re.compile(r"\buvicorn\s+([A-Za-z_][A-Za-z0-9_\.]*):([A-Za-z_][A-Za-z0-9_]*)\b")


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
        framework_related_names = _collect_framework_related_names(tree)
        pydantic_model_names: set[str] = set()

        mod = Module(
            name=mod_name,
            path=py_file,
            package_parts=_package_parts(mod_name),
            tree=tree,
        )

        for node in tree.body:
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                if _is_framework_callback(node):
                    continue
                _maybe_add(
                    mod,
                    node.name,
                    "function",
                    node.lineno,
                    explicit_exports,
                    ignored_names=framework_related_names,
                )
            elif isinstance(node, ast.ClassDef):
                if _is_pydantic_model(node, pydantic_model_names):
                    pydantic_model_names.add(node.name)
                    continue
                _maybe_add(
                    mod,
                    node.name,
                    "class",
                    node.lineno,
                    explicit_exports,
                    ignored_names=framework_related_names,
                )
            elif isinstance(node, ast.Assign):
                if _is_framework_constructor_call(node.value):
                    continue
                for target in node.targets:
                    for name in _names_from_target(target):
                        _maybe_add(
                            mod,
                            name,
                            "variable",
                            node.lineno,
                            explicit_exports,
                            ignored_names=framework_related_names,
                        )
            elif isinstance(node, ast.AnnAssign) and node.target:
                if node.value is not None and _is_framework_constructor_call(node.value):
                    continue
                for name in _names_from_target(node.target):
                    _maybe_add(
                        mod,
                        name,
                        "variable",
                        node.lineno,
                        explicit_exports,
                        ignored_names=framework_related_names,
                    )
            elif hasattr(ast, "TypeAlias") and isinstance(node, ast.TypeAlias):
                for name in _names_from_target(node.name):
                    _maybe_add(
                        mod,
                        name,
                        "variable",
                        node.lineno,
                        explicit_exports,
                        ignored_names=framework_related_names,
                    )

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


def _dotted_name(node: ast.expr) -> str | None:
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        parent = _dotted_name(node.value)
        if parent is None:
            return None
        return f"{parent}.{node.attr}"
    return None


def _decorator_attr_name(decorator: ast.expr) -> str | None:
    target = decorator.func if isinstance(decorator, ast.Call) else decorator
    if isinstance(target, ast.Attribute):
        return target.attr
    return None


def _is_framework_callback(node: ast.FunctionDef | ast.AsyncFunctionDef) -> bool:
    for decorator in node.decorator_list:
        attr_name = _decorator_attr_name(decorator)
        if attr_name in _ROUTE_DECORATORS or attr_name in _CLI_DECORATORS:
            return True
    return False


def _framework_callback_names(node: ast.FunctionDef | ast.AsyncFunctionDef) -> set[str]:
    names: set[str] = set()
    expressions: list[ast.expr] = [*node.decorator_list]

    if node.returns is not None:
        expressions.append(node.returns)

    arg_annotations = [
        arg.annotation
        for arg in [*node.args.posonlyargs, *node.args.args, *node.args.kwonlyargs]
        if arg.annotation is not None
    ]
    expressions.extend(arg_annotations)
    if node.args.vararg and node.args.vararg.annotation is not None:
        expressions.append(node.args.vararg.annotation)
    if node.args.kwarg and node.args.kwarg.annotation is not None:
        expressions.append(node.args.kwarg.annotation)

    defaults = [*node.args.defaults, *(d for d in node.args.kw_defaults if d is not None)]
    expressions.extend(defaults)

    for expr in expressions:
        names.update(_names_in_expr(expr))

    return names


def _names_in_expr(node: ast.AST | None) -> set[str]:
    if node is None:
        return set()
    return {child.id for child in ast.walk(node) if isinstance(child, ast.Name)}


def _is_framework_registration_call(node: ast.expr) -> bool:
    if not isinstance(node, ast.Call):
        return False
    if not isinstance(node.func, ast.Attribute):
        return False
    return node.func.attr in _FRAMEWORK_REGISTRATION_CALLS


def _collect_framework_related_names(tree: ast.Module) -> set[str]:
    names: set[str] = set()
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            if _is_framework_callback(node):
                names.update(_framework_callback_names(node))
            continue

        expr: ast.expr | None = None
        if isinstance(node, (ast.Expr, ast.Assign, ast.AnnAssign)):
            expr = node.value

        if expr is not None and _is_framework_registration_call(expr):
            names.update(_names_in_expr(expr))

    return names


def _is_pydantic_model(node: ast.ClassDef, known_models: set[str]) -> bool:
    for base in node.bases:
        base_name = _dotted_name(base)
        if base_name is None:
            continue
        if base_name == "BaseModel" or base_name.endswith(".BaseModel"):
            return True
        short = base_name.rsplit(".", 1)[-1]
        if short in known_models:
            return True
    return False


def _is_framework_constructor_call(node: ast.expr) -> bool:
    if not isinstance(node, ast.Call):
        return False
    callee = _dotted_name(node.func)
    if callee is None:
        return False
    short = callee.rsplit(".", 1)[-1]
    return short in _FRAMEWORK_CONSTRUCTORS


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
    *,
    ignored_names: set[str] | None = None,
) -> None:
    if name.startswith("_"):
        return
    if name in _ALLOWED_PUBLIC_NAMES:
        return
    if explicit_exports is not None and name in explicit_exports:
        return
    if ignored_names is not None and name in ignored_names:
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


def _load_pyproject_entrypoints(project_root: Path) -> set[tuple[str, str]]:
    pyproject_path = project_root / "pyproject.toml"
    if not pyproject_path.exists():
        return set()

    data = tomllib.loads(pyproject_path.read_text(encoding="utf-8"))
    project_table = data.get("project", {})

    pairs: set[tuple[str, str]] = set()
    for table_key in ("scripts", "gui-scripts"):
        table = project_table.get(table_key, {})
        if not isinstance(table, dict):
            continue
        for raw in table.values():
            if not isinstance(raw, str):
                continue
            if not _ENTRYPOINT_RE.fullmatch(raw):
                continue
            module_name, symbol_name = raw.split(":", 1)
            pairs.add((module_name, symbol_name))
    return pairs


def _entrypoint_shell_files(project_root: Path) -> list[Path]:
    files: list[Path] = []
    files.extend(project_root.glob("*.sh"))
    files.extend(project_root.glob("Dockerfile*"))
    scripts_dir = project_root / "scripts"
    if scripts_dir.exists():
        files.extend(scripts_dir.rglob("*.sh"))
    return sorted(set(files))


def _load_shell_uvicorn_entrypoints(project_root: Path) -> set[tuple[str, str]]:
    pairs: set[tuple[str, str]] = set()
    for path in _entrypoint_shell_files(project_root):
        text = path.read_text(encoding="utf-8")
        for module_name, symbol_name in _UVICORN_RE.findall(text):
            pairs.add((module_name, symbol_name))
    return pairs


def _collect_external_entrypoints(project_root: Path) -> set[tuple[str, str]]:
    pairs = _load_pyproject_entrypoints(project_root)
    pairs.update(_load_shell_uvicorn_entrypoints(project_root))
    return pairs


def find_private_candidates(project_root: Path) -> list[Symbol]:
    """Find symbols that appear module-local and should be private."""
    src_dir = _find_src_dir(project_root)
    if src_dir is None:
        msg = f"No src/ directory found in {project_root}"
        raise FileNotFoundError(msg)

    modules = collect_modules(src_dir)
    cross_imports = find_cross_imports(modules)
    external_entrypoints = _collect_external_entrypoints(project_root)

    candidates = [
        sym
        for mod in modules.values()
        for sym in mod.symbols
        if (sym.module, sym.name) not in cross_imports and (sym.module, sym.name) not in external_entrypoints
    ]
    candidates.sort(key=lambda s: (str(s.path), s.lineno))
    return candidates


def main() -> int:
    """Entry point: scan project and report module-local public symbols."""
    if len(sys.argv) < 2:
        print(f"Usage: {sys.argv[0]} <project-root>", file=sys.stderr)
        return 2

    project_root = Path(sys.argv[1]).resolve()
    try:
        candidates = find_private_candidates(project_root)
    except FileNotFoundError as exc:
        print(str(exc), file=sys.stderr)
        return 1

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
