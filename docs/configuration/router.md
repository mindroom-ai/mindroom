---
icon: lucide/route
---

# Router Configuration

The router is a built-in traffic director that greets rooms and decides which agent should answer when no specific agent is mentioned.

## Basic Configuration

```yaml
router:
  model: haiku
```

## Full Configuration

```yaml
router:
  # Model for routing decisions
  model: haiku

  # Custom routing instructions
  instructions:
    - Route code questions to the code agent
    - Route research questions to the research agent
    - When in doubt, ask for clarification

  # Enable debug mode for routing
  debug: false
```

## How Routing Works

1. A message arrives without an agent mention
2. The router analyzes the message content
3. Based on the available agents and their roles, it selects the best match
4. The selected agent responds to the message

## Disabling the Router

To disable automatic routing, simply don't include a `router` section:

```yaml
agents:
  assistant:
    display_name: Assistant
    role: A helpful assistant
    model: sonnet
    rooms: [lobby]

# No router section = no automatic routing
# Users must mention agents directly: @assistant
```
