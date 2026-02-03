---
icon: lucide/plug-2
---

# Plugins System

MindRoom supports dynamic plugins for extending functionality with custom tools and skills.

## Plugin Structure

A plugin is a directory containing a `manifest.json`:

```
my-plugin/
├── manifest.json
├── tools.py
└── skills/
    └── my-skill/
        └── SKILL.md
```

## Manifest Format

```json
{
  "name": "my-plugin",
  "version": "1.0.0",
  "description": "A custom plugin for MindRoom",
  "tools_module": "tools",
  "skill_paths": ["skills"]
}
```

| Field | Type | Description |
|-------|------|-------------|
| `name` | string | Plugin identifier |
| `version` | string | Semantic version |
| `description` | string | Brief description |
| `tools_module` | string | Python module containing tools |
| `skill_paths` | list | Directories containing skills |

## Installing Plugins

### Local Directory

Place plugins in `~/.mindroom/plugins/`:

```bash
mkdir -p ~/.mindroom/plugins/my-plugin
```

### From Git

Clone directly into the plugins directory:

```bash
cd ~/.mindroom/plugins
git clone https://github.com/user/mindroom-plugin-name
```

## Creating Tools

Define tools in your `tools.py`:

```python
from agno.tools import Toolkit

class MyToolkit(Toolkit):
    """Custom tools for my plugin."""

    def __init__(self):
        super().__init__(name="my_tools")
        self.register(self.my_function)

    def my_function(self, param: str) -> str:
        """Do something useful.

        Args:
            param: The input parameter

        Returns:
            The result
        """
        return f"Processed: {param}"
```

## Creating Skills

Add skills in the `skills/` directory following the [Skills documentation](skills.md).

## Hot Reload

Plugins support hot reload - changes to `manifest.json` trigger automatic reloading:

1. MindRoom watches the plugins directory
2. On manifest change, the plugin is unloaded
3. The updated plugin is loaded fresh
4. Agent toolkits are rebuilt

## Using Plugin Tools

Reference plugin tools in agent configuration:

```yaml
agents:
  assistant:
    tools:
      - my_tools  # From plugin
      - file      # Built-in
```

## Using Plugin Skills

Reference plugin skills the same way:

```yaml
agents:
  assistant:
    skills:
      - my-skill  # From plugin
```

## Plugin Discovery

MindRoom searches for plugins in:

1. `~/.mindroom/plugins/` - User plugins
2. `MINDROOM_PLUGINS_PATH` - Custom path (if set)
3. Installed Python packages with `mindroom.plugins` entry point

## Example Plugin

Here's a complete example plugin:

**manifest.json**:
```json
{
  "name": "weather-plugin",
  "version": "1.0.0",
  "description": "Weather information tools",
  "tools_module": "tools"
}
```

**tools.py**:
```python
import httpx
from agno.tools import Toolkit

class WeatherToolkit(Toolkit):
    def __init__(self, api_key: str):
        super().__init__(name="weather")
        self.api_key = api_key
        self.register(self.get_weather)

    def get_weather(self, city: str) -> str:
        """Get current weather for a city."""
        # Implementation here
        return f"Weather in {city}: Sunny, 72°F"
```

## Best Practices

1. **Version your plugins** - Use semantic versioning
2. **Document dependencies** - List required packages
3. **Handle missing credentials** - Check for API keys gracefully
4. **Test independently** - Plugins should work in isolation
5. **Use type hints** - Helps with tool schema generation
