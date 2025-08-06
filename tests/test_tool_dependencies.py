"""Test that all registered tools can be instantiated and have their dependencies available."""

import pytest

from mindroom.tools import TOOL_REGISTRY, get_tool_by_name

# Tools that require configuration to instantiate
TOOLS_REQUIRING_CONFIG = {
    "github": "Requires GITHUB_ACCESS_TOKEN environment variable",
    "telegram": "Requires chat_id parameter",
    "email": "Requires SMTP configuration",
    "googlesearch": "Requires Google API credentials",
    "tavily": "Requires TAVILY_API_KEY environment variable",
}


def test_all_tools_can_be_imported():
    """Test that all registered tools can be imported and instantiated."""
    successful = []
    config_required = []
    failed = []

    for tool_name in TOOL_REGISTRY:
        try:
            tool_instance = get_tool_by_name(tool_name)
            assert tool_instance is not None
            assert hasattr(tool_instance, "name")
            successful.append(tool_name)
            print(f"✓ {tool_name}")
        except Exception as e:
            if tool_name in TOOLS_REQUIRING_CONFIG:
                config_required.append(tool_name)
                print(f"⚠ {tool_name}: {TOOLS_REQUIRING_CONFIG[tool_name]}")
            else:
                failed.append((tool_name, str(e)))
                print(f"✗ {tool_name}: {e}")

    # Summary
    print("\nSummary:")
    print(f"  Successful: {len(successful)}")
    print(f"  Config required: {len(config_required)}")
    print(f"  Failed: {len(failed)}")

    # Fail the test if any tools failed (excluding config-required ones)
    if failed:
        error_msg = "\nThe following tools failed:\n"
        for tool_name, error in failed:
            error_msg += f"  - {tool_name}: {error}\n"
        pytest.fail(error_msg)
