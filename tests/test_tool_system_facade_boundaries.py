"""Regression tests for removing umbrella tool-system facades."""

from __future__ import annotations

import ast
import tomllib
from pathlib import Path

import mindroom.tool_system.catalog as catalog_module

REPO_ROOT = Path(__file__).resolve().parents[1]
SOURCE_ROOT = REPO_ROOT / "src" / "mindroom"
TACH_CONFIG = REPO_ROOT / "tach.toml"
FORBIDDEN_FACADE_MODULES = {
    "mindroom.tool_system.extensions",
    "mindroom.tool_system.runtime",
}
PRIVATE_REGISTRY_STATE_INTERFACE_VISIBILITY = {
    "mindroom.mcp.registry",
    "mindroom.tool_system.metadata",
    "mindroom.tool_system.plugins",
}
ALLOWED_PUBLIC_CATALOG_REEXPORTS = {
    "TOOL_METADATA",
    "ToolMetadataValidationError",
}
EXPECTED_PUBLIC_CATALOG_SYMBOLS = [
    "TOOL_METADATA",
    "ConfigField",
    "SetupType",
    "ToolAuthoredOverrideValidator",
    "ToolCategory",
    "ToolConfigOverrideError",
    "ToolInitOverrideError",
    "ToolMetadata",
    "ToolMetadataValidationError",
    "ToolStatus",
    "ToolValidationInfo",
    "apply_authored_overrides",
    "authored_tool_overrides_to_runtime",
    "default_worker_routed_tools",
    "deserialize_tool_validation_snapshot",
    "ensure_tool_registry_loaded",
    "export_tools_metadata",
    "get_tool_by_name",
    "normalize_authored_tool_overrides",
    "resolved_tool_metadata_for_runtime",
    "resolved_tool_validation_snapshot_for_runtime",
    "sanitize_tool_init_overrides",
    "serialize_tool_validation_snapshot",
    "validate_authored_tool_entry_overrides",
]


def _load_tach_config() -> dict[str, object]:
    with TACH_CONFIG.open("rb") as f:
        return tomllib.load(f)


def _tach_module_depends_on(module_path: str) -> set[str]:
    config = _load_tach_config()
    matching_modules = [module for module in config["modules"] if module["path"] == module_path]
    assert len(matching_modules) == 1
    return set(matching_modules[0].get("depends_on", []))


def _tach_interface_exposes(
    *,
    from_modules: tuple[str, ...],
    visibility: set[str] | None = None,
) -> list[str]:
    config = _load_tach_config()
    matching_interfaces = []
    for interface in config["interfaces"]:
        if tuple(interface["from"]) != from_modules:
            continue
        interface_visibility = set(interface.get("visibility", []))
        if visibility is None and interface_visibility:
            continue
        if visibility is not None and interface_visibility != visibility:
            continue
        matching_interfaces.append(list(interface["expose"]))
    assert len(matching_interfaces) == 1
    return matching_interfaces[0]


def _private_registry_state_exports() -> set[str]:
    private_interface = set(
        _tach_interface_exposes(
            from_modules=("mindroom.tool_system.registry_state",),
            visibility=PRIVATE_REGISTRY_STATE_INTERFACE_VISIBILITY,
        ),
    )
    assert private_interface
    return private_interface - ALLOWED_PUBLIC_CATALOG_REEXPORTS


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


def _runtime_direct_facade_imports(py_path: Path, importer_module: str) -> set[str]:
    tree = ast.parse(py_path.read_text())
    imports: set[str] = set()

    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            resolved_module = _resolve_import_from_module(importer_module, node)
            if resolved_module in FORBIDDEN_FACADE_MODULES:
                imports.add(resolved_module)
        elif isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name in FORBIDDEN_FACADE_MODULES:
                    imports.add(alias.name)

    return imports


def _facade_importers() -> dict[str, set[str]]:
    importers: dict[str, set[str]] = {}
    for py_path in SOURCE_ROOT.rglob("*.py"):
        importer_module = f"mindroom.{py_path.relative_to(SOURCE_ROOT).with_suffix('').as_posix().replace('/', '.')}"
        imports = _runtime_direct_facade_imports(py_path, importer_module)
        if imports:
            importers[importer_module] = imports
    return importers


def _catalog_export_names() -> set[str]:
    return set(catalog_module.__all__)


