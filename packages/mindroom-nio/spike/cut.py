"""Name-based reachability cut of vendored nio.

Roots: every identifier, attribute name, and identifier-like string in the consumer trees,
plus every reference made by nio module-level code that is not an import or __all__.
A nio def (module function, class, method, nested class) is live when its name is a live
name and its container is live; dunders of a live class are live. Live defs add their
references to the live names. Everything else is dead.

Usage: python -I cut.py NIO_DIR CONSUMER_DIR[,CONSUMER_DIR...] [--apply] [--json OUT]
"""

from __future__ import annotations

import ast
import collections
import json
import pathlib
import re
import sys
from dataclasses import dataclass, field

# Bases whose subclasses carry no convention-called members.
PLAIN_BASES = {
    "object",
    "Enum",
    "StrEnum",
    "IntEnum",
    "Exception",
    "Protocol",
    "TypedDict",
    "Generic",
    "NamedTuple",
}
NIO_IMPORTERS_ONLY = "--nio-importers-only" in sys.argv
IDENT = re.compile(r"^[A-Za-z_][A-Za-z0-9_.]*$")


class Refs(ast.NodeVisitor):
    def __init__(self) -> None:
        self.names: set[str] = set()

    def visit_Import(self, node: ast.Import) -> None:
        pass

    def visit_ImportFrom(self, node: ast.ImportFrom) -> None:
        pass

    def visit_Name(self, node: ast.Name) -> None:
        self.names.add(node.id)

    def visit_Attribute(self, node: ast.Attribute) -> None:
        self.names.add(node.attr)
        self.generic_visit(node)

    def visit_Constant(self, node: ast.Constant) -> None:
        if isinstance(node.value, str) and IDENT.match(node.value):
            self.names.update(node.value.split("."))

    def visit_keyword(self, node: ast.keyword) -> None:
        if node.arg:
            self.names.add(node.arg)
        self.generic_visit(node)


def refs_of(nodes: list[ast.AST]) -> set[str]:
    r = Refs()
    for n in nodes:
        r.visit(n)
    return r.names


@dataclass(eq=False)
class Unit:
    path: pathlib.Path
    qual: str
    name: str
    node: ast.AST
    parent: Unit | None
    refs: set[str] = field(default_factory=set)
    children: list[Unit] = field(default_factory=list)
    # Subclasses a third-party base: the library may call any member by convention
    # (peewee Field.python_value/db_value, Model.Meta, SqliteDatabase._connect).
    foreign_base: bool = False

    @property
    def start(self) -> int:
        decos = getattr(self.node, "decorator_list", [])
        return min([self.node.lineno] + [d.lineno for d in decos])

    @property
    def end(self) -> int:
        return self.node.end_lineno


DEFS = (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)


def is_all_assign(stmt: ast.stmt) -> bool:
    targets = []
    if isinstance(stmt, ast.Assign):
        targets = stmt.targets
    elif isinstance(stmt, (ast.AugAssign, ast.AnnAssign)):
        targets = [stmt.target]
    return any(isinstance(t, ast.Name) and t.id == "__all__" for t in targets)


def build_units(path: pathlib.Path, tree: ast.Module) -> tuple[list[Unit], set[str]]:
    units: list[Unit] = []
    module_refs: set[str] = set()

    def add(node: ast.AST, parent: Unit | None, prefix: str) -> Unit:
        u = Unit(path, f"{prefix}{node.name}", node.name, node, parent)
        units.append(u)
        if isinstance(node, ast.ClassDef):
            header = list(node.bases) + list(node.keywords) + list(node.decorator_list)
            body_rest = [s for s in node.body if not isinstance(s, DEFS)]
            u.refs = refs_of(header + body_rest)
            for s in node.body:
                if isinstance(s, DEFS):
                    u.children.append(add(s, u, f"{u.qual}."))
        else:
            u.refs = refs_of([node])
        return u

    for stmt in tree.body:
        if isinstance(stmt, DEFS):
            add(stmt, None, "")
        elif is_all_assign(stmt):
            continue
        else:
            module_refs |= refs_of([stmt])
    return units, module_refs


def consumer_roots(dirs: list[pathlib.Path]) -> set[str]:
    roots: set[str] = set()
    for d in dirs:
        for p in d.rglob("*.py"):
            try:
                tree = ast.parse(p.read_text())
            except SyntaxError:
                continue
            if NIO_IMPORTERS_ONLY and not any(
                (
                    isinstance(n, ast.ImportFrom)
                    and n.module
                    and n.module.split(".")[0] == "nio"
                )
                or (
                    isinstance(n, ast.Import)
                    and any(a.name.split(".")[0] == "nio" for a in n.names)
                )
                for n in ast.walk(tree)
            ):
                continue
            roots |= refs_of([tree])
            for node in ast.walk(tree):
                if (
                    isinstance(node, ast.ImportFrom)
                    and node.module
                    and node.module.split(".")[0] == "nio"
                ):
                    roots.update(a.name for a in node.names)
                    roots.update(node.module.split("."))
    return roots


