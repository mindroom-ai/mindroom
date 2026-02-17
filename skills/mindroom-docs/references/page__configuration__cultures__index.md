# Culture Configuration

Cultures let a group of agents share evolving principles, practices, and conventions. A culture is backed by [Agno's CultureManager](https://docs.agno.com/agents/culture) and persists its knowledge in a SQLite database under `mindroom_data/culture/<culture_name>.db`.

## Basic Culture

```
cultures:
  engineering:
    description: Follow clean code principles and write tests
    agents: [developer, reviewer]
```

## Full Configuration

```
cultures:
  engineering:
    # Describes the shared principles this culture captures
    description: Follow clean code principles, write tests, and review before merging

    # Agents assigned to this culture (must be defined in agents section)
    agents:
      - developer
      - reviewer

    # How the culture is updated: automatic, agentic, or manual (default: automatic)
    mode: automatic
```

## Configuration Fields

| Field         | Required | Default       | Description                                                               |
| ------------- | -------- | ------------- | ------------------------------------------------------------------------- |
| `description` | No       | `""`          | Description of the shared principles and practices the culture captures   |
| `agents`      | No       | `[]`          | Agent names assigned to this culture (must exist in the `agents` section) |
| `mode`        | No       | `"automatic"` | How culture knowledge is updated (see modes below)                        |

## Culture Modes

| Mode        | Behavior                                                                                                  |
| ----------- | --------------------------------------------------------------------------------------------------------- |
| `automatic` | Culture knowledge is automatically extracted from every agent interaction and added to the shared context |
| `agentic`   | The agent decides when to update culture knowledge via a tool call                                        |
| `manual`    | Culture context is read-only; the description is included in agent context but knowledge is never updated |

All modes include the culture description in the agent's context. The difference is whether and how the culture's knowledge base evolves over time.

## Rules

- Each agent can belong to **at most one** culture. Assigning the same agent to multiple cultures is a validation error.
- All agents listed in a culture must exist in the top-level `agents` section.
- Culture state is persisted to `mindroom_data/culture/<culture_name>.db` and survives restarts.
- Culture managers are cached and shared across agents in the same culture — if two agents belong to the same culture, they share the same `CultureManager` instance.
- Changes to a culture's `description` or `mode` in `config.yaml` invalidate the cache, so the manager is recreated on the next hot-reload.
