# Team Configuration

A team lets several agents answer one request together, either as a named team under `teams:` that has its own Matrix account, or as an ad hoc team formed when a message involves several agents.

<video controls playsinline preload="metadata" aria-label="Mentioning two agents forms a team that answers together" style="width: 100%">
  <source src="https://github.com/user-attachments/assets/b6ea7dff-8542-409f-823a-0cd3590a9555#t=0.1" type="video/mp4" media="(prefers-color-scheme: dark)">
  <source src="https://github.com/user-attachments/assets/3c28159d-4d3e-4e47-bc5e-07e4e0c5eed1#t=0.1" type="video/mp4">
</video>

## Team Modes

| Mode | What happens | Use when | Example |
|------|--------------|----------|---------|
| `coordinate` (default) | The team model splits the task into subtasks, assigns each to the member whose role fits, runs them in sequence or in parallel depending on their dependencies, and synthesizes the results | Members need to do different parts of the work | "Get weather and news": one agent fetches weather, another fetches news |
| `collaborate` | Every member works on the same task independently, and the team model synthesizes their answers | You want several perspectives on one question | "What do you think about X?": every agent answers, then the views are combined |

A team reply opens with a `🤝 Team Response (<members>)` header, shows each member's contribution under its name, and ends with a **Team Consensus** section; without a consensus it notes that only the individual responses are shown.

## Named Teams

```yaml
teams:
  dev_team:
    display_name: Dev Team
    role: Development team for building features
    agents: [architect, coder, reviewer]
    mode: coordinate
    rooms: [dev]
    model: sonnet

  research_team:
    display_name: Research Team
    role: Research team for comprehensive analysis
    agents: [researcher, analyst, writer]
    mode: collaborate
    num_history_runs: 8
    compaction:
      enabled: true
      threshold_percent: 0.8
```

| Field | Type | Default | Description |
|-------|------|---------|-------------|
| `display_name` | string | *required* | Name shown in Matrix |
| `role` | string | *required* | Description of the team's purpose |
| `agents` | list | *required* | Names of agents under `agents:` that form the team; at least one, no duplicates |
| `mode` | string | `coordinate` | `coordinate` or `collaborate` |
| `rooms` | list | `[]` | Room keys, aliases, or Matrix room IDs the team joins and responds in |
| `accept_invites` | bool or list | `true` | `true` accepts every room invitation, `false` or `[]` accepts none, and a list accepts only inviters matching an exact or wildcard Matrix user ID; this is separate from the members' own settings. Joining a room never grants its members access; see [Invitations](https://docs.mindroom.chat/authorization/#invitations) |
| `access` | object | `null` | Who may converse with the team: `current_room_members`, `members_of_rooms`, and `users`. When omitted, members of the team's own managed `rooms` have access. See [Responder access](https://docs.mindroom.chat/authorization/#responder-access) |
| `model` | string | `"default"` | Key under `models:` used for coordination and synthesis |
| `max_tool_calls_per_turn` | int, >= 1 | `defaults.max_tool_calls_per_turn` | Tool calls the team may execute in one turn, delegations to members included; each member keeps its own budget, and the end-of-budget behavior matches [agents](https://docs.mindroom.chat/configuration/agents/#configuration-options) |
| `num_history_runs`, `num_history_messages`, `max_tool_calls_from_history`, `compaction` | | | History replay and compaction for the team's shared history; see [Team History and Compaction](https://docs.mindroom.chat/configuration/history/#team-history-and-compaction) |

Team keys follow the agent [naming rules](https://docs.mindroom.chat/configuration/agents/#naming-rules), and a key cannot be used under both `agents:` and `teams:`.
Private agents, and agents that can reach a private agent through `delegate_to`, cannot be team members; see [Private Instances](https://docs.mindroom.chat/configuration/agents/#private-instances).

## Ad Hoc Teams

MindRoom forms a team without any `teams:` entry when:

1. **Several agents are tagged in one message**, for example `@code @research analyze this`.
2. **An untagged follow-up in a thread** where several agents were mentioned earlier or several agents have replied.
3. **An untagged message in the main timeline of a DM room** that contains several agents.

Tagging exactly one agent gets a reply from that agent alone.
Threads where two or more people have posted never form a team from earlier thread context; tag the agents again in the current message (see [Multi-Human Thread Protection](https://docs.mindroom.chat/configuration/threads/#multi-human-thread-protection)).

If any tagged agent cannot take part, for example because it is not in the room or not available to you, MindRoom replies with the reason instead of forming a partial team, such as `Team request includes agent 'code' that is not available in this room.`
For teams formed from thread context or a DM room, unavailable agents are left out, and if only one remains it answers alone.

The `default` model picks the mode for each ad hoc team from the request: `coordinate` when the agents have different subtasks, `collaborate` for opinions or brainstorming, and `collaborate` when the choice fails.
Ad hoc teams take their history and compaction settings from `defaults`; see [Team History and Compaction](https://docs.mindroom.chat/configuration/history/#team-history-and-compaction).
