"""Drop imports of names a cut removed until `import nio` (and listed submodules) succeed.

Usage: python -I prune_imports.py PYTHON PARENT_DIR MODULE[,MODULE...]
"""

from __future__ import annotations

import ast
import pathlib
import re
import subprocess
import sys

ERR = re.compile(r"cannot import name '(?P<name>\w+)' from '(?P<mod>[\w.]+)'")
ALL = re.compile(r"module '(?P<mod>[\w.]+)' has no attribute '(?P<name>\w+)'")


def drop_all_entry(parent: pathlib.Path, mod: str, name: str) -> None:
    base = parent.joinpath(*mod.split("."))
    path = base / "__init__.py" if base.is_dir() else base.with_suffix(".py")
    lines = path.read_text().splitlines(keepends=True)
    hits = [
        i
        for i, line in enumerate(lines)
        if line.strip() in (f'"{name}",', f"'{name}',")
    ]
    if len(hits) != 1:
        raise SystemExit(f"cannot find unique __all__ entry {name} in {path}: {hits}")
    del lines[hits[0]]
    path.write_text("".join(lines))


FRAME = re.compile(r'File "(?P<file>[^"]+)", line (?P<line>\d+)')


def drop_alias(path: pathlib.Path, line: int, name: str) -> None:
    src = path.read_text()
    lines = src.splitlines(keepends=True)
    tree = ast.parse(src)
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.lineno <= line <= node.end_lineno:
            keep = [a for a in node.names if a.name != name]
            indent = " " * node.col_offset
            if keep:
                names = ", ".join(
                    a.name if not a.asname else f"{a.name} as {a.asname}" for a in keep
                )
                text = f"{indent}from {'.' * node.level}{node.module or ''} import ({names})\n"
            else:
                text = f"{indent}pass\n"
            lines[node.lineno - 1 : node.end_lineno] = [text]
            path.write_text("".join(lines))
            return
    raise SystemExit(f"no ImportFrom covering {path}:{line} for {name}")


def main() -> None:
    python, parent, modules = sys.argv[1], sys.argv[2], sys.argv[3].split(",")
    code = "; ".join(f"import {m}" for m in modules)
    for _ in range(500):
        proc = subprocess.run(
            [python, "-c", code],
            cwd="/tmp",
            env={"PYTHONPATH": parent},
            capture_output=True,
            text=True,
        )
        if proc.returncode == 0:
            print("imports ok")
            return
        a = ALL.search(proc.stderr)
        if a and "import *" in proc.stderr:
            print(f"drop __all__ {a['mod']}.{a['name']}")
            drop_all_entry(pathlib.Path(parent), a["mod"], a["name"])
            continue
        m = ERR.search(proc.stderr)
        if not m:
            print(proc.stderr[-2000:])
            raise SystemExit("unhandled import error")
        frames = [
            f for f in FRAME.finditer(proc.stderr) if f["file"].startswith(parent)
        ]
        frame = frames[-1]
        print(f"drop {m['name']} at {frame['file']}:{frame['line']}")
        drop_alias(pathlib.Path(frame["file"]), int(frame["line"]), m["name"])
    raise SystemExit("too many iterations")


if __name__ == "__main__":
    main()
