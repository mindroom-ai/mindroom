"""Functions never run under baseline coverage that still exist after the cut.

Usage: python -I never_after.py BASE_NIO COV_JSON COV_PREFIX CUT_NIO
"""

import ast
import collections
import json
import os
import pathlib
import sys

base, cov, prefix, cut = (
    pathlib.Path(sys.argv[1]),
    json.loads(pathlib.Path(sys.argv[2]).read_text()),
    sys.argv[3],
    pathlib.Path(sys.argv[4]),
)
FUNCS = (ast.FunctionDef, ast.AsyncFunctionDef)


def funcs(path):
    out = {}

    def walk(node, q):
        for c in ast.iter_child_nodes(node):
            if isinstance(c, (*FUNCS, ast.ClassDef)):
                name = f"{q}{c.name}"
                if isinstance(c, FUNCS):
                    out[name] = c
                walk(c, f"{name}.")

    walk(ast.parse(path.read_text()), "")
    return out


executed = {}
for name, info in cov["files"].items():
    if name.startswith(prefix):
        executed[name[len(prefix) :].lstrip("/")] = set(info["executed_lines"])
never = {}
for p in base.rglob("*.py"):
    rel = str(p.relative_to(base))
    ran = executed.get(rel, set())
    for q, n in funcs(p).items():
        if not any(n.body[0].lineno <= line <= n.end_lineno for line in ran):
            never[(rel, q)] = True
per_file = collections.Counter()
count = 0
for p in cut.rglob("*.py"):
    rel = str(p.relative_to(cut))
    fs = funcs(p)
    for q, n in fs.items():
        if (rel, q) in never and not any(
            (rel, q2) in never and q.startswith(q2 + ".") for q2 in fs
        ):
            per_file[rel] += n.end_lineno - n.lineno + 1
            count += 1
total = sum(len(p.read_text().splitlines()) for p in cut.rglob("*.py"))
print(
    f"never-run functions still present: {count}, {sum(per_file.values())} lines of {total} ({sum(per_file.values()) / total:.1%})"
)
if os.environ.get("LIST"):
    for p in cut.rglob("*.py"):
        rel = str(p.relative_to(cut))
        fs = funcs(p)
        for q, n in fs.items():
            if (rel, q) in never and not any(
                (rel, q2) in never and q.startswith(q2 + ".") for q2 in fs
            ):
                print(f"{n.end_lineno - n.lineno + 1:5d} {rel}::{q}")
for rel, n in per_file.most_common(15):
    print(f"  {n:6d}  {rel}")
