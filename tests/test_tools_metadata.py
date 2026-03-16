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
    TOOL_METADATA,
    ToolCategory,
    ToolManagedInitArg,
    export_tools_metadata,
    get_tool_by_name,
    register_tool_with_metadata,
)


def test_export_tools_metadata_json() -> None:
    """Export tool metadata to JSON file for dashboard consumption.

    This test generates a JSON file that the dashboard backend can read directly,
    avoiding the need to import the entire mindroom.tools module at runtime.
    """
    output_path = Path(__file__).parent.parent / "src/mindroom/tools_metadata.json"

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


def test_registered_tools_declare_managed_init_args_for_explicit_constructor_inputs() -> None:
    """Built-in tools must opt in explicitly instead of relying on hidden constructor inference."""
    managed_arg_names = {managed_arg.value for managed_arg in ToolManagedInitArg}

    for tool_name, tool_factory in _TOOL_REGISTRY.items():
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
            worker_scope: object,
            routing_agent_name: object,
        ) -> None:
            self.runtime_paths = runtime_paths
            self.worker_scope = worker_scope
            self.routing_agent_name = routing_agent_name
            super().__init__(name=tool_name, tools=[])

    @register_tool_with_metadata(
        name=tool_name,
        display_name="Explicit Runtime Tool",
        description="Test-only toolkit for explicit constructor contract coverage.",
        category=ToolCategory.DEVELOPMENT,
        managed_init_args=(
            ToolManagedInitArg.RUNTIME_PATHS,
            ToolManagedInitArg.WORKER_SCOPE,
            ToolManagedInitArg.ROUTING_AGENT_NAME,
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
        tool = get_tool_by_name(
            tool_name,
            runtime_paths,
            worker_scope="shared",
            routing_agent_name="general",
        )
        assert isinstance(tool, ExplicitRuntimeToolkit)
        assert tool.runtime_paths == runtime_paths
        assert tool.worker_scope == "shared"
        assert tool.routing_agent_name == "general"
    finally:
        _TOOL_REGISTRY.pop(tool_name, None)
        TOOL_METADATA.pop(tool_name, None)
