"""The committed OpenAPI schema must match the live FastAPI app."""

import json
import subprocess
import sys
from pathlib import Path

BACKEND_DIR = Path(__file__).parent.parent


def test_committed_openapi_schema_is_current(tmp_path: Path) -> None:
    """Exporting the schema in a clean interpreter must reproduce the committed openapi.json.

    A subprocess is required: conftest mocks the slowapi limiter in-process, while the
    committed schema is produced under the real limiter.
    """
    exported_path = tmp_path / "openapi.json"
    subprocess.run(
        [sys.executable, str(BACKEND_DIR / "scripts" / "export_openapi.py"), str(exported_path)],
        check=True,
        cwd=BACKEND_DIR,
        capture_output=True,
    )
    exported = json.loads(exported_path.read_text())
    committed = json.loads((BACKEND_DIR / "openapi.json").read_text())
    assert exported == committed, "openapi.json is stale; regenerate with `just saas-openapi` and commit the result"
