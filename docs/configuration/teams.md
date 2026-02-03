---
icon: lucide/users
---

# Team Configuration

Teams allow multiple agents to collaborate on tasks. MindRoom supports two collaboration modes.

## Team Modes

### Coordinate Mode

A team leader (automatically created) delegates different subtasks to team members:

```yaml
teams:
  dev_team:
    display_name: Dev Team
    role: Development team for building features
    agents: [architect, coder, reviewer]
    mode: coordinate
```

In coordinate mode, the leader analyzes the task and assigns different subtasks to each agent based on their roles. The leader decides whether to run tasks sequentially or in parallel based on dependencies.

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

In collaborate mode, every team member receives the exact same task and works on it independently. This is useful when you want diverse perspectives on the same problem.

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

## How Teams Work

1. A message mentions the team (e.g., `@super_team`)
2. The team leader (automatically created) receives the message
3. In **coordinate** mode:
   - The leader delegates different subtasks to each agent based on their roles
   - Agents may work sequentially or in parallel depending on task dependencies
   - The leader synthesizes all outputs into a cohesive response
4. In **collaborate** mode:
   - All agents receive the same task and work on it simultaneously
   - Each agent provides their perspective independently
   - The leader synthesizes all perspectives into a final response

## When to Use Each Mode

| Mode | Use Case | Example |
|------|----------|---------|
| `coordinate` | Agents need to do different subtasks | "Get weather and news" - weather agent gets weather, news agent gets news |
| `collaborate` | Want diverse perspectives on same problem | "What do you think about X?" - all agents share their view |

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

When AI mode selection is unavailable, MindRoom falls back to:
- **coordinate** for explicitly tagged agents (they likely have different roles)
- **collaborate** for agents from thread history (likely discussing same topic)
