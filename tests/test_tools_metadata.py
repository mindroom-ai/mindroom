"""Test tool metadata and generate JSON for widget consumption."""

import json
from pathlib import Path

# Import tools to trigger tool registration
import mindroom.tools  # noqa: F401
from mindroom.tool_system.metadata import TOOL_METADATA, export_tools_metadata


def test_export_tools_metadata_json() -> None:
    """Export tool metadata to JSON file for widget consumption.

    This test generates a JSON file that the widget backend can read directly,
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
