# Team Configuration

Teams allow multiple agents to collaborate on tasks. MindRoom supports two collaboration modes.

## Team Modes

### Coordinate Mode

The team coordinator analyzes the task and delegates different subtasks to specific team members:

```
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

```
teams:
  research_team:
    display_name: Research Team
    role: Research team for comprehensive analysis
    agents: [researcher, analyst, writer]
    mode: collaborate
```

In collaborate mode, the task is delegated to all team members simultaneously. Each agent works on the same task independently, and the coordinator synthesizes all perspectives into a final response. This is useful when you want diverse perspectives on the same problem.

## Full Configuration

```
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

    # Participate in room-level startup prewarm for rooms already joined at first sync (default: true)
    startup_thread_prewarm: true

    # Team-scoped replay controls (optional; inherit from defaults when omitted)
    num_history_runs: 8
    num_history_messages: null
    max_tool_calls_from_history: 6

    # Team-scoped auto-compaction overrides (optional)
    compaction:
      enabled: true
      threshold_percent: 0.8
      reserve_tokens: 16384
```

## Configuration Fields

| Field                         | Required | Default                                | Description                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                               |
| ----------------------------- | -------- | -------------------------------------- | ----------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `display_name`                | Yes      | -                                      | Human-readable name shown in Matrix                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                       |
| `role`                        | Yes      | -                                      | Description of the team's purpose                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                         |
| `agents`                      | Yes      | -                                      | List of agent names that compose this team                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                |
| `mode`                        | No       | `coordinate`                           | Collaboration mode: `coordinate` or `collaborate`                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                         |
| `rooms`                       | No       | `[]`                                   | List of room names the team responds in                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                   |
| `model`                       | No       | `default`                              | Model used for team coordination and synthesis                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                            |
| `startup_thread_prewarm`      | No       | `true`                                 | When enabled, this bot may prewarm recent thread snapshots for rooms already joined when first sync completes, which can reduce cold-cache latency for early thread replies after startup                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                 |
| `num_history_runs`            | No       | `defaults.num_history_runs`            | Number of prior team-scoped runs to replay                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                |
| `num_history_messages`        | No       | `defaults.num_history_messages`        | Max messages from team-scoped history replayed into the next run                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                          |
| `max_tool_calls_from_history` | No       | `defaults.max_tool_calls_from_history` | Max tool call messages replayed from team-scoped history                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                  |
| `compaction`                  | No       | `defaults.compaction`                  | Team-scoped auto-compaction overrides (`enabled`, `threshold_tokens`, `threshold_percent`, `reserve_tokens`, `model`). When the active team model has a known `context_window`, MindRoom always computes a final replay plan for the shared team scope and reduces or disables persisted replay for the run when needed. Authoring `defaults.compaction` enables the optional destructive compaction phase for inheriting agents and teams. A non-empty per-team block also enables that destructive phase for the team, but a bare `compaction: {}` is only a no-op override that inherits authored defaults. Replay safety always uses the active team model window. If you set `compaction.model`, that summary model must also define its own `context_window`, but only for the durable summary-generation pass. Compaction uses an in-room lifecycle notice that is edited in place |

Team YAML keys follow the same naming rules as agents: alphanumeric characters and underscores only, and no overlap with agent names.

`num_history_runs` and `num_history_messages` are mutually exclusive, just like the agent-level settings.
When a named team sets these fields, the team scope uses the team-owned policy instead of inheriting one member's history policy.

Startup thread prewarm is a background, best-effort cache warmup for rooms already joined when first sync completes.

## When to Use Each Mode

| Mode          | Use Case                                      | Example                                                                                   |
| ------------- | --------------------------------------------- | ----------------------------------------------------------------------------------------- |
| `coordinate`  | Agents need to do different subtasks          | "Get weather and news" - coordinator assigns weather to one agent, news to another        |
| `collaborate` | Want diverse perspectives on the same problem | "What do you think about X?" - all agents analyze the same question and share their views |

## Dynamic Team Formation

When multiple agents are mentioned in a message (e.g., `@code @research analyze this`), MindRoom automatically forms an ad-hoc team. Dynamic teams form in these scenarios:
In threads with multiple human participants, stale thread context does not auto-form a team.
A fresh explicit `@mention` in the current message is required before agents respond.

1. **Multiple agents explicitly tagged** - e.g., `@code @research analyze this`
1. **Thread with previously mentioned agents** - Follow-up messages in a thread where multiple agents were mentioned earlier, as long as the thread has not become a multi-human conversation that now requires a fresh explicit mention
1. **Thread with multiple agent participants** - Continuing a conversation where multiple agents have responded, as long as the thread has not become a multi-human conversation that now requires a fresh explicit mention
1. **DM room with multiple agents** - Messages in a DM room containing multiple agents (main timeline only)

### Mode Selection

For dynamic teams, the collaboration mode is selected by AI based on the task:

- Tasks with different subtasks for each agent use **coordinate** mode
- Tasks asking for opinions or brainstorming use **collaborate** mode

When AI mode selection is unavailable or fails, MindRoom falls back to:

- **coordinate** when multiple agents are explicitly tagged in the message (they likely have different roles to fulfill)
- **collaborate** for all other cases, such as agents from thread history or DM rooms (likely discussing the same topic)

Dynamic teams do not have a named `teams:` entry, so their history replay and compaction policy comes from `defaults`, not from any participating agent's overrides.
