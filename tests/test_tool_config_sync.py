"""Test that ConfigField definitions match actual tool parameters from agno."""

import inspect

import pytest
from agno.tools.github import GithubTools

# Import tools to ensure they're registered
import mindroom.tools  # noqa: F401
from mindroom.tools_metadata import get_tool_metadata


def test_github_configfields_match_agno_params() -> None:
    """Verify GitHub ConfigFields have all parameter names from agno GithubTools."""
    # Get the actual parameters from agno
    sig = inspect.signature(GithubTools.__init__)
    agno_param_names = set()

    for name, param in sig.parameters.items():
        if name == "self":
            continue
        # Skip **kwargs as it's for forward compatibility
        if param.kind == inspect.Parameter.VAR_KEYWORD:
            continue
        agno_param_names.add(name)

    # Get our ConfigFields for GitHub
    github_metadata = get_tool_metadata("github")
    assert github_metadata is not None, "GitHub tool not found in registry"

    config_fields = github_metadata.config_fields or []
    config_field_names = {field.name for field in config_fields}

    # Check that all agno parameters have corresponding ConfigFields
    missing_fields = agno_param_names - config_field_names
    extra_fields = config_field_names - agno_param_names

    # Build error message if there are issues
    errors = []
    if missing_fields:
        errors.append(f"Missing ConfigFields for agno parameters: {', '.join(sorted(missing_fields))}")
    if extra_fields:
        errors.append(f"Extra ConfigFields not in agno: {', '.join(sorted(extra_fields))}")

    # Assert no errors
    if errors:
        error_msg = "\n\n".join(errors)
        pytest.fail(f"ConfigField validation failed:\n{error_msg}")

    # Success message (will only show with -v flag)
    print(f"\n✅ All {len(config_fields)} GitHub ConfigFields match agno parameter names!")
