# Routing

Routing picks which agent or team answers a message that mentions no one.
No account needs inviting for it: agents work in any room they are in, and the agent a person talks to posts its own approval cards, voice transcripts and command replies.
Configure routing under `router:` to choose its model, whose messages are routed, and optional System One or OpenAI Decisions responder selection.

MindRoom also keeps one account of its own, shown as "MindRoom", for the rooms listed in `config.yaml`.
It creates and administers those rooms, the root Space and the emoji room, and posts their welcome messages.
It is registered only when the config has such rooms, cannot be mentioned, never answers, and accepts no room invitations.

See also [Threads, Replies & Participation](https://docs.mindroom.chat/configuration/threads/), [Access Control](https://docs.mindroom.chat/authorization/), and [Logs & Monitoring](https://docs.mindroom.chat/deployment/operational-log-events/).

## Configuration

```yaml
router:
  model: haiku          # Must exist under models
```

| Option | Type | Default | Description |
|--------|------|---------|-------------|
| `model` | string | `"default"` | Model alias used for routing decisions |
| `judgment` | object or null | `null` | Optional System One or OpenAI Decisions responder selection before LLM routing; see [Responder Selection Judgments](#responder-selection-judgments) |
| `access` | object or null | `null` | Whose messages are routed; see [Access Control](https://docs.mindroom.chat/authorization/) |

Point `model` at a cheap, fast alias to keep routing inexpensive.
Room and thread model overrides also apply to routing.

Unset, `access` restricts nothing: each agent and team still applies its own `access`, so only responders the sender may address are candidates.
Set, it defaults to `current_room_members: true`, which still applies when you set only `access.users`; set `current_room_members: false` to remove it.

`router.accept_invites` is deprecated and ignored.
A config that still sets it loads with one warning at startup and on each reload.
The MindRoom account stays in the rooms it already joined.

## How Routing Works

When a message mentions no agent or team, MindRoom first finds the responders the sender may address in that room.

- In configured rooms, candidates are the agents and teams that list the room in `rooms`.
- In any other room, candidates are the MindRoom agents and teams currently joined to the room.
- Either way, candidates the sender is not allowed to address are removed.

Then:

1. If the room's [reply mode](https://docs.mindroom.chat/configuration/threads/#reply-mode) is `mentions`, nobody answers unless the message is in a thread an agent or team already replied in or is still answering.
2. If the thread requires explicit targeting, nothing is routed; eligible individual agents already in the thread may still use opt-in [adaptive participation](https://docs.mindroom.chat/configuration/threads/#adaptive-participation).
3. If exactly one eligible responder remains, it answers directly without an AI routing call.
4. If several remain, routing reads the message and up to three previous thread messages (each cut to 100 characters) and picks the best match from the candidates' roles, tools, and instructions.
5. The chosen agent or team answers the message: the account doing the routing answers itself when it is chosen, and tells any other choice with a hidden event.
   No routing message appears in the room.

One MindRoom account per room does the routing: the one with the highest power level there, the lowest Matrix ID breaking a tie.
In the rooms MindRoom creates from `config.yaml` that is usually the MindRoom owner account, and elsewhere usually an agent or team, but the same rule decides in every room; an agent or team that routes answers itself when it is the best match.
If the routing call fails, for example because of a model error or an invalid choice, the candidate with the lowest Matrix ID answers.
If no candidate fits the message, nobody answers; mention an agent or team to get an answer.
Mentioning an agent or team with `@name` always bypasses routing, but the entity still answers only if it is in the room, listed for that room in its `rooms` when the room is configured, and allowed by its `access`.
An entity invited into a configured room that does not list it posts a note when it joins, and repeats that note instead of answering when it alone is mentioned there: the room's agents are managed in the MindRoom configuration, so talk to the entity in a new room it is invited to.
A request that mentions several entities gets the same explanation when every entity it cannot use is such an invited, unlisted entity.
The note speaks only to people the entity's `access` admits and names the room's agents they can ask instead.
See [Multi-Human Thread Protection](https://docs.mindroom.chat/configuration/threads/#multi-human-thread-protection) for when threads require explicit tags.

### Mentioning the MindRoom account

The MindRoom account cannot be mentioned by name and is not a conversational agent.
A message that tags only its Matrix ID gets no answer, and a command in such a message is not run.
Mention a specific agent or team to get an answer, or several agents for an ad-hoc collaboration.

## Responder Selection Judgments

Set `router.judgment` to let System One (TypeSafe) or the OpenAI Decisions API choose among the eligible agents and teams before ordinary LLM routing.
It is off by default, even when the backend's API key is set.
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

For the OpenAI Decisions API, set `provider: openai_decisions`; it uses the OpenAI API key described in [Judgment Backends](https://docs.mindroom.chat/configuration/threads/#judgment-backends).

| Field | Type | Default | Description |
|-------|------|---------|-------------|
| `provider` | string | Required | `typesafe` or `openai_decisions` |
| `threshold` | float | `0.8` | From `0` to `1`; both the chosen option's probability and the backend's confidence must reach it |
| `timeout_seconds` | float | `1.5` | Positive deadline of at most `30` seconds |

Unknown fields are rejected.

The backend receives each candidate's role, tools, delegation targets, and brief instructions, the current message, and the last three visible messages with senders replaced by aliases.
Tool calls in those messages appear as `<tool>` tags with the same short input and output previews the chat shows.
It does not receive system prompts, full tool output, attachment contents, or the rest of the conversation.
It runs only with 2 to 253 candidates and a request of at most 16,000 bytes.
Long recent messages are shortened to their start and end, and the oldest drop out first; the current message is never shortened, so a long current message or input that looks like a secret uses ordinary routing instead.

| Outcome | Result |
|---------|--------|
| A candidate is chosen | That agent or team answers, as with LLM routing |
| `no_fit` | Nobody answers; mention an agent or team to get an answer. OpenAI-compatible [`auto`](https://docs.mindroom.chat/openai-api/#auto-routing) requests get HTTP 400 with code `no_suitable_responder` |
| `multiple` | Ordinary LLM routing picks one responder; several agents are never launched |
| Tie, low confidence, abstention, missing key, full judgment limit, timeout, or error | Ordinary LLM routing with `router.model` |

Routing prompt overrides apply only to LLM routing, not to the judgment backend.
Routing judgments share the process-wide limit and logging described in [Judgment Backends](https://docs.mindroom.chat/configuration/threads/#judgment-backends), with at most one routing judgment at a time.

## Welcome Messages

At startup, the MindRoom account posts a welcome in each empty room from `config.yaml`, listing the room's configured responders, how to mention them, and a quick command reference.
When agents or teams join or leave such a room later, it edits that welcome to list the ones there now; a welcome sent before a restart stays as it was.
Rooms that are not in `config.yaml` get no welcome.
Send [`!hi`](https://docs.mindroom.chat/chat-commands/#hi) to see the welcome again, limited to responders visible to you.

Related pages: [command handling](https://docs.mindroom.chat/chat-commands/#command-handling), [room management](https://docs.mindroom.chat/rooms/#room-management), [voice message processing](https://docs.mindroom.chat/voice/#voice-message-processing), [configuration confirmations](https://docs.mindroom.chat/chat-commands/#configuration-confirmations), and [running scheduled tasks](https://docs.mindroom.chat/scheduling/#scheduled-task-restoration).
