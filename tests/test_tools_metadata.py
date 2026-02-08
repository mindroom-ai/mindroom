"""Test tool metadata and generate JSON for widget consumption."""

import json
from pathlib import Path

# Import tools to trigger tool registration
import mindroom.tools  # noqa: F401
from mindroom.tools_metadata import TOOL_METADATA, export_tools_metadata


def test_tools_metadata_json_up_to_date() -> None:
    r"""Verify that tools_metadata.json is up to date with the Python source.

    If this test fails, run the following to regenerate:
        python -c "
        import json, mindroom.tools
        from mindroom.tools_metadata import export_tools_metadata
        from pathlib import Path
        p = Path('src/mindroom/tools_metadata.json')
        p.write_text(json.dumps({'tools': export_tools_metadata()}, indent=2, sort_keys=True) + '\n')
        print(f'Wrote {p}')
        "
    """
    output_path = Path(__file__).parent.parent / "src/mindroom/tools_metadata.json"

    tools = export_tools_metadata()
    expected = json.dumps({"tools": tools}, indent=2, sort_keys=True) + "\n"

    assert output_path.exists(), f"{output_path} does not exist. Run the regeneration command in this test's docstring."
    actual = output_path.read_text(encoding="utf-8")

    assert actual == expected, (
        f"{output_path.name} is out of date with the Python source. "
        "Run the regeneration command in this test's docstring."
    )


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
