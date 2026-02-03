---
icon: lucide/bot
---

# MindRoom

AI agents that live in Matrix and work everywhere via bridges.

## What is MindRoom?

MindRoom is an AI agent orchestration system with Matrix integration. It provides:

- **Multi-agent collaboration** - Configure multiple specialized agents that can work together
- **Matrix-native** - Agents live in Matrix rooms and respond to messages
- **Persistent memory** - Agent, room, and team-scoped memory that persists across conversations
- **80+ tool integrations** - Connect to external services like GitHub, Slack, Gmail, and more
- **Hot-reload configuration** - Update `config.yaml` and agents restart automatically

> [!TIP]
> **Matrix is the backbone** - MindRoom agents communicate through the Matrix protocol, which means they can be bridged to Discord, Slack, Telegram, and other platforms.

## Quick Start

### Installation

```bash
# Using uv (recommended)
uv tool install mindroom

# Using pip
pip install mindroom
```

### Basic Usage

1. Create a `config.yaml`:

```yaml
agents:
  assistant:
    display_name: Assistant
    role: A helpful AI assistant
    model: sonnet
    rooms: [lobby]

models:
  sonnet:
    provider: anthropic
    id: claude-sonnet-4-latest

defaults:
  num_history_runs: 5
  markdown: true
```

2. Set up your Matrix credentials in `.env`:

```bash
MATRIX_HOMESERVER=https://matrix.example.com
MATRIX_USER_ID=@bot:example.com
MATRIX_ACCESS_TOKEN=your_token
ANTHROPIC_API_KEY=your_api_key
```

3. Run MindRoom:

```bash
mindroom run
```

## Features

| Feature | Description |
|---------|-------------|
| **Agents** | Single-specialty actors with specific tools and instructions |
| **Teams** | Collaborative bundles of agents that coordinate or parallelize work |
| **Router** | Built-in traffic director that routes messages to the right agent |
| **Memory** | Mem0-inspired dual memory: agent, room, and team-scoped |
| **Tools** | 80+ integrations for external services |
| **Hot Reload** | Config changes are detected and agents restart automatically |

## Architecture

```
┌─────────────────────────────────────────────────────┐
│                 Matrix Homeserver                    │
└─────────────────────┬───────────────────────────────┘
                      │
┌─────────────────────▼───────────────────────────────┐
│              MultiAgentOrchestrator                  │
│  ┌─────────┐ ┌─────────┐ ┌─────────┐ ┌─────────┐   │
│  │ Router  │ │ Agent 1 │ │ Agent 2 │ │  Team   │   │
│  └─────────┘ └─────────┘ └─────────┘ └─────────┘   │
└─────────────────────────────────────────────────────┘
```

## Documentation

- [Getting Started](getting-started.md) - Installation and first steps
- [Configuration](configuration/index.md) - All configuration options
- [Tools](tools/index.md) - Available tool integrations
- [Skills and Plugins](configuration/skills-and-plugins.md) - Skill loading and plugin extensions
- [Memory System](memory.md) - How agent memory works
- [Architecture](architecture/index.md) - How it works under the hood
- [CLI Reference](cli.md) - Command-line interface

## License

MIT
