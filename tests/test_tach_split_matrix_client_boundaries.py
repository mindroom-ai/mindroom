"""Regression tests for split Matrix client Tach boundaries."""

from __future__ import annotations

import ast
import importlib
import shutil
import subprocess
import sys
import textwrap
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
RUNTIME_PROTOCOL_MODULE = "mindroom.runtime_protocols"
BOT_RUNTIME_VIEW_MODULE = "mindroom.bot_runtime_view"
RUNTIME_VIEW_STRUCTURAL_MEMBER = "orchestrator"
RUNTIME_PROTOCOL_PRIVATE_SYMBOL = "_check_narrow_protocols_are_subsets_of_bot_runtime_view"
RUNTIME_PROTOCOL_PUBLIC_SYMBOLS = [
    "OrchestratorRuntime",
    "SupportsClientConfig",
    "SupportsClientConfigOrchestrator",
    "SupportsConfig",
    "SupportsConfigOrchestrator",
    "SupportsRunningState",
]
RUNTIME_PROTOCOL_IMPORTERS = {
    "mindroom.bot_room_lifecycle",
    "mindroom.conversation_resolver",
    "mindroom.conversation_state_writer",
    "mindroom.delivery_gateway",
    "mindroom.edit_regenerator",
    "mindroom.hooks.context",
    "mindroom.inbound_turn_normalizer",
    "mindroom.knowledge.utils",
    "mindroom.post_response_effects",
    "mindroom.turn_policy",
}
BOT_RUNTIME_VIEW_ALLOWED_IMPORTERS = {
    "mindroom.bot",
    "mindroom.matrix.cache.thread_reads",
    "mindroom.matrix.cache.thread_write_cache_ops",
    "mindroom.matrix.conversation_cache",
    "mindroom.matrix.thread_bookkeeping",
    "mindroom.response_runner",
    "mindroom.tool_system.runtime_context",
    "mindroom.turn_controller",
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


def _record_runtime_module_import(
    node: ast.AST,
    importer_module: str,
    target_modules: set[str],
    imports: set[str],
) -> None:
    if isinstance(node, ast.ImportFrom):
        resolved_module = _resolve_import_from_module(importer_module, node)
        if resolved_module in target_modules:
            imports.add(resolved_module)
        return

    if isinstance(node, ast.Import):
        for alias in node.names:
            if alias.name in target_modules:
                imports.add(alias.name)


def _walk_runtime_nodes(
    node: ast.AST,
    importer_module: str,
    target_modules: set[str],
    imports: set[str],
    *,
    in_type_checking: bool = False,
) -> None:
    if isinstance(node, ast.If) and _is_type_checking_test(node.test):
        for child in node.body:
            _walk_runtime_nodes(child, importer_module, target_modules, imports, in_type_checking=True)
        for child in node.orelse:
            _walk_runtime_nodes(
                child,
                importer_module,
                target_modules,
                imports,
                in_type_checking=in_type_checking,
            )
        return

    if not in_type_checking:
        _record_runtime_module_import(node, importer_module, target_modules, imports)

    for child in ast.iter_child_nodes(node):
        _walk_runtime_nodes(child, importer_module, target_modules, imports, in_type_checking=in_type_checking)


def _runtime_direct_imports(py_path: Path, importer_module: str, target_modules: set[str]) -> set[str]:
    tree = ast.parse(py_path.read_text())
    imports: set[str] = set()
    _walk_runtime_nodes(tree, importer_module, target_modules, imports)
    return imports


def _runtime_direct_split_imports(py_path: Path, importer_module: str) -> set[str]:
    return _runtime_direct_imports(py_path, importer_module, SPLIT_MATRIX_CLIENT_MODULES)


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


def _runtime_protocol_importers() -> set[str]:
    importers: set[str] = set()
    for py_path in SOURCE_ROOT.rglob("*.py"):
        importer_module = f"mindroom.{py_path.relative_to(SOURCE_ROOT).with_suffix('').as_posix().replace('/', '.')}"
        if importer_module == RUNTIME_PROTOCOL_MODULE:
            continue
        runtime_imports = _runtime_direct_imports(py_path, importer_module, {RUNTIME_PROTOCOL_MODULE})
        if runtime_imports:
            importers.add(importer_module)
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


def test_runtime_protocol_importers_have_explicit_tach_modules() -> None:
    """Every runtime-protocol consumer must be explicit and Tach-governed."""
    module_entries = _module_entries_by_path()
    actual_importers = _runtime_protocol_importers()

    assert actual_importers == RUNTIME_PROTOCOL_IMPORTERS
    assert RUNTIME_PROTOCOL_MODULE in module_entries

    missing_module_entries: list[str] = []
    missing_dependencies: list[str] = []
    missing_visibility: list[str] = []

    runtime_protocol_entry = module_entries[RUNTIME_PROTOCOL_MODULE]
    visibility = runtime_protocol_entry.get("visibility")
    assert isinstance(visibility, list)
    assert set(visibility) == RUNTIME_PROTOCOL_IMPORTERS

    for importer_module in sorted(actual_importers):
        importer_entry = module_entries.get(importer_module)
        if importer_entry is None:
            missing_module_entries.append(importer_module)
            continue

        depends_on = importer_entry.get("depends_on")
        if not isinstance(depends_on, list) or RUNTIME_PROTOCOL_MODULE not in depends_on:
            missing_dependencies.append(f"{importer_module} -> {RUNTIME_PROTOCOL_MODULE}")

        if importer_module not in visibility:
            missing_visibility.append(f"{RUNTIME_PROTOCOL_MODULE} !<- {importer_module}")

    assert not missing_module_entries, f"Missing explicit runtime-protocol modules: {missing_module_entries}"
    assert not missing_dependencies, f"Missing runtime-protocol dependencies: {missing_dependencies}"
    assert not missing_visibility, f"Missing runtime-protocol module visibility: {missing_visibility}"


def test_runtime_protocol_interface_matches_public_symbols() -> None:
    """The runtime protocol facade must expose only the declared public protocols."""
    module_entries = _module_entries_by_path()
    runtime_protocols = importlib.import_module(RUNTIME_PROTOCOL_MODULE)

    assert runtime_protocols.__all__ == RUNTIME_PROTOCOL_PUBLIC_SYMBOLS

    interface_entries = [
        entry for entry in _load_tach_config()["interfaces"] if entry["from"] == [RUNTIME_PROTOCOL_MODULE]
    ]
    assert len(interface_entries) == 1
    assert interface_entries[0]["expose"] == RUNTIME_PROTOCOL_PUBLIC_SYMBOLS
    assert RUNTIME_PROTOCOL_MODULE in module_entries


def test_bot_runtime_view_visibility_is_limited_to_owner_and_protocol_facade() -> None:
    """Only the bot shell, the protocol facade, and core runtime-heavy modules may import the full runtime view."""
    module_entries = _module_entries_by_path()
    entry = module_entries[BOT_RUNTIME_VIEW_MODULE]
    visibility = entry.get("visibility")

    assert isinstance(visibility, list)
    assert set(visibility) == BOT_RUNTIME_VIEW_ALLOWED_IMPORTERS

    runtime_protocol_entry = module_entries[RUNTIME_PROTOCOL_MODULE]
    depends_on = runtime_protocol_entry.get("depends_on")
    assert isinstance(depends_on, list)
    assert BOT_RUNTIME_VIEW_MODULE not in depends_on


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


def test_tach_rejects_forbidden_runtime_protocol_import(tmp_path: Path) -> None:
    """A new runtime-protocol consumer must fail Tach until allowed explicitly."""
    project_root = tmp_path / "project"
    shutil.copytree(REPO_ROOT / "src", project_root / "src")
    shutil.copy2(TACH_CONFIG, project_root / "tach.toml")

    attachments_path = project_root / "src" / "mindroom" / "custom_tools" / "attachments.py"
    original_text = attachments_path.read_text()
    probe_import = "from mindroom.runtime_protocols import SupportsConfig as _tach_probe_runtime_protocol\n"
    attachments_path.write_text(
        original_text.replace(
            "from __future__ import annotations\n\n",
            f"from __future__ import annotations\n\n{probe_import}",
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
    assert RUNTIME_PROTOCOL_MODULE in (result.stdout + result.stderr)


def test_tach_rejects_private_runtime_protocol_import(tmp_path: Path) -> None:
    """An allowed consumer still may not import a private runtime-protocol helper."""
    project_root = tmp_path / "project"
    shutil.copytree(REPO_ROOT / "src", project_root / "src")
    shutil.copy2(TACH_CONFIG, project_root / "tach.toml")

    effects_path = project_root / "src" / "mindroom" / "post_response_effects.py"
    original_text = effects_path.read_text()
    probe_import = (
        "from mindroom.runtime_protocols import "
        "_check_narrow_protocols_are_subsets_of_bot_runtime_view as _tach_probe_private_runtime_protocol\n"
    )
    effects_path.write_text(
        original_text.replace(
            "from mindroom.runtime_protocols import SupportsClientConfig  # noqa: TC001\n",
            f"from mindroom.runtime_protocols import SupportsClientConfig  # noqa: TC001\n{probe_import}",
        ),
    )

    result = subprocess.run(
        [sys.executable, "-m", "tach", "check", "--dependencies", "--interfaces"],
        cwd=project_root,
        check=False,
        capture_output=True,
        text=True,
    )

    output = result.stdout + result.stderr
    assert result.returncode == 1, output
    assert RUNTIME_PROTOCOL_MODULE in output
    assert RUNTIME_PROTOCOL_PRIVATE_SYMBOL in output


def test_tach_rejects_forbidden_bot_runtime_view_import(tmp_path: Path) -> None:
    """A direct full-runtime import must fail Tach outside the owning bot shell."""
    project_root = tmp_path / "project"
    shutil.copytree(REPO_ROOT / "src", project_root / "src")
    shutil.copy2(TACH_CONFIG, project_root / "tach.toml")

    effects_path = project_root / "src" / "mindroom" / "post_response_effects.py"
    original_text = effects_path.read_text()
    probe_import = "from mindroom.bot_runtime_view import BotRuntimeView as _tach_probe_runtime_view\n"
    effects_path.write_text(
        original_text.replace(
            "from __future__ import annotations\n\n",
            f"from __future__ import annotations\n\n{probe_import}",
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
    assert BOT_RUNTIME_VIEW_MODULE in (result.stdout + result.stderr)


def test_ty_rejects_runtime_view_missing_structural_member(tmp_path: Path) -> None:
    """The runtime view must keep members required by the narrow protocol proof."""
    project_root = tmp_path / "project"
    shutil.copytree(REPO_ROOT / "src", project_root / "src")

    runtime_protocols_path = project_root / "src" / "mindroom" / "runtime_protocols.py"
    bot_runtime_view_path = project_root / "src" / "mindroom" / "bot_runtime_view.py"
    original_text = bot_runtime_view_path.read_text()
    missing_member_block = f"\n    @property\n    def {RUNTIME_VIEW_STRUCTURAL_MEMBER}(self) -> OrchestratorRuntime | None: ...  # noqa: D102\n"
    assert missing_member_block in original_text
    bot_runtime_view_path.write_text(original_text.replace(missing_member_block, "\n"))

    helper_path = project_root / "check_runtime_view.py"
    helper_path.write_text(
        textwrap.dedent(
            f"""
            from mindroom.bot_runtime_view import BotRuntimeView
            from mindroom.runtime_protocols import OrchestratorRuntime


            def require_orchestrator(view: BotRuntimeView) -> OrchestratorRuntime | None:
                return view.{RUNTIME_VIEW_STRUCTURAL_MEMBER}
            """,
        ).lstrip(),
    )

    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "ty",
            "check",
            "--project",
            str(project_root),
            str(runtime_protocols_path),
            str(bot_runtime_view_path),
            str(helper_path),
        ],
        cwd=project_root,
        check=False,
        capture_output=True,
        text=True,
    )

    output = result.stdout + result.stderr
    assert result.returncode == 1, output
    assert RUNTIME_VIEW_STRUCTURAL_MEMBER in output
    assert "SupportsConfigOrchestrator" in output
