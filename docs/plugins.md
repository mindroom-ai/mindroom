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
| `skills` | list | Relative directories containing skills (optional) |

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

## Tools module example

```python
from agno.tools import Toolkit
from mindroom.tools_metadata import ToolCategory, register_tool_with_metadata


class GreeterTools(Toolkit):
    """A simple greeting toolkit."""

    def __init__(self) -> None:
        super().__init__(name="greeter", tools=[self.greet])

    def greet(self, name: str) -> str:
        """Greet someone by name."""
        return f"Hello, {name}!"


@register_tool_with_metadata(
    name="greeter",
    display_name="Greeter",
    description="A simple greeting tool",
    category=ToolCategory.DEVELOPMENT,
)
def greeter_tools() -> type[GreeterTools]:
    return GreeterTools
```

The factory function (decorated with `@register_tool_with_metadata`) must return the **class**, not an instance. MindRoom instantiates the class when building agents.

## Plugin skills

List skill directories in the manifest `skills` array. Those directories are added to the skill search roots.

## Reloading plugins

Plugin manifests and tools modules are cached by mtime. Changes are picked up the next time MindRoom reloads the tool registry (for example, on startup or config reload).

## Security notes

Plugins execute code in-process. Only install plugins you trust.
