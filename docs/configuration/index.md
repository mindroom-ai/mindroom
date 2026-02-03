---
icon: lucide/settings
---

# Configuration

MindRoom is configured through a `config.yaml` file. This section covers all configuration options.

## Configuration File

The configuration file is loaded from the current directory by default. You can specify a different path:

```bash
mindroom run --config /path/to/config.yaml
```

## Basic Structure

```yaml
# Agent definitions
agents:
  assistant:
    display_name: Assistant
    role: A helpful AI assistant
    model: sonnet
    tools: [file, shell]
    rooms: [lobby]

# Model configurations
models:
  sonnet:
    provider: anthropic
    id: claude-sonnet-4-latest

# Team configurations (optional)
teams:
  research_team:
    display_name: Research Team
    agents: [researcher, writer]
    mode: collaborate

# Router configuration (optional)
router:
  model: haiku
  instructions:
    - Route technical questions to the code agent

# Default settings
defaults:
  num_history_runs: 5
  markdown: true
  debug: false

# Timezone for scheduled tasks
timezone: America/Los_Angeles
```

## Sections

- [Agents](agents.md) - Configure individual AI agents
- [Models](models.md) - Configure AI model providers
- [Teams](teams.md) - Configure multi-agent collaboration
- [Router](router.md) - Configure message routing
- [Skills and Plugins](skills-and-plugins.md) - Configure skill loading and plugins
