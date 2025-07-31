# Agent Configuration Guide

mindroom uses a YAML-based configuration system for defining agents. This allows you to easily customize existing agents or create entirely new ones without modifying any code.

## Configuration File Location

The default agent configuration file is located at `agents.yaml` in the project root. You can also specify a custom configuration file path when initializing the bot.

## Agent Configuration Structure

Each agent is defined with the following properties:

```yaml
agents:
  agent_name:
    display_name: "Human-readable name for the agent"
    role: "Description of what the agent does"
    tools:
      - tool_name_1
      - tool_name_2
    instructions:
      - "Specific instruction 1"
      - "Specific instruction 2"
    num_history_runs: 5  # Number of previous conversations to include
```

## Available Tools

The following tools are available for agents to use:

- `calculator` - Mathematical calculations
- `file` - File reading and writing operations
- `shell` - Shell command execution
- `csv` - CSV file handling and data analysis
- `arxiv` - Academic paper search from arXiv
- `duckduckgo` - Web search
- `wikipedia` - Wikipedia article lookup
- `newspaper` - News article parsing
- `yfinance` - Financial data and stock information

## Creating Custom Agents

### Example 1: Simple Agent (No Tools)

```yaml
agents:
  motivator:
    display_name: "MotivationalCoach"
    role: "Provide encouragement and positive reinforcement"
    tools: []
    instructions:
      - "Always be positive and encouraging"
      - "Provide specific, actionable advice"
      - "Celebrate small wins"
    num_history_runs: 3
```

### Example 2: Advanced Agent (Multiple Tools)

```yaml
agents:
  project_manager:
    display_name: "ProjectManagerAgent"
    role: "Help manage software projects and development tasks"
    tools:
      - file
      - shell
      - csv
    instructions:
      - "Track project tasks and milestones"
      - "Generate status reports"
      - "Analyze project metrics"
      - "Help with git operations"
      - "Create and update documentation"
    num_history_runs: 10
```

## Using Custom Configuration Files

You can create your own configuration file and use it:

```python
from pathlib import Path
from mindroom.agents import get_agent
from mindroom.agent_loader import create_agent

# Use a custom configuration file directly
config_path = Path("path/to/your/agents.yaml")
agent = create_agent("your_custom_agent", model, config_path)

# Or use the standard interface (uses default agents.yaml)
agent = get_agent("agent_name", model)
```

## Best Practices

1. **Clear Role Definition**: Make the agent's purpose clear in the `role` field
2. **Specific Instructions**: Provide detailed instructions for consistent behavior
3. **Appropriate Tools**: Only include tools the agent actually needs
4. **History Context**: Set `num_history_runs` based on how much context the agent needs
5. **Testing**: Test your custom agents thoroughly before deployment

## Extending the System

### Adding Custom Tools

To add new tools to the system:

1. Implement your tool following the Agno tools interface
2. Register it in `mindroom/tools.py` using the decorator:

```python
from mindroom.tools import register_tool

@register_tool("your_tool_name")
def _get_your_custom_tool():
    from your_module import YourCustomTool
    return YourCustomTool
```

3. Use it in your agent configuration:

```yaml
agents:
  your_agent:
    tools:
      - your_tool_name
```

The lazy import pattern (importing inside the function) ensures that missing dependencies won't break the entire system.

## Examples

See `agents_custom_example.yaml` for more examples of custom agent configurations.
