"""Architecture boundary tests for configuration and Matrix identity modules."""

from __future__ import annotations

import ast
from pathlib import Path

CONFIG_MODULES = tuple(Path("src/mindroom/config").glob("*.py"))
PRODUCTION_MODULES = tuple(Path("src/mindroom").rglob("*.py"))
MATRIX_IDENTITY_MODULE = Path("src/mindroom/matrix/identity.py")
LEGACY_MATRIX_NAMING_MODULE = Path("src/mindroom/matrix_naming.py")
CONCRETE_ORCHESTRATOR_IMPORT_ALLOWLIST = {
    Path("src/mindroom/orchestrator.py"),
}
RUNTIME_PROTOCOLS_MODULE = Path("src/mindroom/runtime_protocols.py")
CONFIG_MAIN_MODULE = Path("src/mindroom/config/main.py")
APPROVAL_CONFIG_MODULE = Path("src/mindroom/config/approval.py")
MATRIX_MESSAGE_TOOL_MODULE = Path("src/mindroom/custom_tools/matrix_message.py")
RESPONSE_RUNNER_MODULE = Path("src/mindroom/response_runner.py")
RESPONSE_LIFECYCLE_MODULE = Path("src/mindroom/response_lifecycle.py")
KNOWLEDGE_AVAILABILITY_NOTICE_OWNER_MODULES = {
    Path("src/mindroom/agent_run_context.py"),
    Path("src/mindroom/knowledge/__init__.py"),
    Path("src/mindroom/knowledge/utils.py"),
}
MATRIX_MESSAGE_LOW_LEVEL_IMPORTS = frozenset(
    {
        "mindroom.custom_tools.attachments",
        "mindroom.interactive",
        "mindroom.matrix.client_delivery",
        "mindroom.matrix.client_thread_history",
        "mindroom.matrix.client_visible_messages",
        "mindroom.matrix.mentions",
    },
)
MATRIX_IDENTIFIER_HELPERS = frozenset(
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


def test_tool_approval_config_uses_pydantic_validation_only() -> None:
    """Tool approval config should not duplicate Pydantic with raw pre-validation."""
    forbidden_names = {
        "validate_raw_tool_approval_config",
        "_validate_tool_approval_default",
        "_validate_tool_approval_rule",
        "_validate_positive_timeout_days",
        "_coerce_positive_float",
    }
    found: list[str] = []

    for source_path in (APPROVAL_CONFIG_MODULE, CONFIG_MAIN_MODULE):
        tree = ast.parse(source_path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.FunctionDef) and node.name in forbidden_names:
                found.append(f"{source_path}:{node.lineno}: def {node.name}")
            if isinstance(node, ast.ImportFrom):
                found.extend(
                    f"{source_path}:{node.lineno}: import {alias.name}"
                    for alias in node.names
                    if alias.name in forbidden_names
                )
            if isinstance(node, ast.Name) and node.id in forbidden_names:
                found.append(f"{source_path}:{node.lineno}: {node.id}")
            if isinstance(node, ast.Attribute) and node.attr in forbidden_names:
                found.append(f"{source_path}:{node.lineno}: {node.attr}")

    assert found == []


def test_matrix_identity_does_not_reexport_identifier_helpers() -> None:
    """Matrix identity owns Matrix IDs; pure identifier helpers live in matrix_identifiers."""
    tree = ast.parse(MATRIX_IDENTITY_MODULE.read_text(encoding="utf-8"))
    exported_names: set[str] = set()
    direct_naming_imports: list[str] = []
    public_identifier_module_imports: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module == "mindroom":
            public_identifier_module_imports.extend(
                alias.name
                for alias in node.names
                if alias.name == "matrix_identifiers" and not (alias.asname or "").startswith("_")
            )
        if isinstance(node, ast.ImportFrom) and node.module == "mindroom.matrix_identifiers":
            direct_naming_imports.extend(alias.name for alias in node.names if alias.name in MATRIX_IDENTIFIER_HELPERS)
        if not isinstance(node, ast.Assign):
            continue
        if not any(isinstance(target, ast.Name) and target.id == "__all__" for target in node.targets):
            continue
        if isinstance(node.value, ast.List):
            exported_names.update(item.value for item in node.value.elts if isinstance(item, ast.Constant))

    assert public_identifier_module_imports == []
    assert sorted(direct_naming_imports) == []
    assert sorted(exported_names & MATRIX_IDENTIFIER_HELPERS) == []


def test_production_code_imports_identifier_helpers_from_matrix_identifiers() -> None:
    """Callers use the neutral identifier module instead of Matrix identity compatibility exports."""
    forbidden: list[str] = []
    for source_path in PRODUCTION_MODULES:
        if source_path == MATRIX_IDENTITY_MODULE:
            continue
        tree = ast.parse(source_path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if not isinstance(node, ast.ImportFrom) or node.module != "mindroom.matrix.identity":
                continue
            imported_helpers = sorted(alias.name for alias in node.names if alias.name in MATRIX_IDENTIFIER_HELPERS)
            if imported_helpers:
                forbidden.append(f"{source_path}:{node.lineno}: {', '.join(imported_helpers)}")

    assert forbidden == []


def test_matrix_naming_compatibility_module_does_not_exist() -> None:
    """Pure Matrix identifier helpers live in matrix_identifiers with no legacy re-export module."""
    forbidden: list[str] = []
    if LEGACY_MATRIX_NAMING_MODULE.exists():
        forbidden.append(str(LEGACY_MATRIX_NAMING_MODULE))

    for source_path in PRODUCTION_MODULES:
        tree = ast.parse(source_path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and node.module == "mindroom.matrix_naming":
                forbidden.append(f"{source_path}:{node.lineno}: from {node.module}")
            if isinstance(node, ast.Import):
                forbidden.extend(
                    f"{source_path}:{node.lineno}: import {alias.name}"
                    for alias in node.names
                    if alias.name == "mindroom.matrix_naming"
                )

    assert forbidden == []


def test_runtime_collaborators_do_not_import_concrete_orchestrator() -> None:
    """Collaborators depend on a narrow runtime protocol instead of MultiAgentOrchestrator."""
    forbidden: list[str] = []
    for source_path in PRODUCTION_MODULES:
        if source_path in CONCRETE_ORCHESTRATOR_IMPORT_ALLOWLIST:
            continue
        tree = ast.parse(source_path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if not isinstance(node, ast.ImportFrom) or node.module != "mindroom.orchestrator":
                continue
            if any(alias.name == "MultiAgentOrchestrator" for alias in node.names):
                forbidden.append(f"{source_path}:{node.lineno}: from {node.module} import MultiAgentOrchestrator")

    assert forbidden == []


def test_orchestrator_runtime_protocol_exposes_only_public_members() -> None:
    """The orchestrator runtime protocol is the public cross-module contract."""
    tree = ast.parse(RUNTIME_PROTOCOLS_MODULE.read_text(encoding="utf-8"))
    protocol_class = next(
        node for node in ast.walk(tree) if isinstance(node, ast.ClassDef) and node.name == "OrchestratorRuntime"
    )

    private_members = [
        node.name
        for node in protocol_class.body
        if isinstance(node, ast.FunctionDef) and node.name.startswith("_") and not node.name.startswith("__")
    ]

    assert private_members == []


def test_matrix_message_tool_uses_conversation_operations_boundary() -> None:
    """The model-facing Matrix message tool delegates protocol behavior below the tool adapter."""
    forbidden: list[str] = []
    tree = ast.parse(MATRIX_MESSAGE_TOOL_MODULE.read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module in MATRIX_MESSAGE_LOW_LEVEL_IMPORTS:
            forbidden.append(f"{MATRIX_MESSAGE_TOOL_MODULE}:{node.lineno}: from {node.module}")
        if isinstance(node, ast.Import):
            forbidden.extend(
                f"{MATRIX_MESSAGE_TOOL_MODULE}:{node.lineno}: import {alias.name}"
                for alias in node.names
                if alias.name in MATRIX_MESSAGE_LOW_LEVEL_IMPORTS
            )

    assert forbidden == []


def test_response_runner_delegates_lifecycle_coordination() -> None:
    """ResponseRunner should delegate lock and queued-turn state to response_lifecycle."""
    tree = ast.parse(RESPONSE_RUNNER_MODULE.read_text(encoding="utf-8"))
    forbidden_names = {
        "_QueuedMessageState",
        "_get_or_create_queued_signal",
        "_response_lifecycle_lock",
        "_response_lifecycle_locks",
        "_should_signal_queued_message",
        "_thread_queued_signals",
    }
    found: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.ClassDef | ast.FunctionDef | ast.AsyncFunctionDef) and node.name in forbidden_names:
            found.append(f"{RESPONSE_RUNNER_MODULE}:{node.lineno}: {node.name}")
        if isinstance(node, ast.Name) and node.id in forbidden_names:
            found.append(f"{RESPONSE_RUNNER_MODULE}:{node.lineno}: {node.id}")

    assert found == []


def test_response_lifecycle_does_not_reach_into_response_runner_internals() -> None:
    """Response lifecycle owns its dependencies instead of calling runner-private helpers."""
    tree = ast.parse(RESPONSE_LIFECYCLE_MODULE.read_text(encoding="utf-8"))
    forbidden_private_helpers = {
        "_emit_pipeline_timing_summary",
        "_emit_session_started_safely",
        "_log_post_response_effects_failure",
        "_response_outcome",
        "_should_watch_session_started",
    }
    found: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            imported_from_response_runner = (
                node.level == 1 and node.module == "response_runner"
            ) or node.module == "mindroom.response_runner"
            if imported_from_response_runner:
                found.append(f"{RESPONSE_LIFECYCLE_MODULE}:{node.lineno}: from response_runner")
        if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef) and node.name in forbidden_private_helpers:
            found.append(f"{RESPONSE_LIFECYCLE_MODULE}:{node.lineno}: {node.name}")
        if isinstance(node, ast.Name) and node.id in {"ResponseRequest", "ResponseRunner", *forbidden_private_helpers}:
            found.append(f"{RESPONSE_LIFECYCLE_MODULE}:{node.lineno}: {node.id}")
        if isinstance(node, ast.Attribute) and node.attr in forbidden_private_helpers:
            found.append(f"{RESPONSE_LIFECYCLE_MODULE}:{node.lineno}: {node.attr}")

    assert found == []


def test_agent_run_context_owns_knowledge_availability_notice_injection() -> None:
    """Agent-run adapters use the shared helper instead of formatting availability notices inline."""
    forbidden: list[str] = []
    for source_path in PRODUCTION_MODULES:
        if source_path in KNOWLEDGE_AVAILABILITY_NOTICE_OWNER_MODULES:
            continue
        tree = ast.parse(source_path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and node.module == "mindroom.knowledge":
                imported_names = [alias.name for alias in node.names]
                if "format_knowledge_availability_notice" in imported_names:
                    forbidden.append(f"{source_path}:{node.lineno}: from {node.module}")
            if isinstance(node, ast.Name) and node.id == "format_knowledge_availability_notice":
                forbidden.append(f"{source_path}:{node.lineno}: {node.id}")

    assert forbidden == []
