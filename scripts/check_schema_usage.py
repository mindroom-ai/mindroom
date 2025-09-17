"""Check that consolidated Supabase schema columns are referenced in the repo.

The script parses `000_consolidated_complete_schema.sql`, extracts tables and
column names, then uses ripgrep to see where each identifier is referenced in
the codebase (excluding the schema file itself).

The output lists columns with zero matches so they can be investigated.
"""

from __future__ import annotations

import re
import subprocess
import sys
from collections import defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCHEMA_PATH = ROOT / "saas-platform" / "supabase" / "migrations" / "000_consolidated_complete_schema.sql"


TABLE_RE = re.compile(r"^CREATE TABLE\s+([A-Za-z0-9_\"]+)\s*\(", re.IGNORECASE)
BLOCK_END_RE = re.compile(r"^\s*\)\s*;")
SKIP_PREFIXES = {
    "PRIMARY",
    "FOREIGN",
    "UNIQUE",
    "CHECK",
    "CONSTRAINT",
    "ALTER",
    "GRANT",
    "REFERENCES",
}


def parse_schema(schema: Path) -> dict[str, list[str]]:
    """Return mapping of table -> column names defined in the schema."""
    if not schema.exists():
        message = f"Schema file not found: {schema}"
        raise FileNotFoundError(message)

    tables: dict[str, list[str]] = defaultdict(list)
    current_table: str | None = None

    with schema.open(encoding="utf-8") as handle:
        for raw_line in handle:
            line = raw_line.strip()

            if not line or line.startswith("--"):
                continue

            match = TABLE_RE.match(line)
            if match:
                current_table = match.group(1).strip('"')
                continue

            if current_table is None:
                continue

            if BLOCK_END_RE.match(line):
                current_table = None
                continue

            # Remove inline comments and trailing commas
            no_comment = line.split("--", 1)[0].rstrip(", ")
            if not no_comment:
                continue

            first_token = no_comment.split()[0]
            token_upper = first_token.upper().strip('"').split("(")[0]

            if token_upper in SKIP_PREFIXES:
                continue

            column_name = first_token.strip('"').split("(")[0]
            if column_name:
                tables[current_table].append(column_name)

    return tables


def search_usage(identifier: str) -> set[Path]:
    """Return set of files referencing the identifier (excluding schema)."""
    cmd = [
        "rg",
        "-n",
        "-w",
        "--color",
        "never",
        "--no-heading",
        "--glob",
        "!saas-platform/supabase/migrations/000_consolidated_complete_schema.sql",
        identifier,
    ]

    result = subprocess.run(cmd, check=False, cwd=ROOT, capture_output=True, text=True)

    files: set[Path] = set()
    if result.returncode not in (0, 1):
        message = f"rg failed for {identifier}: {result.stderr}"
        raise RuntimeError(message)

    for line in result.stdout.splitlines():
        if not line:
            continue
        file_part = line.split(":", 1)[0]
        files.add(Path(file_part))

    return files


def main() -> None:
    """Parse the schema and report any columns with zero code references."""
    tables = parse_schema(SCHEMA_PATH)

    unused: dict[str, list[str]] = defaultdict(list)

    for table, columns in tables.items():
        for column in columns:
            matches = search_usage(column)
            if not matches:
                unused[table].append(column)

    if not unused:
        print("All columns have at least one usage outside the schema file.")
        return

    print("Columns with no references (excluding schema file):")
    for table, columns in unused.items():
        print(f"\nTable: {table}")
        for column in columns:
            print(f"  - {column}")

    sys.exit(1)


if __name__ == "__main__":
    main()
