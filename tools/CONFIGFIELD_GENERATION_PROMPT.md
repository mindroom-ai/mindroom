# Prompt for Generating Tool ConfigField Definitions

## Task
Generate ConfigField definitions for the [TOOL_NAME] tool from the agno library and create a SEPARATE module file. This is part of migrating tools from the monolithic `__init__.py` file into individual, dedicated modules.

**CRITICAL**: Create a NEW file at `src/mindroom/tools/[tool_name].py` - DO NOT modify `__init__.py`

## Instructions

1. Analyze the `agno.tools.[TOOL_MODULE].[TOOL_CLASS]` class (see `.venv/lib/python3.13/site-packages/agno/tools`)
2. Determine tool metadata from agno docs structure:
   - **Category**: Infer from the agno docs URL path: `https://docs.agno.com/tools/toolkits/[CATEGORY]/[tool_name]`
     - `local/` → `ToolCategory.DEVELOPMENT`
     - `email/` → `ToolCategory.EMAIL`
     - `communication/` → `ToolCategory.COMMUNICATION`
     - `research/` → `ToolCategory.RESEARCH`
     - `productivity/` → `ToolCategory.PRODUCTIVITY`
     - `integrations/` → `ToolCategory.INTEGRATIONS`
     - `others/` → `ToolCategory.DEVELOPMENT` (fallback)
   - **Status**: Determine based on configuration requirements:
     - If tool requires API keys, tokens, or authentication → `ToolStatus.REQUIRES_CONFIG`
     - If tool works without configuration → `ToolStatus.AVAILABLE`
   - **Setup Type**: Based on authentication method:
     - API key parameters (access_token, api_key, etc.) → `SetupType.API_KEY`
     - OAuth-based tools → `SetupType.OAUTH`
     - No authentication needed → `SetupType.NONE`
     - Special setup (like Google tools) → `SetupType.SPECIAL`
3. Extract ALL parameters from the `__init__` method (except `self` and `**kwargs`)
4. For each parameter, create a ConfigField with:
   - `name`: Exact parameter name from agno
   - `label`: Human-readable label (title case with spaces)
   - `type`: Map Python types as follows:
     - `bool` → `"boolean"`
     - `int` or `float` → `"number"`
     - `str` → Check parameter name:
       - If contains "token", "password", "secret", "key", "api_key" → `"password"`
       - If contains "url", "uri", "endpoint", "host" → `"url"`
       - Otherwise → `"text"`
     - For Optional types, use the underlying type
   - `required`: Set to `False` for Optional parameters, `True` otherwise
   - `default`: Use the actual default value from agno
   - `placeholder`: Add helpful placeholder for user input (optional)
   - `description`: Clear description of what the parameter does

## Available Tool Categories

Available `ToolCategory` values:
- `EMAIL` - Email services (Gmail, Outlook, etc.)
- `SHOPPING` - E-commerce and shopping tools
- `ENTERTAINMENT` - Media and entertainment services
- `SOCIAL` - Social media platforms
- `DEVELOPMENT` - Development tools, local utilities
- `RESEARCH` - Academic and research tools
- `INFORMATION` - Information lookup services
- `PRODUCTIVITY` - Productivity and office tools
- `COMMUNICATION` - Communication platforms
- `INTEGRATIONS` - Integration services
- `SMART_HOME` - Smart home and IoT tools

## Category Mapping Examples

Based on agno docs URLs:
- `calculator` → `local/calculator` → `ToolCategory.DEVELOPMENT`
- `gmail` → `email/gmail` → `ToolCategory.EMAIL`
- `slack` → `communication/slack` → `ToolCategory.COMMUNICATION`
- `arxiv` → `research/arxiv` → `ToolCategory.RESEARCH`
- `github` → `others/github` → `ToolCategory.DEVELOPMENT`
- `wikipedia` → `research/wikipedia` → `ToolCategory.RESEARCH`

## Output Format

**CRITICAL**: Follow the EXACT pattern from `src/mindroom/tools/github.py` - use the decorator pattern, NOT a class.

```python
"""[Tool name] tool configuration."""

from __future__ import annotations

from typing import TYPE_CHECKING

from mindroom.tools_metadata import (
    ConfigField,
    SetupType,
    ToolCategory,
    ToolStatus,
    register_tool_with_metadata,
)

if TYPE_CHECKING:
    from agno.tools.[module] import [ToolClass]


@register_tool_with_metadata(
    name="[tool_name]",
    display_name="[Tool Display Name]",
    description="[What this tool does]",
    category=ToolCategory.[CATEGORY],  # Derived from docs URL
    status=ToolStatus.[STATUS],  # REQUIRES_CONFIG or AVAILABLE
    setup_type=SetupType.[SETUP_TYPE],  # API_KEY, OAUTH, NONE, or SPECIAL
    icon="[IconName]",  # React icon name (e.g., FaGithub, Mail, Calculator)
    icon_color="text-[color]-[shade]",  # Tailwind color class
    config_fields=[
        # Authentication/Connection parameters first
        ConfigField(
            name="[exact_param_name]",
            label="[Human Readable Label]",
            type="[type]",
            required=[True/False],
            default=[default_value],
            placeholder="[example_value]",
            description="[Clear description of the parameter]",
        ),
        # Then feature flags/boolean parameters grouped by functionality
        # Group 1: [Description of group]
        ConfigField(
            name="[exact_param_name]",
            label="[Human Readable Label]",
            type="boolean",
            required=False,
            default=[True/False],
            description="Enable [what it enables]",
        ),
        # Continue for ALL parameters...
    ],
    dependencies=["[pip-package-name]"],  # From agno requirements
    docs_url="https://docs.agno.com/tools/toolkits/[category]/[tool_name]",
)
def [tool_name]_tools() -> type[[ToolClass]]:
    """Return [tool description]."""
    from agno.tools.[module] import [ToolClass]  # noqa: PLC0415

    return [ToolClass]
```

## Example Analysis Process

For a parameter like `api_key: Optional[str] = None`:
- name: "api_key"
- label: "API Key"
- type: "password" (contains "key")
- required: False (it's Optional)
- default: None
- placeholder: "sk-..."
- description: "API key for authentication (can also be set via [ENV_VAR_NAME] env var)"

For a parameter like `enable_search: bool = True`:
- name: "enable_search"
- label: "Enable Search"
- type: "boolean"
- required: False (has default)
- default: True
- description: "Enable search functionality"

## Important Notes

1. **EVERY** parameter from the agno tool MUST have a corresponding ConfigField
2. Parameter names must match EXACTLY (including underscores)
3. Group related boolean flags together with comments
4. Put authentication/connection parameters first
5. Use the actual default values from agno, not made-up values
6. The test `verify_tool_configfields("[tool_name]", [ToolClass])` must pass

## Verification

After generation, this test should pass:
```python
from mindroom.tests.test_tool_config_sync import verify_tool_configfields
from agno.tools.[module] import [ToolClass]

verify_tool_configfields("[tool_name]", [ToolClass])
```

This ensures:
- All parameter names match exactly
- All types are correctly mapped
- No missing or extra parameters