def _catalog_private_importers() -> dict[str, set[str]]:
    forbidden_catalog_exports = _private_registry_state_exports()
    importers: dict[str, set[str]] = {}
    for py_path in SOURCE_ROOT.rglob("*.py"):
        importer_module = f"mindroom.{py_path.relative_to(SOURCE_ROOT).with_suffix('').as_posix().replace('/', '.')}"
        tree = ast.parse(py_path.read_text())
        imported_symbols: set[str] = set()
        for node in ast.walk(tree):
            if (
                isinstance(node, ast.ImportFrom)
                and _resolve_import_from_module(importer_module, node) == "mindroom.tool_system.catalog"
            ):
                for alias in node.names:
                    if alias.name in forbidden_catalog_exports:
                        imported_symbols.add(alias.name)
        if imported_symbols:
            importers[importer_module] = imported_symbols
    return importers


def _private_registry_state_importers_outside_tool_system() -> set[tuple[str, str]]:
    importers: set[tuple[str, str]] = set()
    for py_path in SOURCE_ROOT.rglob("*.py"):
        if py_path.is_relative_to(SOURCE_ROOT / "tool_system"):
            continue
        importer_module = f"mindroom.{py_path.relative_to(SOURCE_ROOT).with_suffix('').as_posix().replace('/', '.')}"
        tree = ast.parse(py_path.read_text())
        for node in ast.walk(tree):
            if (
                isinstance(node, ast.ImportFrom)
                and _resolve_import_from_module(importer_module, node) == "mindroom.tool_system.registry_state"
            ):
                for alias in node.names:
                    if alias.name.startswith("_"):
                        importers.add((importer_module, alias.name))
            elif isinstance(node, ast.Import):
                for alias in node.names:
                    if alias.name == "mindroom.tool_system.registry_state":
                        importers.add((importer_module, alias.asname or alias.name.rsplit(".", maxsplit=1)[-1]))
    return importers


def _imported_module_names(node: ast.AST) -> set[str]:
    if isinstance(node, ast.Import):
        return {alias.name for alias in node.names}
    if isinstance(node, ast.ImportFrom) and node.module is not None:
        return {node.module}
    return set()


def _collect_nested_imports(node: ast.AST) -> set[str]:
    imports = _imported_module_names(node)
    if imports:
        return imports

    nested_imports: set[str] = set()
    for child in ast.iter_child_nodes(node):
        nested_imports.update(_collect_nested_imports(child))
    return nested_imports


def _function_local_imports(py_path: Path) -> set[str]:
    tree = ast.parse(py_path.read_text())
    local_imports: set[str] = set()

    for node in tree.body:
        if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef):
            local_imports.update(_collect_nested_imports(node))
        elif isinstance(node, ast.ClassDef):
            for child in node.body:
                if isinstance(child, ast.FunctionDef | ast.AsyncFunctionDef):
                    local_imports.update(_collect_nested_imports(child))

    return local_imports


def test_tool_system_runtime_and_extensions_modules_are_removed() -> None:
    """The runtime and extensions facade modules should no longer exist."""
    assert not (SOURCE_ROOT / "tool_system" / "runtime.py").exists()
    assert not (SOURCE_ROOT / "tool_system" / "extensions.py").exists()


def test_tool_system_runtime_and_extensions_have_no_importers() -> None:
    """Production modules should import narrow tool-system modules directly."""
    assert not _facade_importers()


def test_tach_does_not_expose_removed_tool_system_facades() -> None:
    """Tach should not keep stale references to the removed facade modules."""
    config = _load_tach_config()
    module_paths = {module["path"] for module in config["modules"]}
    interface_modules = {module_name for interface in config["interfaces"] for module_name in interface["from"]}
    depended_on_modules = {dependency for module in config["modules"] for dependency in module.get("depends_on", [])}
    visible_modules = {
        dependency for interface in config["interfaces"] for dependency in interface.get("visibility", [])
    }

    assert FORBIDDEN_FACADE_MODULES.isdisjoint(module_paths)
    assert FORBIDDEN_FACADE_MODULES.isdisjoint(interface_modules)
    assert FORBIDDEN_FACADE_MODULES.isdisjoint(depended_on_modules)
    assert FORBIDDEN_FACADE_MODULES.isdisjoint(visible_modules)


def test_catalog_does_not_export_private_registry_helpers() -> None:
    """The public catalog seam should not expose mutable registry internals."""
    forbidden_catalog_exports = _private_registry_state_exports()
    assert forbidden_catalog_exports.isdisjoint(_catalog_export_names())


