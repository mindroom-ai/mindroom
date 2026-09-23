"""Regenerate the configuration schema snapshot that the dashboard tests render.

The dashboard fetches the live schema from ``/api/config/schema``; this
snapshot only lets frontend tests exercise every real config shape.
"""

from __future__ import annotations

import json
from pathlib import Path

from mindroom.config.main import dashboard_config_schema

REPO_ROOT = Path(__file__).resolve().parents[2]
SNAPSHOT_PATH = REPO_ROOT / "frontend" / "src" / "test" / "fixtures" / "config-schema.json"


def main() -> None:
    """Write the snapshot when its content differs from the current schema."""
    schema = dashboard_config_schema()
    # Compare content, not bytes, so the prettier hook's formatting is kept.
    if SNAPSHOT_PATH.exists() and json.loads(SNAPSHOT_PATH.read_text(encoding="utf-8")) == schema:
        return
    SNAPSHOT_PATH.parent.mkdir(parents=True, exist_ok=True)
    SNAPSHOT_PATH.write_text(json.dumps(schema, indent=2) + "\n", encoding="utf-8")


if __name__ == "__main__":
    main()
