---
icon: lucide/rocket
---

# Getting Started

This guide will help you set up MindRoom and create your first AI agent.

## Prerequisites

- Python 3.12 or higher
- A Matrix homeserver (or use a public one like matrix.org)
- API keys for your preferred AI provider (Anthropic, OpenAI, etc.)

## Installation

=== "uv (recommended)"

    ```bash
    uv tool install mindroom
    ```

=== "pip"

    ```bash
    pip install mindroom
    ```

=== "From source"

    ```bash
    git clone https://github.com/mindroom-ai/mindroom
    cd mindroom
    uv sync
    ```

## Configuration

### 1. Create your config file

Create a `config.yaml` in your working directory:

```yaml
agents:
  assistant:
    display_name: Assistant
    role: A helpful AI assistant that can answer questions
    model: sonnet
    rooms: [lobby]

models:
  sonnet:
    provider: anthropic
    id: claude-sonnet-4-latest

defaults:
  num_history_runs: 5
  markdown: true

timezone: America/Los_Angeles
```

### 2. Set up environment variables

Create a `.env` file with your credentials:

```bash
# Matrix homeserver (must allow open registration for agent accounts)
MATRIX_HOMESERVER=https://matrix.example.com

# AI provider API keys
ANTHROPIC_API_KEY=your_anthropic_key
# OPENAI_API_KEY=your_openai_key
# GOOGLE_API_KEY=your_google_key
```

!!! note "Matrix Registration"
    MindRoom automatically creates Matrix user accounts for each agent.
    Your Matrix homeserver must allow open registration, or you need to
    configure it to allow registration from localhost.

### 3. Run MindRoom

```bash
mindroom run
```

MindRoom will:

1. Connect to your Matrix homeserver
2. Create Matrix users for each agent
3. Join the specified rooms
4. Start listening for messages

## Next Steps

- Learn about [agent configuration](configuration/agents.md)
- Explore [available tools](tools/index.md)
- Set up [teams for multi-agent collaboration](configuration/teams.md)
