---
icon: lucide/plug-2
---

# Plugins

MindRoom plugins add tools and can optionally ship skills. Plugins are loaded from paths listed in `config.yaml`.

## Plugin structure

A plugin is a directory containing `mindroom.plugin.json`:

```
my-plugin/
├── mindroom.plugin.json
├── tools.py
└── skills/
    └── my-skill/
        └── SKILL.md
```

## Manifest format

```json
{
  "name": "my-plugin",
  "tools_module": "tools.py",
  "skills": ["skills"]
}
```

| Field | Type | Description |
| --- | --- | --- |
| `name` | string | Plugin identifier (required) |
| `tools_module` | string | Path to the tools module (optional) |
| `skills` | list of strings | Relative directories containing skills (optional) |

Unknown fields are ignored.

## Configure plugins

Add plugin paths to `config.yaml`:

```yaml
plugins:
  - ./plugins/my-plugin
  - python:my_skill_pack
```

Paths may be:

- Absolute paths
- Paths relative to `config.yaml`
- Python package specs (see below)

## Python package plugins

MindRoom can resolve plugins from installed Python packages:

```yaml
plugins:
  - my_skill_pack
  - python:my_skill_pack
  - pkg:my_skill_pack:plugins/demo
  - module:my_skill_pack:plugins/demo
```

Rules:

- A bare package name is allowed if it contains no slashes.
- `python:`, `pkg:`, and `module:` are explicit prefixes.
- `:sub/path` points to a subdirectory inside the package.

MindRoom resolves the package location and looks for `mindroom.plugin.json` in that directory.

## MCP via plugins (advanced)

MindRoom does not yet support direct MCP server configuration in `config.yaml`.
If you need MCP today, wrap Agno `MCPTools` in a plugin tool factory:

```python
from agno.tools.mcp import MCPTools
from mindroom.tool_system.metadata import (
    SetupType,
    ToolCategory,
    ToolStatus,
    register_tool_with_metadata,
)


class FilesystemMCPTools(MCPTools):
    def __init__(self, **kwargs):
        super().__init__(
            command="npx -y @modelcontextprotocol/server-filesystem /path/to/dir",
            **kwargs,
        )


@register_tool_with_metadata(
    name="mcp_filesystem",
    display_name="MCP Filesystem",
    description="Tools from an MCP filesystem server",
    category=ToolCategory.DEVELOPMENT,
    status=ToolStatus.AVAILABLE,
    setup_type=SetupType.NONE,
)
def mcp_filesystem_tools():
    return FilesystemMCPTools
```

Reference the plugin and tool in `config.yaml`:

```yaml
plugins:
  - ./plugins/mcp-filesystem

agents:
  assistant:
    tools:
      - mcp_filesystem
```

The factory function must return the toolkit class, not an instance. MCP toolkits are async; Agno's async agent runs (`arun`, `aprint_response`) handle MCP connect and disconnect automatically.

## Tools module example

```python
from __future__ import annotations

from typing import TYPE_CHECKING

from mindroom.tool_system.metadata import (
    SetupType,
    ToolCategory,
    ToolStatus,
    register_tool_with_metadata,
)

if TYPE_CHECKING:
    from agno.tools import Toolkit


@register_tool_with_metadata(
    name="greeter",
    display_name="Greeter",
    description="A simple greeting tool",
    category=ToolCategory.DEVELOPMENT,
    status=ToolStatus.AVAILABLE,
    setup_type=SetupType.NONE,
)
def greeter_tools() -> type[Toolkit]:
    from agno.tools import Toolkit

    class GreeterTools(Toolkit):
        """A simple greeting toolkit."""

        def __init__(self) -> None:
            super().__init__(name="greeter", tools=[self.greet])

        def greet(self, name: str) -> str:
            """Greet someone by name."""
            return f"Hello, {name}!"

    return GreeterTools
```

The factory function (decorated with `@register_tool_with_metadata`) must return the **class**, not an instance. MindRoom instantiates the class when building agents.

All decorator arguments are keyword-only. Required fields:

- `name`: Tool identifier
- `display_name`: Human-readable name
- `description`: Brief description
- `category`: A `ToolCategory` enum value (`COMMUNICATION`, `DEVELOPMENT`, `EMAIL`, `ENTERTAINMENT`, `INFORMATION`, `INTEGRATIONS`, `PRODUCTIVITY`, `RESEARCH`, `SHOPPING`, `SMART_HOME`, `SOCIAL`)

Common optional fields:

- `status`: `ToolStatus.AVAILABLE` (default) or `REQUIRES_CONFIG`
- `setup_type`: `SetupType.NONE` (default), `API_KEY`, `OAUTH`, or `SPECIAL`
- `config_fields`: List of `ConfigField` objects (see below)
- `dependencies`: List of required pip packages
- `docs_url`: Link to documentation
- `managed_init_args`: Explicit MindRoom-managed constructor kwargs such as `runtime_paths` or `credentials_manager`

### ConfigField

Each `ConfigField` describes one constructor parameter that can be configured through the dashboard or credentials store.

| Field | Type | Default | Description |
| --- | --- | --- | --- |
| `name` | string | *required* | Constructor kwarg name (e.g., `"api_key"`) |
| `label` | string | *required* | Display label shown in the dashboard |
| `type` | string | `"text"` | Input type: `text`, `password`, `url`, `number`, `boolean`, or `select` |
| `required` | bool | `True` | Whether the field must be set before the tool can be used |
| `default` | any | `None` | Default value when not configured |
| `placeholder` | string | `None` | Placeholder text shown in the input |
| `description` | string | `None` | Help text for the field |
| `options` | list | `None` | For `select` type: list of `{"label": "...", "value": "..."}` dicts |
| `validation` | dict | `None` | Optional validation rules (min, max, pattern, etc.) |

If your toolkit constructor expects MindRoom-managed values, declare them with `managed_init_args`.
This applies to built-in tools under `src/mindroom/tools/` just as much as external plugins.
MindRoom no longer inspects constructor parameter names and injects those values automatically.
Undeclared managed constructor inputs will not be passed through.

Available `ToolManagedInitArg` values:

| Value | Constructor kwarg | Description |
| --- | --- | --- |
| `RUNTIME_PATHS` | `runtime_paths` | Access to storage paths and environment values |
| `CREDENTIALS_MANAGER` | `credentials_manager` | Read and write the per-tool credentials store |
| `WORKER_TARGET` | `worker_target` | Resolved worker routing context (scope, execution identity, worker key) |

For example, a toolkit that expects `runtime_paths` must opt in explicitly:

```python
from agno.tools import Toolkit
from mindroom.tool_system.metadata import ToolCategory, ToolManagedInitArg, register_tool_with_metadata


@register_tool_with_metadata(
    name="needs_runtime",
    display_name="Needs Runtime",
    description="Example tool that needs runtime paths",
    category=ToolCategory.DEVELOPMENT,
    managed_init_args=(ToolManagedInitArg.RUNTIME_PATHS,),
)
def needs_runtime_tools() -> type[Toolkit]:
    class NeedsRuntimeTools(Toolkit):
        def __init__(self, *, runtime_paths):
            self.runtime_paths = runtime_paths
            super().__init__(name="needs_runtime", tools=[])

    return NeedsRuntimeTools
```

## Plugin skills

List skill directories in the manifest `skills` array. Those directories are added to the skill search roots.

## Reloading plugins

Plugin manifests and tools modules are cached by mtime. Changes are picked up the next time MindRoom reloads the tool registry (for example, on startup or config reload).

## Security notes

Plugins execute code in-process. Only install plugins you trust.
