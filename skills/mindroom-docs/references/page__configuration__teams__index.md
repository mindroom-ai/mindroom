# Team Configuration

Teams allow multiple agents to collaborate on tasks. MindRoom supports two collaboration modes.

<video controls playsinline preload="metadata" aria-label="Mentioning two agents forms a team that answers together" style="width: 100%">
  <source src="https://github.com/user-attachments/assets/b6ea7dff-8542-409f-823a-0cd3590a9555#t=0.1" type="video/mp4" media="(prefers-color-scheme: dark)">
  <source src="https://github.com/user-attachments/assets/3c28159d-4d3e-4e47-bc5e-07e4e0c5eed1#t=0.1" type="video/mp4">
</video>

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

    # Accept all, none, or matching inviter ID patterns
    accept_invites: true

    # Model for team coordination (default: "default")
    model: sonnet


    # Team-scoped replay controls (optional; inherit from defaults when omitted)
    num_history_runs: 8
    num_history_messages: null
    max_tool_calls_from_history: 6
    max_tool_calls_per_turn: 200

    # Team-scoped required-compaction overrides (optional)
    # Soft thresholds do not compact by themselves while history still fits.
    compaction:
      enabled: true
      threshold_percent: 0.8
      reserve_tokens: 16384
      timeout_seconds: 600
```

## Configuration Fields

| Field | Required | Default | Description |
|-------|----------|---------|-------------|
| `display_name` | Yes | - | Human-readable name shown in Matrix |
| `role` | Yes | - | Description of the team's purpose |
| `agents` | Yes | - | List of agent names that compose this team |
| `mode` | No | `coordinate` | Collaboration mode: `coordinate` or `collaborate` |
| `rooms` | No | `[]` | List of room names the team responds in |
| `accept_invites` | No | `true` | Accept all inbound Matrix room invites with `true`, none with `false` or `[]`, or only inviters matching an exact or wildcard Matrix user ID after human-only alias resolution; non-human accounts retain their exact transport ID |
| `access` | No | `null` | Conversation-access policy with `current_room_members`, `members_of_rooms`, and `users`. Omitting it grants members of this team's own managed `rooms`. See [Authorization](https://docs.mindroom.chat/authorization/) |
| `model` | No | `default` | Model used for team coordination and synthesis |
| `num_history_runs` | | | See [History & Compaction](https://docs.mindroom.chat/configuration/history/#team-history-and-compaction) |
| `num_history_messages` | | | See [History & Compaction](https://docs.mindroom.chat/configuration/history/#team-history-and-compaction) |
| `max_tool_calls_from_history` | | | See [History & Compaction](https://docs.mindroom.chat/configuration/history/#team-history-and-compaction) |
| `max_tool_calls_per_turn` | No | `defaults.max_tool_calls_per_turn` | Tool calls the team coordinator may execute in one turn, delegations to members included; each member keeps its own `max_tool_calls_per_turn`, and the same end-of-budget behavior as for [agents](https://docs.mindroom.chat/configuration/agents/) applies |
| `compaction` | | | See [History & Compaction](https://docs.mindroom.chat/configuration/history/#team-history-and-compaction) |

Team YAML keys follow the same naming rules as agents: alphanumeric characters and underscores only, and no overlap with agent names.
Each configured team must contain at least one unique, known agent.
Private agents, and agents whose delegation closure reaches private agents, are not supported in configured teams.

Invitation acceptance is independent from team conversation access.
After joining, the team applies its ordinary `access` policy to every interaction.

See [Team History and Compaction](https://docs.mindroom.chat/configuration/history/#team-history-and-compaction).

## When to Use Each Mode

| Mode | Use Case | Example |
|------|----------|---------|
| `coordinate` | Agents need to do different subtasks | "Get weather and news" - coordinator assigns weather to one agent, news to another |
| `collaborate` | Want diverse perspectives on the same problem | "What do you think about X?" - all agents analyze the same question and share their views |

## Dynamic Team Formation

When multiple agents are mentioned in a message (e.g., `@code @research analyze this`), MindRoom automatically forms an ad-hoc team. Dynamic teams form in these scenarios:
In threads with multiple human participants, stale thread context does not auto-form a team.
A fresh explicit `@mention` in the current message is required for team responses in those threads.
An eligible individual agent that already replied can separately opt into [Adaptive Participation](https://docs.mindroom.chat/configuration/threads/#adaptive-participation) and answer an untagged turn after approval; this does not automatically form a team.

1. **Multiple agents explicitly tagged** - e.g., `@code @research analyze this`
2. **Thread with previously mentioned agents** - Follow-up messages in a thread where multiple agents were mentioned earlier, as long as the thread has not become a multi-human conversation that now requires a fresh explicit mention
3. **Thread with multiple agent participants** - Continuing a conversation where multiple agents have responded, as long as the thread has not become a multi-human conversation that now requires a fresh explicit mention
4. **DM room with multiple agents** - Messages in a DM room containing multiple agents (main timeline only)

### Mode Selection

For dynamic teams, the collaboration mode is selected by AI based on the task:

- Tasks with different subtasks for each agent use **coordinate** mode
- Tasks asking for opinions or brainstorming use **collaborate** mode

Before model selection completes, explicit mentions carry a provisional **coordinate** heuristic and thread-derived groups carry a provisional **collaborate** heuristic.
The execution layer asks the model to refine that choice; when model selection fails or returns an unexpected result, MindRoom falls back to **collaborate**.