def test_catalog_module_namespace_does_not_leak_private_registry_helpers() -> None:
    """The public catalog module should not expose private registry helpers as attributes."""
    for forbidden in _private_registry_state_exports():
        assert not hasattr(catalog_module, forbidden), f"{forbidden} leaked into catalog module namespace"


def test_catalog_exports_expected_public_surface() -> None:
    """The public catalog seam should export exactly the approved surface."""
    assert set(catalog_module.__all__) == set(EXPECTED_PUBLIC_CATALOG_SYMBOLS)
    assert catalog_module.__all__ == EXPECTED_PUBLIC_CATALOG_SYMBOLS


def test_catalog_private_registry_helpers_have_no_importers() -> None:
    """Production modules should not import private registry helpers through catalog."""
    assert not _catalog_private_importers()


def test_tach_does_not_expose_catalog_private_registry_helpers() -> None:
    """Tach should not publish catalog private registry helpers as public interfaces."""
    forbidden_catalog_exports = _private_registry_state_exports()
    catalog_interface_exposes = set(_tach_interface_exposes(from_modules=("mindroom.tool_system.catalog",)))
    metadata_visible_to_catalog = set(
        _tach_interface_exposes(
            from_modules=("mindroom.tool_system.metadata",),
            visibility={"mindroom.tool_system.catalog"},
        ),
    )
    bootstrap_visible_to_catalog = set(
        _tach_interface_exposes(
            from_modules=("mindroom.tool_system.bootstrap",),
            visibility={"mindroom.tool_system.catalog"},
        ),
    )

    assert forbidden_catalog_exports.isdisjoint(catalog_interface_exposes)
    assert forbidden_catalog_exports.isdisjoint(metadata_visible_to_catalog)
    assert forbidden_catalog_exports.isdisjoint(bootstrap_visible_to_catalog)


def test_private_registry_state_import_is_whitelisted_outside_tool_system() -> None:
    """Only the MCP registry may import private registry state directly outside tool_system."""
    assert _private_registry_state_importers_outside_tool_system() == {
        ("mindroom.mcp.registry", "_TOOL_REGISTRY"),
    }


def test_tach_catalog_interface_matches_catalog_public_surface() -> None:
    """The public tach interface should stay in lockstep with catalog.__all__."""
    catalog_interface_exposes = _tach_interface_exposes(from_modules=("mindroom.tool_system.catalog",))
    assert set(catalog_interface_exposes) == set(catalog_module.__all__)
    assert catalog_interface_exposes == catalog_module.__all__


def test_tach_breaks_metadata_plugins_cycle_with_private_split_modules() -> None:
    """Tach should model the split private ownership modules instead of a metadata/plugins cycle."""
    metadata_deps = _tach_module_depends_on("mindroom.tool_system.metadata")
    plugins_deps = _tach_module_depends_on("mindroom.tool_system.plugins")
    catalog_deps = _tach_module_depends_on("mindroom.tool_system.catalog")
    bootstrap_deps = _tach_module_depends_on("mindroom.tool_system.bootstrap")

    assert "mindroom.tool_system.plugins" not in metadata_deps
    assert "mindroom.tool_system.metadata" not in plugins_deps
    assert "mindroom.tool_system.registry_state" in metadata_deps
    assert "mindroom.tool_system.plugin_imports" in metadata_deps
    assert "mindroom.tool_system.registry_state" in plugins_deps
    assert "mindroom.tool_system.plugin_imports" in plugins_deps
    assert catalog_deps == {"mindroom.tool_system.bootstrap", "mindroom.tool_system.metadata"}
    assert bootstrap_deps == {"mindroom.mcp.registry", "mindroom.tool_system.plugins"}


def test_selected_modules_do_not_hide_tool_system_imports_inside_functions() -> None:
    """Standalone tool-system helpers should be imported at module scope once the cycle is split."""
    assert "mindroom.tool_system.worker_routing" not in _function_local_imports(SOURCE_ROOT / "credentials.py")
    assert "mindroom.tool_system.plugin_identity" not in _function_local_imports(
        SOURCE_ROOT / "hooks" / "context.py",
    )
    assert "mindroom.tool_system.catalog" not in _function_local_imports(SOURCE_ROOT / "mcp" / "manager.py")
