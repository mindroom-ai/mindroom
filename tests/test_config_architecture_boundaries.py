"""Architecture boundary tests for configuration modules."""

from __future__ import annotations

import ast
from pathlib import Path

CONFIG_MODULES = tuple(Path("src/mindroom/config").glob("*.py"))


def _is_matrix_runtime_module(module: str) -> bool:
    return module == "mindroom.matrix" or module.startswith("mindroom.matrix.")


def test_config_modules_do_not_import_matrix_runtime_modules() -> None:
    """Config models stay authored-data focused and avoid Matrix runtime imports."""
    forbidden: list[str] = []
    for source_path in CONFIG_MODULES:
        tree = ast.parse(source_path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and node.module and _is_matrix_runtime_module(node.module):
                forbidden.append(f"{source_path}:{node.lineno}: from {node.module}")
            if isinstance(node, ast.Import):
                forbidden.extend(
                    f"{source_path}:{node.lineno}: import {alias.name}"
                    for alias in node.names
                    if _is_matrix_runtime_module(alias.name)
                )

    assert forbidden == []
