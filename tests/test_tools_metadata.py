"""Test tool metadata JSON snapshot for dashboard consumption."""

import inspect
import json
import sys
from pathlib import Path
from types import ModuleType
from typing import Never

import pytest
from agno.tools import Toolkit

import mindroom.tool_system.metadata as metadata_module

# Import tools to trigger tool registration
import mindroom.tools  # noqa: F401
from mindroom.config.main import Config, load_config
from mindroom.constants import resolve_runtime_paths
from mindroom.tool_system.metadata import (
    _PLUGIN_MODULE_PREFIX,
    _TOOL_REGISTRY,
    AUTHORED_OVERRIDE_INHERIT,
    TOOL_METADATA,
    ConfigField,
    ToolAuthoredOverrideValidator,
    ToolCategory,
    ToolConfigOverrideError,
    ToolManagedInitArg,
    _capture_tool_registry_snapshot,
    _execute_validation_plugin_module,
    _restore_tool_registry_snapshot,
    deserialize_tool_validation_snapshot,
    ensure_tool_registry_loaded,
    export_tools_metadata,
    get_tool_by_name,
    register_tool_with_metadata,
    resolved_tool_validation_snapshot_for_runtime,
    serialize_tool_validation_snapshot,
    validate_authored_overrides,
)
from mindroom.tool_system.worker_routing import ResolvedWorkerTarget, resolve_worker_target

_BASE_TOOL_REGISTRY = _TOOL_REGISTRY.copy()
_BASE_TOOL_METADATA = TOOL_METADATA.copy()
_SKIP_PARALLEL_FACTORY_IMPORTS = {"daytona", "openbb"}
_OPTIONAL_TOOL_IMPORTS = frozenset({"telegram"})


def _restore_builtin_tool_metadata_state() -> None:
    """Reset tool registries to the built-in metadata snapshot."""
    _TOOL_REGISTRY.clear()
    _TOOL_REGISTRY.update(_BASE_TOOL_REGISTRY)
    TOOL_METADATA.clear()
    TOOL_METADATA.update(_BASE_TOOL_METADATA)


def test_export_tools_metadata_json() -> None:
    """Verify committed tool metadata JSON matches the current registry export."""
    output_path = Path(__file__).parent.parent / "src/mindroom/tools_metadata.json"
    _restore_builtin_tool_metadata_state()

    committed_content = output_path.read_text(encoding="utf-8")
    tools = export_tools_metadata()
    expected_content = json.dumps({"tools": tools}, indent=2, sort_keys=True) + "\n"

    assert committed_content == expected_content, (
        "tools_metadata.json is out of date, regenerate it with "
        './.venv/bin/python -c "import json; import mindroom.tools; '
        "from pathlib import Path; "
        "from mindroom.tool_system.metadata import export_tools_metadata; "
        "Path('src/mindroom/tools_metadata.json').write_text("
        "json.dumps({'tools': export_tools_metadata()}, indent=2, sort_keys=True) + '\\n', "
        "encoding='utf-8')\""
    )

    with output_path.open(encoding="utf-8") as f:
        data = json.load(f)
        assert "tools" in data
        assert len(data["tools"]) > 0

        # Verify structure of first tool
        first_tool = data["tools"][0]
        required_fields = ["name", "display_name", "description", "category", "status", "setup_type"]
        for field in required_fields:
            assert field in first_tool, f"Missing required field: {field}"
        assert "managed_init_args" not in first_tool


def test_export_tools_metadata_json_resets_leaked_registry_entries() -> None:
    """Export should ignore temporary registry contamination from earlier tests."""
    tool_name = "test_leaked_tool"

    class LeakedTool(Toolkit):
        def __init__(self) -> None:
            super().__init__(name="leaked", tools=[])

    @register_tool_with_metadata(
        name=tool_name,
        display_name="Leaked Tool",
        description="Temporary leaked tool metadata",
        category=ToolCategory.DEVELOPMENT,
    )
    def leaked_tool_factory() -> type[Toolkit]:
        return LeakedTool

    try:
        assert tool_name in TOOL_METADATA

        _restore_builtin_tool_metadata_state()

        exported_names = {tool["name"] for tool in export_tools_metadata()}
        assert tool_name not in exported_names
    finally:
        _TOOL_REGISTRY.pop(tool_name, None)
        TOOL_METADATA.pop(tool_name, None)
        _restore_builtin_tool_metadata_state()


