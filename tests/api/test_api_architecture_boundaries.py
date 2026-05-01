"""Architecture boundary tests for API modules."""

from __future__ import annotations

import ast
from pathlib import Path


def test_oauth_routes_do_not_import_api_app_entrypoint() -> None:
    """OAuth route handlers must not import the FastAPI app entrypoint."""
    source_path = Path("src/mindroom/api/oauth.py")
    tree = ast.parse(source_path.read_text(encoding="utf-8"))

    forbidden_imports = [
        node for node in ast.walk(tree) if isinstance(node, ast.ImportFrom) and node.module == "mindroom.api.main"
    ]

    assert forbidden_imports == []
