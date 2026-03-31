"""Test tool metadata and generate JSON for dashboard consumption."""

import inspect
import json
from pathlib import Path

import pytest
from agno.tools import Toolkit

# Import tools to trigger tool registration
import mindroom.tools  # noqa: F401
from mindroom.constants import resolve_runtime_paths
from mindroom.tool_system.metadata import (
    _TOOL_REGISTRY,
    AUTHORED_OVERRIDE_INHERIT,
    TOOL_METADATA,
    ConfigField,
    ToolCategory,
    ToolConfigOverrideError,
    ToolManagedInitArg,
    export_tools_metadata,
    get_tool_by_name,
    register_tool_with_metadata,
    validate_authored_overrides,
)
from mindroom.tool_system.worker_routing import ResolvedWorkerTarget, resolve_worker_target

_BASE_TOOL_REGISTRY = _TOOL_REGISTRY.copy()
_BASE_TOOL_METADATA = TOOL_METADATA.copy()
_SKIP_PARALLEL_FACTORY_IMPORTS = {"daytona", "openbb"}


def _restore_builtin_tool_metadata_state() -> None:
    """Reset tool registries to the built-in metadata snapshot."""
    _TOOL_REGISTRY.clear()
    _TOOL_REGISTRY.update(_BASE_TOOL_REGISTRY)
    TOOL_METADATA.clear()
    TOOL_METADATA.update(_BASE_TOOL_METADATA)


def test_export_tools_metadata_json() -> None:
    """Export tool metadata to JSON file for dashboard consumption.

    This test generates a JSON file that the dashboard backend can read directly,
    avoiding the need to import the entire mindroom.tools module at runtime.
    """
    output_path = Path(__file__).parent.parent / "src/mindroom/tools_metadata.json"
    _restore_builtin_tool_metadata_state()

    tools = export_tools_metadata()

    # Write the JSON file
    output_path.parent.mkdir(exist_ok=True)
    content = json.dumps({"tools": tools}, indent=2, sort_keys=True)
    output_path.write_text(content + "\n", encoding="utf-8")

    # Verify it was created and is valid
    assert output_path.exists()
    with output_path.open() as f:
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
        if tool_name in _SKIP_PARALLEL_FACTORY_IMPORTS:
            continue
        metadata = TOOL_METADATA[tool_name]
        tool_class = tool_factory()
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