def test_plugin_validation_uses_sys_modules_snapshot(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Plugin validation should snapshot sys.modules before iterating over it."""

    class SnapshotOnlyModules(dict[str, ModuleType]):
        def items(self) -> Never:
            msg = "live sys.modules.items() should not be used"
            raise RuntimeError(msg)

        def copy(self) -> dict[str, ModuleType]:
            return dict(self)

    plugin_root = tmp_path / "plugins" / "demo"
    plugin_root.mkdir(parents=True)
    module_path = plugin_root / "tools.py"
    module_path.write_text("VALUE = 1\n", encoding="utf-8")

    snapshot_modules = SnapshotOnlyModules(sys.modules.copy())
    monkeypatch.setattr(metadata_module.sys, "modules", snapshot_modules)

    snapshot = _capture_tool_registry_snapshot()
    assert isinstance(snapshot.plugin_modules, dict)

    module_name = _execute_validation_plugin_module("demo", plugin_root, module_path, {})
    assert module_name
    assert "demo" in module_name
    assert "__validation__" in module_name


def test_restore_tool_registry_snapshot_uses_sys_modules_snapshot(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Restoring registry state should snapshot sys.modules before iterating over it."""

    class SnapshotOnlyModules(dict[str, ModuleType]):
        def __iter__(self) -> Never:
            msg = "live sys.modules iteration should not be used"
            raise RuntimeError(msg)

        def copy(self) -> dict[str, ModuleType]:
            return dict(self)

    snapshot = _capture_tool_registry_snapshot()
    leaked_module_name = f"{_PLUGIN_MODULE_PREFIX}leaked"
    assert leaked_module_name not in snapshot.plugin_modules

    leaked_module = ModuleType(leaked_module_name)
    leaked_module.__file__ = str(tmp_path / "leaked.py")

    snapshot_modules = SnapshotOnlyModules(sys.modules.copy())
    snapshot_modules[leaked_module_name] = leaked_module
    monkeypatch.setattr(metadata_module.sys, "modules", snapshot_modules)

    _restore_tool_registry_snapshot(snapshot)

    assert leaked_module_name not in metadata_module.sys.modules


def test_tool_metadata_consistency() -> None:
    """Verify that all tool metadata is properly configured."""
    for tool_name, metadata in TOOL_METADATA.items():
        # Check that all required fields are present
        assert metadata.name == tool_name, f"Tool name mismatch: {tool_name} != {metadata.name}"
        assert metadata.display_name, f"Tool {tool_name} missing display_name"
        assert metadata.description, f"Tool {tool_name} missing description"
        assert metadata.category, f"Tool {tool_name} missing category"
        assert metadata.status, f"Tool {tool_name} missing status"
        assert metadata.setup_type, f"Tool {tool_name} missing setup_type"


def test_tool_metadata_does_not_advertise_env_var_fallbacks() -> None:
    """Tool metadata should describe explicit config, not resurrect env fallback docs."""
    forbidden_phrases = (
        "falls back to",
        "can also be set via",
    )

    for tool_name, metadata in TOOL_METADATA.items():
        text_snippets = [metadata.description, metadata.helper_text]
        text_snippets.extend(field.description for field in metadata.config_fields or [])

        for text in filter(None, text_snippets):
            lowered = text.lower()
            assert not any(phrase in lowered for phrase in forbidden_phrases), (
                f"Tool metadata for {tool_name} still advertises env fallback: {text}"
            )


@pytest.mark.timeout(180)
def test_registered_tools_declare_managed_init_args_for_explicit_constructor_inputs() -> None:
    """Built-in tools must opt in explicitly instead of relying on hidden constructor inference."""
    managed_arg_names = {managed_arg.value for managed_arg in ToolManagedInitArg}

    for tool_name, tool_factory in _TOOL_REGISTRY.items():
        metadata = TOOL_METADATA[tool_name]
        if tool_name in _SKIP_PARALLEL_FACTORY_IMPORTS:
            continue
        try:
            tool_class = tool_factory()
        except ImportError as exc:
            if tool_name in _OPTIONAL_TOOL_IMPORTS:
                continue
            msg = f"Unexpected ImportError while loading tool {tool_name}: {exc}"
            pytest.fail(msg)
        init_signature = inspect.signature(tool_class.__init__)
        constructor_param_names = {name for name in init_signature.parameters if name != "self"}
        expected_managed_args = tuple(
            managed_arg for managed_arg in ToolManagedInitArg if managed_arg.value in constructor_param_names
        )
        assert metadata.managed_init_args == expected_managed_args, (
            f"{tool_name} declares constructor inputs "
            f"{sorted(constructor_param_names & managed_arg_names)} but metadata lists "
            f"{[managed_arg.value for managed_arg in metadata.managed_init_args]}"
        )

    for tool_name, metadata in TOOL_METADATA.items():
        if tool_name not in _TOOL_REGISTRY:
            assert metadata.managed_init_args == (), (
                f"{tool_name} is metadata-only and should not declare managed init args: "
                f"{[managed_arg.value for managed_arg in metadata.managed_init_args]}"
            )


def test_get_tool_by_name_does_not_infer_hidden_constructor_kwargs(tmp_path: Path) -> None:
    """Undeclared MindRoom-managed kwargs should not be inferred from parameter names."""
    tool_name = "test_hidden_runtime_tool"

    class HiddenRuntimeToolkit(Toolkit):
        def __init__(self, *, runtime_paths: object) -> None:
            self.runtime_paths = runtime_paths
            super().__init__(name=tool_name, tools=[])

    @register_tool_with_metadata(
        name=tool_name,
        display_name="Hidden Runtime Tool",
        description="Test-only toolkit for constructor contract coverage.",
        category=ToolCategory.DEVELOPMENT,
    )
    def _hidden_runtime_tool_factory() -> type[HiddenRuntimeToolkit]:
        return HiddenRuntimeToolkit

    runtime_paths = resolve_runtime_paths(
        config_path=tmp_path / "config.yaml",
        storage_path=tmp_path / "storage",
        process_env={},
    )

    try:
        with pytest.raises(TypeError, match="runtime_paths"):
            get_tool_by_name(
                tool_name,
                runtime_paths,
                runtime_overrides={"runtime_paths": runtime_paths},
                worker_target=None,
            )
    finally:
        _TOOL_REGISTRY.pop(tool_name, None)
        TOOL_METADATA.pop(tool_name, None)


def test_get_tool_by_name_passes_declared_managed_init_args(tmp_path: Path) -> None:
    """Declared MindRoom-managed kwargs should reach the constructor directly."""
    tool_name = "test_explicit_runtime_tool"

    class ExplicitRuntimeToolkit(Toolkit):
        def __init__(
            self,
            *,
            runtime_paths: object,
            worker_target: object,
        ) -> None:
            self.runtime_paths = runtime_paths
            self.worker_target = worker_target
            super().__init__(name=tool_name, tools=[])

    @register_tool_with_metadata(
        name=tool_name,
        display_name="Explicit Runtime Tool",
        description="Test-only toolkit for explicit constructor contract coverage.",
        category=ToolCategory.DEVELOPMENT,
        managed_init_args=(
            ToolManagedInitArg.RUNTIME_PATHS,
            ToolManagedInitArg.WORKER_TARGET,
        ),
    )
    def _explicit_runtime_tool_factory() -> type[ExplicitRuntimeToolkit]:
        return ExplicitRuntimeToolkit

    runtime_paths = resolve_runtime_paths(
        config_path=tmp_path / "config.yaml",
        storage_path=tmp_path / "storage",
        process_env={},
    )

    try:
        worker_target = resolve_worker_target(
            "shared",
            "general",
            execution_identity=None,
            tenant_id=runtime_paths.env_value("CUSTOMER_ID"),
            account_id=runtime_paths.env_value("ACCOUNT_ID"),
        )
        tool = get_tool_by_name(
            tool_name,
            runtime_paths,
            worker_target=worker_target,
        )
        assert isinstance(tool, ExplicitRuntimeToolkit)
        assert tool.runtime_paths == runtime_paths
        assert tool.worker_target == ResolvedWorkerTarget(
            worker_scope="shared",
            routing_agent_name="general",
            execution_identity=None,
            tenant_id=None,
            account_id=None,
            worker_key=None,
        )
    finally:
        _TOOL_REGISTRY.pop(tool_name, None)
        TOOL_METADATA.pop(tool_name, None)


def test_validate_authored_overrides_accepts_declared_field_types_and_nulls() -> None:
    """Authored overrides should accept declared scalar types and optional nulls."""
    tool_name = "test_authored_override_tool"

    class _FakeToolkit(Toolkit):
        def __init__(self, **_kwargs: object) -> None:
            super().__init__(name=tool_name, tools=[])

    @register_tool_with_metadata(
        name=tool_name,
        display_name="Authored Override Tool",
        description="Test-only toolkit for authored override validation.",
        category=ToolCategory.DEVELOPMENT,
        config_fields=[
            ConfigField(name="enabled", label="Enabled", type="boolean", required=False),
            ConfigField(name="count", label="Count", type="number", required=False),
            ConfigField(name="label", label="Label", type="text", required=False),
            ConfigField(name="endpoint", label="Endpoint", type="url", required=False),
        ],
    )
    def _fake_tool_factory() -> type[_FakeToolkit]:
        return _FakeToolkit

    try:
        assert validate_authored_overrides(
            tool_name,
            {
                "enabled": True,
                "count": 3.5,
                "label": None,
                "endpoint": "https://example.com",
            },
            config_path_prefix="agents.code.tools[0]",
        ) == {
            "enabled": True,
            "count": 3.5,
            "label": None,
            "endpoint": "https://example.com",
        }
    finally:
        _TOOL_REGISTRY.pop(tool_name, None)
        TOOL_METADATA.pop(tool_name, None)


def test_validate_authored_overrides_accepts_inherit_sentinel_for_required_fields() -> None:
    """The inherit sentinel should be allowed even when the field itself is required."""
    tool_name = "test_authored_override_inherit_required"

    class _FakeToolkit(Toolkit):
        def __init__(self, **_kwargs: object) -> None:
            super().__init__(name=tool_name, tools=[])

    @register_tool_with_metadata(
        name=tool_name,
        display_name="Authored Override Inherit Required",
        description="Test-only toolkit for inherit sentinel coverage.",
        category=ToolCategory.DEVELOPMENT,
        config_fields=[
            ConfigField(name="workspace_id", label="Workspace ID", type="text", required=True),
        ],
    )
    def _fake_tool_factory() -> type[_FakeToolkit]:
        return _FakeToolkit

    try:
        assert validate_authored_overrides(
            tool_name,
            {"workspace_id": AUTHORED_OVERRIDE_INHERIT},
            config_path_prefix="agents.code.tools[0]",
        ) == {"workspace_id": AUTHORED_OVERRIDE_INHERIT}
    finally:
        _TOOL_REGISTRY.pop(tool_name, None)
        TOOL_METADATA.pop(tool_name, None)


def test_validate_authored_overrides_accepts_string_lists_for_text_fields_with_agent_override_arrays() -> None:
    """Text config fields may accept list-form values when the agent override schema exposes string arrays."""
    tool_name = "test_authored_override_string_array_compat"

    class _FakeToolkit(Toolkit):
        def __init__(self, **_kwargs: object) -> None:
            super().__init__(name=tool_name, tools=[])

    @register_tool_with_metadata(
        name=tool_name,
        display_name="Authored Override String Array Compat",
        description="Test-only toolkit for string-array compatibility coverage.",
        category=ToolCategory.DEVELOPMENT,
        config_fields=[
            ConfigField(name="patterns", label="Patterns", type="text", required=False),
        ],
        agent_override_fields=[
            ConfigField(name="patterns", label="Patterns", type="string[]", required=False),
        ],
    )
    def _fake_tool_factory() -> type[_FakeToolkit]:
        return _FakeToolkit

    try:
        assert validate_authored_overrides(
            tool_name,
            {"patterns": ["GITEA_*", "WHISPER_URL"]},
            config_path_prefix="agents.code.tools[0]",
        ) == {"patterns": "GITEA_*, WHISPER_URL"}
    finally:
        _TOOL_REGISTRY.pop(tool_name, None)
        TOOL_METADATA.pop(tool_name, None)


def test_validate_authored_overrides_rejects_bad_types_and_password_fields() -> None:
    """Authored overrides should reject bad types, runtime-only fields, and password fields."""
    tool_name = "test_authored_override_errors"

    class _FakeToolkit(Toolkit):
        def __init__(self, **_kwargs: object) -> None:
            super().__init__(name=tool_name, tools=[])

    @register_tool_with_metadata(
        name=tool_name,
        display_name="Authored Override Errors",
        description="Test-only toolkit for override error coverage.",
        category=ToolCategory.DEVELOPMENT,
        config_fields=[
            ConfigField(name="flag", label="Flag", type="boolean", required=False),
            ConfigField(name="base_dir", label="Base Dir", type="text", required=False, authored_override=False),
            ConfigField(name="api_key", label="API Key", type="password", required=False),
        ],
    )
    def _fake_tool_factory() -> type[_FakeToolkit]:
        return _FakeToolkit

    try:
        with pytest.raises(
            ToolConfigOverrideError,
            match=r"agents.code.tools\[0\].test_authored_override_errors.flag",
        ):
            validate_authored_overrides(
                tool_name,
                {"flag": "yes"},
                config_path_prefix="agents.code.tools[0]",
            )

        with pytest.raises(ToolConfigOverrideError, match="authored overrides are not allowed for this field"):
            validate_authored_overrides(
                tool_name,
                {"base_dir": "/workspace"},
                config_path_prefix="agents.code.tools[0]",
            )

        with pytest.raises(ToolConfigOverrideError, match="password fields"):
            validate_authored_overrides(
                tool_name,
                {"api_key": "sk-test"},
                config_path_prefix="agents.code.tools[0]",
            )

        with pytest.raises(ToolConfigOverrideError, match="unknown authored override field"):
            validate_authored_overrides(
                tool_name,
                {"missing": True},
                config_path_prefix="agents.code.tools[0]",
            )
    finally:
        _TOOL_REGISTRY.pop(tool_name, None)
        TOOL_METADATA.pop(tool_name, None)


def test_tool_validation_snapshot_round_trips_mcp_override_validation(tmp_path: Path) -> None:
    """Validation snapshots should preserve explicit MCP override-validator semantics."""
    runtime_paths = resolve_runtime_paths(config_path=tmp_path / "config.yaml")
    config = Config.model_validate(
        {
            "models": {
                "default": {
                    "provider": "openai",
                    "id": "gpt-5.4",
                },
            },
            "agents": {},
            "router": {"model": "default"},
            "mcp_servers": {
                "demo": {
                    "transport": "stdio",
                    "command": "npx",
                    "args": ["-y", "@modelcontextprotocol/server-everything"],
                },
            },
        },
        context={"runtime_paths": runtime_paths},
    )

    snapshot = resolved_tool_validation_snapshot_for_runtime(runtime_paths, config)
    payload = serialize_tool_validation_snapshot(snapshot)
    restored_snapshot = deserialize_tool_validation_snapshot(payload)

    assert restored_snapshot["mcp_demo"].authored_override_validator == ToolAuthoredOverrideValidator.MCP
    assert restored_snapshot["mcp_demo"].agent_override_fields is not None


def test_deserialize_tool_validation_snapshot_rejects_non_boolean_runtime_loadable() -> None:
    """Validation snapshot payloads should type-check runtime_loadable strictly."""
    with pytest.raises(TypeError, match="runtime_loadable to a boolean"):
        deserialize_tool_validation_snapshot(
            {
                "shell": {
                    "config_fields": [],
                    "agent_override_fields": [],
                    "authored_override_validator": "default",
                    "runtime_loadable": "yes",
                },
            },
        )


def test_get_tool_by_name_rejects_invalid_mcp_assignment_overrides(tmp_path: Path) -> None:
    """Direct tool construction must enforce the same MCP-specific override rules as config loading."""
    config_path = tmp_path / "config.yaml"
    config_path.write_text(
        "models:\n"
        "  default:\n"
        "    provider: openai\n"
        "    id: gpt-5.4\n"
        "router:\n"
        "  model: default\n"
        "mcp_servers:\n"
        "  demo:\n"
        "    transport: stdio\n"
        "    command: python\n"
        "    args:\n"
        "      - -c\n"
        "      - print(0)\n"
        "agents:\n"
        "  code:\n"
        "    display_name: Code\n"
        "    role: test\n"
        "    model: default\n"
        "    tools:\n"
        "      - mcp_demo\n",
        encoding="utf-8",
    )
    runtime_paths = resolve_runtime_paths(config_path=config_path, storage_path=tmp_path / "storage")
    config = load_config(runtime_paths)
    ensure_tool_registry_loaded(runtime_paths, config)

    with pytest.raises(ToolConfigOverrideError, match="include_tools and exclude_tools overlap"):
        get_tool_by_name(
            "mcp_demo",
            runtime_paths,
            tool_config_overrides={
                "include_tools": ["echo"],
                "exclude_tools": ["echo"],
            },
            disable_sandbox_proxy=True,
            worker_target=None,
        )


def test_secret_like_config_fields_are_marked_password() -> None:
    """Secret-like tool config fields should be declared as password inputs."""
    suspicious_suffixes = ("_api_key", "_password", "_secret", "_token")
    suspicious_exact = {
        "api_key",
        "password",
        "secret",
        "token",
        "access_token",
        "refresh_token",
        "auth_token",
        "bearer_token",
    }

    for tool_name, metadata in TOOL_METADATA.items():
        for field in metadata.config_fields or []:
            lowered = field.name.lower()
            if "url" in lowered or lowered.endswith("_id") or lowered == "client_id":
                continue
            if lowered in suspicious_exact or lowered.endswith(suspicious_suffixes):
                assert field.type == "password", f"{tool_name}.{field.name} should use type='password'"
