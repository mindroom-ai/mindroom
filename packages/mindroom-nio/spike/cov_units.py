"""Per-function coverage of nio from one or more coverage.py JSON reports.

A function counts as executed when any line of its body (after the signature) ran.
Prints statement coverage and never-executed function lines per file.

Usage: python -I cov_units.py NIO_DIR COV_JSON[,COV_JSON...]
"""

from __future__ import annotations

import ast
import collections
import json
import pathlib
import sys

FUNCS = (ast.FunctionDef, ast.AsyncFunctionDef)


def main() -> None:
    nio_dir = pathlib.Path(sys.argv[1]).resolve()
    executed: dict[pathlib.Path, set[int]] = collections.defaultdict(set)
    statements: dict[pathlib.Path, set[int]] = collections.defaultdict(set)
    for report in sys.argv[2].split(","):
        data = json.loads(pathlib.Path(report).read_text())
        for name, info in data["files"].items():
            p = pathlib.Path(name)
            p = p if p.is_absolute() else pathlib.Path.cwd() / p
            p = p.resolve()
            if nio_dir not in p.parents:
                continue
            executed[p] |= set(info["executed_lines"])
            statements[p] |= set(info["executed_lines"]) | set(info["missing_lines"])

    total_stmts = sum(len(s) for s in statements.values())
    total_exec = sum(len(s) for s in executed.values())
    print(
        f"statements={total_stmts} executed={total_exec} ({total_exec / total_stmts:.1%})"
    )

    never: dict[pathlib.Path, int] = collections.Counter()
    never_count = 0
    func_count = 0
    for p in sorted(nio_dir.rglob("*.py")):
        tree = ast.parse(p.read_text())
        ran = executed.get(p, set())
        for node in ast.walk(tree):
            if not isinstance(node, FUNCS):
                continue
            func_count += 1
            body_start = node.body[0].lineno
            if not any(body_start <= line <= node.end_lineno for line in ran):
                # Only count the outermost never-run function.
                never[p] += 0
                node._never = True  # type: ignore[attr-defined]
        for node in ast.walk(tree):
            if isinstance(node, FUNCS) and getattr(node, "_never", False):
                parents_never = False
                for other in ast.walk(tree):
                    if (
                        other is not node
                        and isinstance(other, FUNCS)
                        and getattr(other, "_never", False)
                    ):
                        if (
                            other.lineno < node.lineno
                            and other.end_lineno >= node.end_lineno
                        ):
                            parents_never = True
                            break
                if not parents_never:
                    never[p] += node.end_lineno - node.lineno + 1
                    never_count += 1
    total_lines = sum(len(p.read_text().splitlines()) for p in nio_dir.rglob("*.py"))
    never_lines = sum(never.values())
    print(
        f"functions={func_count} never_run={never_count} never_run_lines={never_lines} of {total_lines} ({never_lines / total_lines:.1%})"
    )
    for p, n in sorted(never.items(), key=lambda kv: -kv[1])[:20]:
        print(f"  {n:6d}  {p.relative_to(nio_dir)}")


if __name__ == "__main__":
    main()
