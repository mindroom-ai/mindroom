# Router Configuration

The router is a built-in MindRoom account that is always present, cannot be disabled, and joins every room with configured agents or teams.
It picks which agent or team answers a message that mentions no one, accepts room invitations, and sends welcome messages.
Configure it under `router:` to choose its routing model, who may invite it into rooms, who may interact with it, and optional System One responder selection.

See also [Threads, Replies & Participation](https://docs.mindroom.chat/configuration/threads/), [Access Control](https://docs.mindroom.chat/authorization/), and [Logs & Monitoring](https://docs.mindroom.chat/deployment/operational-log-events/).

## Configuration

```yaml
router:
  model: haiku          # Must exist under models
  accept_invites: true
```

| Option | Type | Default | Description |
|--------|------|---------|-------------|
| `model` | string | `"default"` | Model alias used for routing decisions |
| `judgment` | object or null | `null` | Optional System One responder selection before LLM routing; see [Responder Selection Judgments](#responder-selection-judgments) |
| `access` | object or null | `null` | Who may interact with the router; see [Access Control](https://docs.mindroom.chat/authorization/) |
| `accept_invites` | bool or list[string] | `true` | `true` accepts every room invitation, `false` or `[]` accepts none, and a list accepts only inviters matching an exact or wildcard Matrix user ID |

Point `model` at a cheap, fast alias to keep routing inexpensive.
Room and thread model overrides also apply to routing.

### Invitations and access

Inviter patterns are matched after [identity alias](https://docs.mindroom.chat/authorization/#requester-identity-and-private-state) resolution and use the same case-sensitive wildcard rules as `access.users`.
Rooms joined through an invitation are kept across restarts and survive room cleanup.
Accepting an invitation only joins the room; every interaction afterwards still requires the router's `access` policy.
Router `access` defaults to `current_room_members: true`, which still applies when you set only `access.users`; set `current_room_members: false` to remove it.

## How Routing Works

When a message mentions no agent or team, MindRoom first finds the responders the sender may address in that room.

- In configured rooms, candidates are the agents and teams that list the room in `rooms`.
- In ad-hoc rooms the router joined by invitation, candidates are the MindRoom agents and teams currently joined to the room.
- Either way, candidates the sender is not allowed to address are removed.

Then:

1. If the thread requires explicit targeting, the router stays silent; eligible individual agents already in the thread may still use opt-in [adaptive participation](https://docs.mindroom.chat/configuration/threads/#adaptive-participation).
2. If exactly one eligible responder remains, it answers directly without an AI routing call.
3. If several remain, the router reads the message and up to three previous thread messages (each cut to 100 characters) and picks the best match from the candidates' roles, tools, and instructions.
4. The router posts a message mentioning the chosen agent or team (for example "@code could you help with this?"), and that entity answers in the thread.

If routing fails, for example because of a model error or an invalid choice, the router replies "Please try mentioning an agent or team directly with @ or rephrase your request."
Mentioning an agent or team with `@name` always bypasses routing, but the entity still answers only if it is in the room, listed for that room in its `rooms` when the room is configured, and allowed by its `access`.
An entity invited into a configured room that does not list it posts a note when it joins, and repeats that note instead of answering when it alone is mentioned there: the room's agents are managed in the MindRoom configuration, so talk to the entity in a new room it is invited to.
A request that mentions several entities gets the same explanation when every entity it cannot use is such an invited, unlisted entity.
The note speaks only to people the entity's `access` admits and names the room's agents they can ask instead.
See [Multi-Human Thread Protection](https://docs.mindroom.chat/configuration/threads/#multi-human-thread-protection) for when threads require explicit tags.

### Mentioning the router

The router is not a conversational agent.
A message that mentions only the router gets a short rules-of-engagement reply instead of an answer.
Mention a specific agent or team to get an answer, or several agents for an ad-hoc collaboration.

## Responder Selection Judgments

Set `router.judgment` to let System One (TypeSafe) choose among the eligible agents and teams before ordinary LLM routing.
It is off by default, even when `TYPESAFE_API_KEY` is set.
Explicit mentions, thread participation rules, access control, and the single-candidate shortcut work the same way with or without it.

Set `TYPESAFE_API_KEY` in the process environment or the `.env` next to `config.yaml`, then enable it:

```yaml
router:
  model: default
  judgment:
    provider: typesafe
    threshold: 0.8
    timeout_seconds: 1.5
```

| Field | Type | Default | Description |
|-------|------|---------|-------------|
| `provider` | string | Required | Must be `typesafe` |
| `threshold` | float | `0.8` | From `0` to `1`; both the chosen option's probability and System One's confidence must reach it |
| `timeout_seconds` | float | `1.5` | Positive deadline of at most `30` seconds |

Unknown fields are rejected.

System One receives each candidate's role, tools, delegation targets, and brief instructions, the current message, and the last three visible messages with senders replaced by aliases.
It does not receive system prompts, tool results, attachment contents, or the rest of the conversation.
It runs only with 2 to 253 candidates and a request of at most 16,000 bytes; messages are never shortened, so long input or input that looks like a secret uses ordinary routing instead.

| Outcome | Result |
|---------|--------|
| A candidate is chosen | The router mentions that agent or team as usual |
| `no_fit` | The router asks the user to mention a responder or rephrase; OpenAI-compatible [`auto`](https://docs.mindroom.chat/openai-api/#auto-routing) requests get HTTP 400 with code `no_suitable_responder` |
| `multiple` | Ordinary LLM routing picks one responder; several agents are never launched |
| Tie, low confidence, abstention, missing key, full judgment limit, timeout, or error | Ordinary LLM routing with `router.model` |

The router's prompt overrides apply only to LLM routing, not to System One.
Router judgments share the process-wide limit and logging described in [Judgment Backends](https://docs.mindroom.chat/configuration/threads/#judgment-backends), with at most one router judgment at a time.

## Welcome Messages

After accepting an invitation into a room with no message history, the router posts a welcome if the inviter has router access, listing the agents and teams the inviter may address, how to mention them, and a quick command reference.
At startup, empty configured rooms get a welcome listing their configured responders.
Send [`!hi`](https://docs.mindroom.chat/chat-commands/#hi) to see the welcome again, limited to responders visible to you.

The router's other duties are documented on their own pages: [command handling](https://docs.mindroom.chat/chat-commands/#command-handling), [room management](https://docs.mindroom.chat/rooms/#room-management), [voice message processing](https://docs.mindroom.chat/voice/#voice-message-processing), [configuration confirmations](https://docs.mindroom.chat/chat-commands/#configuration-confirmations), and [scheduled task restoration](https://docs.mindroom.chat/scheduling/#scheduled-task-restoration).
