---
icon: lucide/user
---

# Agent Configuration

Agents are the core building blocks of MindRoom. Each agent is a specialized AI actor with specific capabilities.

## Basic Agent

```yaml
agents:
  assistant:
    display_name: Assistant
    role: A helpful AI assistant
    model: sonnet
    rooms: [lobby]
```

## Full Configuration

```yaml
agents:
  code:
    # Display name shown in Matrix
    display_name: CodeAgent

    # Role description - guides the agent's behavior
    role: Generate code, manage files, execute shell commands

    # Model to use (defined in models section)
    model: sonnet

    # Tools the agent can use
    tools:
      - file
      - shell
      - github

    # Custom instructions
    instructions:
      - Always read files before modifying them
      - Use clear variable names
      - Add comments for complex logic

    # Rooms to join (will be created if they don't exist)
    rooms:
      - lobby
      - dev

    # Number of previous messages to include for context
    num_history_runs: 10

    # Enable markdown formatting
    markdown: true

    # Enable debug mode for this agent
    debug: false

    # Enable memory for this agent
    memory: true
