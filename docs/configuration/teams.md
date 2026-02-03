---
icon: lucide/users
---

# Team Configuration

Teams allow multiple agents to collaborate on tasks. MindRoom supports two collaboration modes.

## Team Modes

### Coordinate Mode

The team coordinator analyzes the task and delegates different subtasks to specific team members:

```yaml
teams:
  dev_team:
    display_name: Dev Team
    role: Development team for building features
    agents: [architect, coder, reviewer]
    mode: coordinate
```

In coordinate mode, the coordinator analyzes the task and selects which agents should handle which subtasks based on their roles. The coordinator decides whether to run tasks sequentially or in parallel based on dependencies, then synthesizes all outputs into a cohesive response.

### Collaborate Mode

All agents work on the same task simultaneously and their outputs are synthesized:

```yaml
teams:
  research_team:
    display_name: Research Team
    role: Research team for comprehensive analysis
    agents: [researcher, analyst, writer]
    mode: collaborate
```

In collaborate mode, the task is delegated to all team members simultaneously. Each agent works on the same task independently, and the coordinator synthesizes all perspectives into a final response. This is useful when you want diverse perspectives on the same problem.

## Full Configuration

```yaml
teams:
  super_team:
    # Display name shown in Matrix
    display_name: Super Team

    # Description of the team's purpose (required)
    role: Multi-disciplinary team for complex tasks

    # Agents in this team (must be defined in agents section)
    agents:
      - code
      - research
      - finance

    # Collaboration mode: coordinate or collaborate (default: coordinate)
    mode: collaborate

    # Rooms the team responds in
    rooms:
      - team-room

    # Model for team coordination (default: "default")
    model: sonnet
```

## Configuration Fields

| Field | Required | Default | Description |
|-------|----------|---------|-------------|
| `display_name` | Yes | - | Human-readable name shown in Matrix |
| `role` | Yes | - | Description of the team's purpose |
| `agents` | Yes | - | List of agent names that compose this team |
| `mode` | No | `coordinate` | Collaboration mode: `coordinate` or `collaborate` |
| `rooms` | No | `[]` | List of room names the team responds in |
| `model` | No | `default` | Model used for team coordination and synthesis |

## When to Use Each Mode

| Mode | Use Case | Example |
|------|----------|---------|
| `coordinate` | Agents need to do different subtasks | "Get weather and news" - coordinator assigns weather to one agent, news to another |
| `collaborate` | Want diverse perspectives on the same problem | "What do you think about X?" - all agents analyze the same question and share their views |

## Dynamic Team Formation

When multiple agents are mentioned in a message (e.g., `@code @research analyze this`), MindRoom automatically forms an ad-hoc team. Dynamic teams form in these scenarios:

1. **Multiple agents explicitly tagged** - e.g., `@code @research analyze this`
2. **Thread with previously mentioned agents** - Follow-up messages in a thread where multiple agents were mentioned earlier
3. **Thread with multiple agent participants** - Continuing a conversation where multiple agents have responded
4. **DM room with multiple agents** - Messages in a DM room containing multiple agents (main timeline only)

### Mode Selection

For dynamic teams, the collaboration mode is selected by AI based on the task:

- Tasks with different subtasks for each agent use **coordinate** mode
- Tasks asking for opinions or brainstorming use **collaborate** mode

When AI mode selection is unavailable or fails, MindRoom falls back to:
- **coordinate** when multiple agents are explicitly tagged in the message (they likely have different roles to fulfill)
- **collaborate** for all other cases, such as agents from thread history or DM rooms (likely discussing the same topic)
