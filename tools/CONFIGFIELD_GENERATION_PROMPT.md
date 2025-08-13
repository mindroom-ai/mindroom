# Prompt for Generating Tool ConfigField Definitions

## Task
Generate ConfigField definitions for the [TOOL_NAME] tool from the agno library and create a SEPARATE module file. This is part of migrating tools from the monolithic `__init__.py` file into individual, dedicated modules.

**CRITICAL**: Create a NEW file at `src/mindroom/tools/[tool_name].py` - DO NOT modify `__init__.py`

## Instructions

1. **Fetch the agno documentation**:
   - First, fetch `https://docs.agno.com/llms.txt` to find the tool's documentation URL
   - Look for the pattern: `- [ToolName](https://docs.agno.com/tools/toolkits/[category]/[tool_name].md)`
   - Fetch the specific tool's documentation page (the .md file) to get parameter descriptions
   - For the `docs_url` field in the code, use the URL WITHOUT the .md extension
   - Use the documentation's parameter descriptions when available (they are more accurate and user-friendly)

2. **Analyze the source code**:
   - Examine `agno.tools.[TOOL_MODULE].[TOOL_CLASS]` class (see `.venv/lib/python3.13/site-packages/agno/tools`)
   - Extract ALL parameters from the `__init__` method
   - Use source code for complete parameter list and default values

3. Determine tool metadata from agno docs structure:
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
4. **Merge documentation with source code analysis**:
   - Use parameter descriptions from the documentation when available
   - Fall back to generating descriptions based on parameter names when not documented
   - Always use the source code for the complete list of parameters

5. **Add dependencies to pyproject.toml**:
   - Check what dependencies the tool requires (from the `dependencies` field in the tool file)
   - Add any missing dependencies to `pyproject.toml` in the main dependencies list
   - Use the format: `"package-name",  # for [Tool Name] tool`
   - Follow the existing pattern in the file with proper comments

6. For each parameter, create a ConfigField with:
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
   - `description`: Use from documentation if available, otherwise create a clear description

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

## Category Mapping from Agno Docs

The correct category mapping based on actual agno documentation structure:
- Tools under `/tools/toolkits/database/` → `ToolCategory.PRODUCTIVITY`
- Tools under `/tools/toolkits/local/` → `ToolCategory.DEVELOPMENT`
- Tools under `/tools/toolkits/models/` → `ToolCategory.DEVELOPMENT`
- Tools under `/tools/toolkits/others/` → `ToolCategory.DEVELOPMENT`
- Tools under `/tools/toolkits/search/` → `ToolCategory.RESEARCH`
- Tools under `/tools/toolkits/social/` → `ToolCategory.COMMUNICATION` (for slack, discord, etc.) or `ToolCategory.EMAIL` (for email, gmail)
- Tools under `/tools/toolkits/web_scrape/` → `ToolCategory.RESEARCH`

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
    docs_url="https://docs.agno.com/tools/toolkits/[category]/[tool_name]",  # URL from llms.txt but WITHOUT .md extension
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

1. **ALWAYS** read `CLAUDE.md` in the project root for project-specific instructions
2. **ALWAYS** fetch and use the agno documentation for accurate parameter descriptions
3. **EVERY** parameter from the agno tool MUST have a corresponding ConfigField
4. Parameter names must match EXACTLY (including underscores)
5. Group related boolean flags together with comments
6. Put authentication/connection parameters first
7. Use the actual default values from agno, not made-up values
8. The test `verify_tool_configfields("[tool_name]", [ToolClass])` must pass
9. For `docs_url`: Use the URL from `llms.txt` but **remove the .md extension**
   - Example: `https://docs.agno.com/tools/toolkits/database/pandas` (not `.../pandas.md`)
10. **Update pyproject.toml dependencies**: Add any new tool dependencies with proper comments

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
