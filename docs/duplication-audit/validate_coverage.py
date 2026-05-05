"""Validate source duplication audit report coverage."""

# ruff: noqa: INP001

from __future__ import annotations

import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
AUDIT_DIR = ROOT / "docs" / "duplication-audit"
SYMBOLS_PATH = AUDIT_DIR / "symbols-src.json"


def _coverage_qualnames(report_text: str) -> list[str]:
    match = re.search(r"```coverage-tsv\n(?P<body>.*?)\n```", report_text, re.DOTALL)
    if not match:
        return []

    rows = [line for line in match.group("body").splitlines() if line.strip() and not line.startswith("qualname\t")]
    return [row.split("\t", 1)[0] for row in rows]


def main() -> int:
    """Validate every generated report against the symbol manifest."""
    entries = json.loads(SYMBOLS_PATH.read_text())
    failures: list[str] = []

    for entry in entries:
        expected = [symbol["qualname"] for symbol in entry["symbols"]]
        if not expected:
            expected = ["MODULE_LEVEL"]

        report_path = ROOT / entry["report_path"]
        if not report_path.exists():
            failures.append(f"MISSING REPORT\t{entry['path']}\t{entry['report_path']}")
            continue

        actual = _coverage_qualnames(report_path.read_text())
        missing = sorted(set(expected) - set(actual))
        extra = sorted(set(actual) - set(expected))
        duplicate_rows = sorted({name for name in actual if actual.count(name) > 1})

        if missing or extra or duplicate_rows:
            failures.append(
                f"COVERAGE MISMATCH\t{entry['path']}\tmissing={missing}\textra={extra}\tduplicates={duplicate_rows}",
            )

    if failures:
        print("\n".join(failures))
        return 1

    print(f"Coverage complete for {len(entries)} source modules.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