def is_dunder(name: str) -> bool:
    return name.startswith("__") and name.endswith("__")


def reach(units: list[Unit], roots: set[str]) -> set[Unit]:
    by_name: dict[str, list[Unit]] = collections.defaultdict(list)
    for u in units:
        by_name[u.name].append(u)
    live: set[Unit] = set()
    names: set[str] = set()
    queue: list[str] = []

    def add_name(n: str) -> None:
        if n not in names:
            names.add(n)
            queue.append(n)

    def activate(u: Unit) -> None:
        if u in live or (u.parent is not None and u.parent not in live):
            return
        live.add(u)
        for n in u.refs:
            add_name(n)
        for c in u.children:
            if c.name in names or is_dunder(c.name) or u.foreign_base:
                activate(c)

    # Registration decorators (`@Checker.checks(...)`) wire a def in by side effect.
    for u in units:
        if u.parent is None and any(
            isinstance(d, ast.Call) and isinstance(d.func, ast.Attribute)
            for d in getattr(u.node, "decorator_list", [])
        ):
            add_name(u.name)
    for n in roots:
        add_name(n)
    while queue:
        n = queue.pop()
        for u in by_name.get(n, []):
            activate(u)
    return live


def leading_comment_start(lines: list[str], start: int) -> int:
    """Extend a 1-based start line upward over directly attached comment lines."""
    i = start - 1
    while i - 1 >= 0 and lines[i - 1].lstrip().startswith("#"):
        i -= 1
    return i + 1


def main() -> None:
    nio_dir = pathlib.Path(sys.argv[1])
    consumers = [pathlib.Path(p) for p in sys.argv[2].split(",")]
    apply = "--apply" in sys.argv
    json_out = sys.argv[sys.argv.index("--json") + 1] if "--json" in sys.argv else None

    trees = {p: ast.parse(p.read_text()) for p in sorted(nio_dir.rglob("*.py"))}
    units: list[Unit] = []
    module_refs: set[str] = set()
    for p, tree in trees.items():
        us, mr = build_units(p, tree)
        units += us
        module_refs |= mr
    local_classes = {u.name for u in units if isinstance(u.node, ast.ClassDef)}
    for u in units:
        if isinstance(u.node, ast.ClassDef):
            base_names = {
                ast.unparse(b).split("[")[0].split(".")[-1] for b in u.node.bases
            }
            u.foreign_base = bool(base_names - local_classes - PLAIN_BASES)
    roots = consumer_roots(consumers) | module_refs
    live = reach(units, roots)

    # Top-most dead units: dead with a live (or no) container.
    dead_top = [
        u for u in units if u not in live and (u.parent is None or u.parent in live)
    ]
    per_file: dict[pathlib.Path, list[Unit]] = collections.defaultdict(list)
    for u in dead_top:
        per_file[u.path].append(u)

    total = sum(len(p.read_text().splitlines()) for p in trees)
    dead_lines = sum(u.end - u.start + 1 for u in dead_top)
    print(f"units={len(units)} live={len(live)} dead_top={len(dead_top)}")
    print(f"nio lines={total} dead def lines={dead_lines} ({dead_lines / total:.1%})")
    by_file = sorted(
        per_file.items(), key=lambda kv: -sum(u.end - u.start + 1 for u in kv[1])
    )
    for p, us in by_file[:25]:
        print(f"  {sum(u.end - u.start + 1 for u in us):6d}  {p.relative_to(nio_dir)}")
    if json_out:
        pathlib.Path(json_out).write_text(
            json.dumps(
                [
                    {
                        "file": str(u.path.relative_to(nio_dir)),
                        "qual": u.qual,
                        "lines": u.end - u.start + 1,
                    }
                    for u in dead_top
                ],
                indent=1,
            )
        )

    if not apply:
        return
    for p, us in per_file.items():
        lines = p.read_text().splitlines(keepends=True)
        tree = trees[p]
        # Class bodies that would become empty need a `pass`.
        needs_pass: dict[int, str] = {}
        dead_ids = {id(u.node) for u in us}
        for node in ast.walk(tree):
            if isinstance(node, ast.ClassDef) and all(
                id(s) in dead_ids for s in node.body
            ):
                first = node.body[0]
                indent = " " * first.col_offset
                needs_pass[
                    first.lineno
                    if not getattr(first, "decorator_list", None)
                    else min(d.lineno for d in first.decorator_list)
                ] = f"{indent}pass\n"
        spans = [
            (
                leading_comment_start(lines, u.start),
                u.end,
                needs_pass.get(u.start),
            )
            for u in us
        ]
        for start, end, replacement in sorted(spans, reverse=True):
            lines[start - 1 : end] = [replacement] if replacement else []
        p.write_text("".join(lines))


if __name__ == "__main__":
    main()
