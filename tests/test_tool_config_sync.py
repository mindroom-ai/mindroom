"""Test that ConfigField definitions match actual tool parameters from agno."""

import inspect
from typing import Union, get_args, get_origin

import pytest

# Import tools to ensure they're registered
import mindroom.tools  # noqa: F401
from mindroom.tools_metadata import TOOL_METADATA, TOOL_REGISTRY

SKIP_CUSTOM = {"homeassistant", "imdb", "gmail", "google_calendar", "google_sheets"}


@pytest.mark.parametrize("tool_name", list(TOOL_REGISTRY.keys()))
def test_all(tool_name: str) -> None:
    """Test that all tools have matching ConfigFields and agno parameters."""
    if tool_name in SKIP_CUSTOM:
        pytest.skip(f"{tool_name} is a custom tool, skipping test")
    tool_factory = TOOL_REGISTRY[tool_name]
    try:
        tool_class = tool_factory()
    except NotImplementedError:
        pytest.skip(f"{tool_name} tool is not implemented, skipping test")
    verify_tool_configfields(tool_name, tool_class)


def verify_tool_configfields(tool_name: str, tool_class: type) -> None:  # noqa: C901, PLR0912, PLR0915
    """Verify tool ConfigFields match agno tool parameters.

    Args:
        tool_name: Name of the tool in the registry
        tool_class: The agno tool class to check against

    """
    # Get the actual parameters from agno
    sig = inspect.signature(tool_class.__init__)  # type: ignore[misc]
    agno_params = {}

    for name, param in sig.parameters.items():
        if name == "self":
            continue
        # Skip **kwargs as it's for forward compatibility
        if param.kind == inspect.Parameter.VAR_KEYWORD:
            continue
        agno_params[name] = {
            "type": param.annotation if param.annotation != inspect.Parameter.empty else None,
        }

    # Get our ConfigFields for the tool
    tool_metadata = TOOL_METADATA[tool_name]

    config_fields = tool_metadata.config_fields or []
    config_field_map = {field.name: field for field in config_fields}

    # Check parameter names
    agno_param_names = set(agno_params.keys())
    config_field_names = set(config_field_map.keys())

    missing_fields = agno_param_names - config_field_names
    extra_fields = config_field_names - agno_param_names

    # Build error message if there are issues
    errors = []
    if missing_fields:
        errors.append(f"Missing ConfigFields for agno parameters: {', '.join(sorted(missing_fields))}")
    if extra_fields:
        errors.append(f"Extra ConfigFields not in agno: {', '.join(sorted(extra_fields))}")

    # Check types for matching parameters
    type_mismatches = []
    for param_name, param_info in agno_params.items():
        if param_name not in config_field_map:
            continue

        field = config_field_map[param_name]
        param_type = param_info["type"]

        # Handle Optional types
        actual_type = param_type
        if get_origin(param_type) is Union:
            args = get_args(param_type)
            if type(None) in args:
                # It's Optional, get the actual type
                actual_type = next(arg for arg in args if arg is not type(None))

        if actual_type is bool:
            expected_type = "boolean"
        elif actual_type is int or actual_type is float:
            expected_type = "number"
        elif actual_type is str:
            # String parameters - check name patterns for special types
            if (
                "token" in param_name.lower()
                or "password" in param_name.lower()
                or "secret" in param_name.lower()
                or "key" in param_name.lower()
            ):
                expected_type = "password"
            elif (
                "url" in param_name.lower()
                or "uri" in param_name.lower()
                or "proxy" in param_name.lower()
                or "endpoint" in param_name.lower()
                or "host" in param_name.lower()
            ):
                expected_type = "url"
            else:
                expected_type = "text"
        else:
            # For Any or other types, we can't determine automatically
            continue

        if field.type != expected_type:
            type_mismatches.append(
                f"{param_name}: expected type '{expected_type}' (from {param_type}), got '{field.type}'",
            )

    if type_mismatches:
        errors.append("Type mismatches:\n  " + "\n  ".join(type_mismatches))

    # Assert no errors
    if errors:
        error_msg = "\n\n".join(errors)
        pytest.fail(f"{tool_name} ConfigField validation failed:\n{error_msg}")

    # Success message (will only show with -v flag)
    print(f"\n✅ All {len(config_fields)} {tool_name} ConfigFields match agno parameter names and types!")
