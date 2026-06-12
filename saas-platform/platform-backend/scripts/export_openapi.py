"""Export the platform backend OpenAPI schema to openapi.json.

The frontend generates its typed API client from this file
(`saas-platform/platform-frontend`, `bun run generate:api`).
Regenerate both with `just saas-openapi` after changing routes or models.

An optional argument overrides the output path (used by the schema-freshness test).
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

from main import app

DEFAULT_OUTPUT_PATH = Path(__file__).parent.parent / "openapi.json"


def main() -> None:
    """Write the app's OpenAPI schema as deterministic, pretty-printed JSON."""
    output_path = Path(sys.argv[1]) if len(sys.argv) > 1 else DEFAULT_OUTPUT_PATH
    schema = app.openapi()
    output_path.write_text(json.dumps(schema, indent=2, sort_keys=True) + "\n")
    print(f"Wrote {output_path}")


if __name__ == "__main__":
    main()
