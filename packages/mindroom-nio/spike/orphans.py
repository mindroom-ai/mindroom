"""Delete nio modules that no live import reaches.

Roots: every nio module MindRoom's consumer trees import, plus their parent packages.
Edges: every import statement inside nio (including TYPE_CHECKING and try blocks).

Usage: python -I orphans.py SRC_DIR CONSUMER_DIR[,CONSUMER_DIR...] [--apply]
"""

from __future__ import annotations

import ast
import pathlib
import sys


def module_name(src: pathlib.Path, path: pathlib.Path) -> str:
    parts = list(path.relative_to(src).with_suffix("").parts)
    if parts[-1] == "__init__":
        parts.pop()
    return ".".join(parts)


def resolve(current: str, is_pkg: bool, node: ast.ImportFrom) -> str:
    if node.level == 0:
        return node.module or ""
    base = current.split(".") if is_pkg else current.split(".")[:-1]
    base = base[: len(base) - (node.level - 1)]
    return ".".join(base + ([node.module] if node.module else []))


def with_parents(name: str) -> set[str]:
    parts = name.split(".")
    return {".".join(parts[: i + 1]) for i in range(len(parts))}


def main() -> None:
    src = pathlib.Path(sys.argv[1])
    consumers = [pathlib.Path(p) for p in sys.argv[2].split(",")]
    files = {module_name(src, p): p for p in (src / "nio").rglob("*.py")}

    def targets(node: ast.AST, current: str, is_pkg: bool) -> set[str]:
        out: set[str] = set()
        if isinstance(node, ast.Import):
            for a in node.names:
                if a.name.split(".")[0] == "nio":
                    out |= with_parents(a.name)
        elif isinstance(node, ast.ImportFrom):
            mod = resolve(current, is_pkg, node)
            if mod.split(".")[0] == "nio":
                out |= with_parents(mod)
                for a in node.names:
                    if f"{mod}.{a.name}" in files:
                        out.add(f"{mod}.{a.name}")
        return out

    live: set[str] = set()
    for d in consumers:
        for p in d.rglob("*.py"):
            try:
                tree = ast.parse(p.read_text())
            except SyntaxError:
                continue
            for node in ast.walk(tree):
                live |= targets(node, "", False)
    queue = [m for m in live if m in files]
    seen: set[str] = set()
    while queue:
        m = queue.pop()
        if m in seen:
            continue
        seen.add(m)
        p = files[m]
        tree = ast.parse(p.read_text())
        for node in ast.walk(tree):
            queue.extend(
                t
                for t in targets(node, m, p.name == "__init__.py")
                if t in files and t not in seen
            )
    orphans = sorted(set(files) - seen)
    for m in orphans:
        print(f"orphan {m} ({len(files[m].read_text().splitlines())} lines)")
        if "--apply" in sys.argv:
            files[m].unlink()


if __name__ == "__main__":
    main()
