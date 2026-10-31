"""Compare per-function test coverage (baseline tree) with live coverage (cut tree).

Usage: python -I compare.py BASE_NIO BASE_COV CUT_NIO LIVE_COV PREFIX
"""

import ast
import collections
import json
import pathlib
import sys

base, base_cov, cut, live_cov, prefix = sys.argv[1:6]
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


def ran_map(root, cov_path):
    cov = json.loads(pathlib.Path(cov_path).read_text())["files"]
    out = {}
    for p in pathlib.Path(root).rglob("*.py"):
        rel = str(p.relative_to(root))
        lines = set(cov.get(f"{prefix}/{rel}", {}).get("executed_lines", []))
        for q, n in funcs(p).items():
            out[(rel, q)] = (
                any(n.body[0].lineno <= line <= n.end_lineno for line in lines),
                n.end_lineno - n.lineno + 1,
            )
    return out


test = ran_map(base, base_cov)
live = ran_map(cut, live_cov)
cats = collections.Counter()
lines = collections.Counter()
unproven = collections.Counter()
for key, (live_ran, size) in live.items():
    test_ran = test.get(key, (False, 0))[0]
    cat = {
        (True, True): "tests+live",
        (True, False): "tests only",
        (False, True): "live only (mocked in tests)",
        (False, False): "neither",
    }[(test_ran, live_ran)]
    cats[cat] += 1
    lines[cat] += size
    if cat == "neither":
        unproven[key[0]] += size
total = sum(len(p.read_text().splitlines()) for p in pathlib.Path(cut).rglob("*.py"))
print(f"cut tree: {total} lines, {len(live)} functions")
for cat in ["tests+live", "tests only", "live only (mocked in tests)", "neither"]:
    print(f"  {cat:30s} {cats[cat]:5d} functions {lines[cat]:6d} lines")
print("neither, by file:")
for f, n in unproven.most_common(12):
    print(f"  {n:6d}  {f}")
