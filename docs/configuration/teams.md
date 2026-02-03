---
icon: lucide/users
---

# Team Configuration

Teams allow multiple agents to collaborate on tasks. MindRoom supports two collaboration modes.

## Team Modes

### Coordinate Mode

A lead agent orchestrates the work of other agents:

```yaml
teams:
  dev_team:
    display_name: Dev Team
    agents: [architect, coder, reviewer]
    mode: coordinate
    lead: architect
```

### Collaborate Mode

All agents respond in parallel and a summary is generated:

```yaml
teams:
  research_team:
    display_name: Research Team
    agents: [researcher, analyst, writer]
    mode: collaborate
```

## Full Configuration

```yaml
teams:
  super_team:
    # Display name shown in Matrix
    display_name: Super Team

    # Agents in this team
    agents:
      - code
      - research
      - finance

    # Collaboration mode: coordinate or collaborate
    mode: collaborate

    # Lead agent for coordinate mode
    lead: code

    # Rooms the team responds in
    rooms:
      - team-room

    # Model for team coordination
    model: sonnet

    # Custom instructions for team behavior
    instructions:
      - Discuss findings before concluding
      - Cite sources when possible
```

## How Teams Work

1. A message mentions the team (e.g., `@super_team`)
2. In **coordinate** mode, the lead agent plans and delegates
3. In **collaborate** mode, all agents respond in parallel
4. Results are synthesized into a final response
