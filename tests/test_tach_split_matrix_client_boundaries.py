"""Regression tests for split Matrix client Tach boundaries."""

from __future__ import annotations

import ast
import shutil
import subprocess
import sys
import tomllib
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
SOURCE_ROOT = REPO_ROOT / "src" / "mindroom"
TACH_CONFIG = REPO_ROOT / "tach.toml"
SPLIT_MATRIX_CLIENT_MODULES = {
    "mindroom.matrix.client_delivery",
    "mindroom.matrix.client_room_admin",
    "mindroom.matrix.client_session",
    "mindroom.matrix.client_thread_history",
    "mindroom.matrix.client_visible_messages",
}


def _load_tach_config() -> dict[str, object]:
    with TACH_CONFIG.open("rb") as f:
        return tomllib.load(f)


def _module_entries_by_path() -> dict[str, dict[str, object]]:
    config = _load_tach_config()
    module_entries: dict[str, dict[str, object]] = {}
    for module in config["modules"]:
        module_entry = dict(module)
        path = module_entry.get("path")
        if isinstance(path, str):
            module_entries[path] = module_entry
    return module_entries


def _resolve_import_from_module(importer_module: str, node: ast.ImportFrom) -> str | None:
    if node.level == 0:
        return node.module

    package_parts = importer_module.split(".")[:-1]
    if node.level > len(package_parts):
        return None

    base_parts = package_parts[: len(package_parts) - node.level + 1]
    if node.module:
        base_parts.extend(node.module.split("."))
    return ".".join(base_parts)


def _is_type_checking_test(test: ast.expr) -> bool:
    if isinstance(test, ast.Name):
        return test.id == "TYPE_CHECKING"
    return (
        isinstance(test, ast.Attribute)
        and isinstance(test.value, ast.Name)
        and test.value.id == "typing"
        and test.attr == "TYPE_CHECKING"
    )


def _record_runtime_split_import(node: ast.AST, importer_module: str, imports: set[str]) -> None:
    if isinstance(node, ast.ImportFrom):
        resolved_module = _resolve_import_from_module(importer_module, node)
        if resolved_module in SPLIT_MATRIX_CLIENT_MODULES:
            imports.add(resolved_module)
        return

    if isinstance(node, ast.Import):
        for alias in node.names:
            if alias.name in SPLIT_MATRIX_CLIENT_MODULES:
                imports.add(alias.name)


def _walk_runtime_nodes(
    node: ast.AST,
    importer_module: str,
    imports: set[str],
    *,
    in_type_checking: bool = False,
) -> None:
    if isinstance(node, ast.If) and _is_type_checking_test(node.test):
        for child in node.body:
            _walk_runtime_nodes(child, importer_module, imports, in_type_checking=True)
        for child in node.orelse:
            _walk_runtime_nodes(child, importer_module, imports, in_type_checking=in_type_checking)
        return

    if not in_type_checking:
        _record_runtime_split_import(node, importer_module, imports)

    for child in ast.iter_child_nodes(node):
        _walk_runtime_nodes(child, importer_module, imports, in_type_checking=in_type_checking)


def _runtime_direct_split_imports(py_path: Path, importer_module: str) -> set[str]:
    tree = ast.parse(py_path.read_text())
    imports: set[str] = set()
    _walk_runtime_nodes(tree, importer_module, imports)
    return imports


def _split_matrix_client_importers() -> dict[str, set[str]]:
    importers: dict[str, set[str]] = {}
    for py_path in SOURCE_ROOT.rglob("*.py"):
        importer_module = f"mindroom.{py_path.relative_to(SOURCE_ROOT).with_suffix('').as_posix().replace('/', '.')}"
        if importer_module in SPLIT_MATRIX_CLIENT_MODULES or importer_module == "mindroom.matrix.client":
            continue
        runtime_imports = _runtime_direct_split_imports(py_path, importer_module)
        if runtime_imports:
            importers[importer_module] = runtime_imports
    return importers


def test_split_matrix_client_importers_have_explicit_tach_modules() -> None:
    """Every runtime direct importer must own an explicit Tach module entry."""
    module_entries = _module_entries_by_path()
    importers = _split_matrix_client_importers()

    missing_module_entries: list[str] = []
    missing_dependencies: list[str] = []
    missing_visibility: list[str] = []

    for importer_module, imported_targets in sorted(importers.items()):
        importer_entry = module_entries.get(importer_module)
        if importer_entry is None:
            missing_module_entries.append(importer_module)
            continue

        depends_on = importer_entry.get("depends_on")
        if not isinstance(depends_on, list):
            missing_dependencies.extend(f"{importer_module} -> {target}" for target in sorted(imported_targets))
            continue

        for target in sorted(imported_targets):
            if target not in depends_on:
                missing_dependencies.append(f"{importer_module} -> {target}")
            target_entry = module_entries.get(target)
            visibility = target_entry.get("visibility") if target_entry is not None else None
            if not isinstance(visibility, list) or importer_module not in visibility:
                missing_visibility.append(f"{target} !<- {importer_module}")

    assert not missing_module_entries, f"Missing explicit Tach modules: {missing_module_entries}"
    assert not missing_dependencies, f"Missing split-client dependencies: {missing_dependencies}"
    assert not missing_visibility, f"Missing split-client module visibility: {missing_visibility}"


def test_tach_rejects_forbidden_split_matrix_client_import(tmp_path: Path) -> None:
    """A new direct split-client import must fail Tach until tach.toml is updated."""
    project_root = tmp_path / "project"
    shutil.copytree(REPO_ROOT / "src", project_root / "src")
    shutil.copy2(TACH_CONFIG, project_root / "tach.toml")

    attachments_path = project_root / "src" / "mindroom" / "custom_tools" / "attachments.py"
    original_text = attachments_path.read_text()
    probe_import = (
        "from mindroom.matrix.client_session import _create_matrix_client as _tach_probe_private_client_session\n"
    )
    attachments_path.write_text(
        original_text.replace(
            "from mindroom.matrix.client_delivery import send_file_message\n",
            f"{probe_import}from mindroom.matrix.client_delivery import send_file_message\n",
        ),
    )

    result = subprocess.run(
        [sys.executable, "-m", "tach", "check", "--dependencies", "--interfaces"],
        cwd=project_root,
        check=False,
        capture_output=True,
        text=True,
    )

    assert result.returncode == 1, result.stdout + result.stderr
    assert "mindroom.matrix.client_session" in (result.stdout + result.stderr)
