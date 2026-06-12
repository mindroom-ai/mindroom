"""Export the platform backend OpenAPI schema to openapi.json.

The frontend generates its typed API client from this file
(`saas-platform/platform-frontend`, `bun run generate:api`).
Regenerate both with `just saas-openapi` after changing routes or models.
"""

from __future__ import annotations

import json
from pathlib import Path

from main import app

OUTPUT_PATH = Path(__file__).parent.parent / "openapi.json"


def main() -> None:
    """Write the app's OpenAPI schema as deterministic, pretty-printed JSON."""
    schema = app.openapi()
    OUTPUT_PATH.write_text(json.dumps(schema, indent=2, sort_keys=True) + "\n")
    print(f"Wrote {OUTPUT_PATH}")


if __name__ == "__main__":
    main()
