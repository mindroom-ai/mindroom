"""Test tool metadata and generate JSON for widget consumption."""

import json
from dataclasses import asdict
from pathlib import Path

# Import tools to trigger tool registration
import mindroom.tools  # noqa: F401
from mindroom.tools_metadata import TOOL_METADATA


def test_export_tools_metadata_json() -> None:
    """Export tool metadata to JSON file for widget consumption.

    This test generates a JSON file that the widget backend can read directly,
    avoiding the need to import the entire mindroom.tools module at runtime.
    """
    output_path = Path(__file__).parent.parent / "mindroom/tools_metadata.json"

    tools = []
    for metadata in TOOL_METADATA.values():
        # Convert dataclass to dict
        tool_dict = asdict(metadata)

        # Convert enums to strings for JSON serialization
        tool_dict["category"] = metadata.category.value
        tool_dict["status"] = metadata.status.value
        tool_dict["setup_type"] = metadata.setup_type.value

        # Remove non-serializable fields
        tool_dict.pop("factory", None)  # Callable is not JSON serializable

        tools.append(tool_dict)

    # Sort for consistent output
    tools.sort(key=lambda t: (t["category"], t["name"]))

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

    print(f"✓ Exported {len(data['tools'])} tools to {output_path}")


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
