"""Architecture boundary tests for configuration and Matrix identity modules."""

from __future__ import annotations

import ast
from pathlib import Path

CONFIG_MODULES = tuple(Path("src/mindroom/config").glob("*.py"))
PRODUCTION_MODULES = tuple(Path("src/mindroom").rglob("*.py"))
MATRIX_IDENTITY_MODULE = Path("src/mindroom/matrix/identity.py")
MATRIX_NAMING_HELPERS = frozenset(
    {
        "agent_username_localpart",
        "extract_server_name_from_homeserver",
        "managed_room_alias_localpart",
        "managed_room_key_from_alias_localpart",
        "managed_space_alias_localpart",
        "mindroom_namespace",
        "room_alias_localpart",
    },
)


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


def test_matrix_identity_does_not_reexport_naming_helpers() -> None:
    """Matrix identity owns Matrix IDs; pure naming helpers live in matrix_naming."""
    tree = ast.parse(MATRIX_IDENTITY_MODULE.read_text(encoding="utf-8"))
    exported_names: set[str] = set()
    direct_naming_imports: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module == "mindroom.matrix_naming":
            direct_naming_imports.extend(alias.name for alias in node.names if alias.name in MATRIX_NAMING_HELPERS)
        if not isinstance(node, ast.Assign):
            continue
        if not any(isinstance(target, ast.Name) and target.id == "__all__" for target in node.targets):
            continue
        if isinstance(node.value, ast.List):
            exported_names.update(item.value for item in node.value.elts if isinstance(item, ast.Constant))

    assert sorted(direct_naming_imports) == []
    assert sorted(exported_names & MATRIX_NAMING_HELPERS) == []


def test_production_code_imports_naming_helpers_from_matrix_naming() -> None:
    """Callers use the neutral naming module instead of Matrix identity compatibility exports."""
    forbidden: list[str] = []
    for source_path in PRODUCTION_MODULES:
        if source_path == MATRIX_IDENTITY_MODULE:
            continue
        tree = ast.parse(source_path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if not isinstance(node, ast.ImportFrom) or node.module != "mindroom.matrix.identity":
                continue
            imported_helpers = sorted(alias.name for alias in node.names if alias.name in MATRIX_NAMING_HELPERS)
            if imported_helpers:
                forbidden.append(f"{source_path}:{node.lineno}: {', '.join(imported_helpers)}")

    assert forbidden == []
