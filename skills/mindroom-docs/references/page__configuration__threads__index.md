# Threads, Replies & Participation

See also [Routing & Responder Selection](https://docs.mindroom.chat/configuration/router/), [Access Control](https://docs.mindroom.chat/authorization/), and [Logs & Monitoring](https://docs.mindroom.chat/deployment/operational-log-events/).

## Thread Mode Resolution

Thread mode is resolved per message using the current room ID.
A persisted `!thread_mode room` or `!thread_mode thread` override takes precedence for all entities in that room.
Room admins can use `!thread_mode reset` to restore the static resolution rules below; see [Chat Commands](https://docs.mindroom.chat/chat-commands/).
For an agent, MindRoom checks `room_thread_modes` in this order.
First, it checks an exact room ID key.
Second, it checks the managed room key/alias associated with that room ID.
Third, it resolves each configured `room_thread_modes` key to a room ID and matches that against the current room.
If none match, it falls back to `thread_mode`.

For a team, MindRoom resolves mode per member agent for that room.
If all member agents resolve to the same mode, the team uses that mode.
If member modes differ, the team defaults to `thread`.

For the router, MindRoom resolves mode using agents relevant to the active room.
This includes agents directly configured for the room and agents included via `teams.<name>.rooms`.
If all relevant agents resolve to the same mode, the router uses that mode.
If modes are mixed, the router defaults to `thread`.

### `!thread_mode`

Show or switch how future agent replies are grouped in the current Matrix room.

```
!thread_mode
!thread_mode show
!thread_mode room
!thread_mode thread
!thread_mode reset
```

`!thread_mode` and `!thread_mode show` show the current room override.
`!thread_mode room` uses one continuous conversation for the whole room.
`!thread_mode thread` uses Matrix threads for separate conversations in this room.
`!thread_mode reset` removes the room override so agents use configured `thread_mode` and `room_thread_modes` values again.
Set and reset are Matrix room-admin-only actions.
The override is stored in MindRoom runtime state under `mindroom_data/tracking`, not in `config.yaml`, so it works when config is static or read-only.

## Threading (MSC3440)

MindRoom emits thread replies following [MSC3440](https://github.com/matrix-org/matrix-spec-proposals/blob/main/proposals/3440-threading-via-relations.md), using `m.relates_to` with `rel_type: m.thread`.

Explicit `m.thread` metadata remains the primary source of thread conversation context.
For clients or bridges that send plain replies without thread metadata (`m.in_reply_to` but no `rel_type: m.thread`), MindRoom applies a transitive compatibility rule.
If a reply chain eventually reaches explicit thread `T` or a proven thread root, MindRoom treats the new reply as part of `T`.
Replies that never reach threaded context stay room-level.

### Resolution Rules

When deriving context for an incoming event, MindRoom:

1. Uses explicit `m.thread` relations as the primary inbound thread identity.
2. Lets plain replies inherit thread membership transitively when their reply chain reaches a threaded ancestor or proven thread root.
3. Lets edits, reactions, redactions, and other target-bound operations inherit the canonical thread membership of their target event.
4. May start a new thread under a room-root event when agent thread mode requires it.

A read that must come straight from the homeserver, such as the thread-summary pin check, restart auto-resume, and thread-root proofs for tools, walks room history back at most 1,000 pages of 100 events and keeps at most 10,000 events from that walk, counting each non-edit message and one edit per original and sender; a root older than either bound is reported as unproven, so those callers fail closed.
Every message event in the room counts toward the page bound, including each streaming edit of every other reply on a homeserver that keeps them.

```
├── User: @assistant help with this code
│   ├── Assistant: I can help! Let me look at it...
│   ├── User: It should return a list
│   └── Assistant: Here's the updated version...
```

Use `build_message_content()` from `message_builder.py` to construct thread-aware messages, and `EventInfo.from_event()` to analyze event relations (threads, edits, replies, reactions).

## Multi-Human Thread Protection

When multiple human users have posted in a thread, explicit `@mention` targeting is the default.
An authorized, materializable individual agent that already replied in the thread can opt into judging an untagged turn with `agents.<name>.participation`.
It can answer after participation approval; a decline produces no text reply, and failed checks stay quiet after any configured judgment fallback.
A configured `decline_reaction` can acknowledge a deliberate decline.
See [Adaptive Participation](#adaptive-participation) for eligibility and configuration.
The router stays silent and multi-human threads do not automatically form ad-hoc teams.

The rules are:

1. **Mentioned eligible agents or teams respond** — an explicit `@agent` or `@team` bypasses AI routing, but room configuration and reply permissions still apply.
2. **Non-thread messages** — a single eligible agent or team can auto-respond, regardless of how many humans are present.
3. **Threads with one human** — normal auto-response behavior applies, so the agent or team continues the conversation.
4. **Threads with two or more humans** — explicit targeting is required by default; eligible individual agents can use the opt-in participation exception above.
5. **Mentioning another joined room participant** — if a message tags only joined users who are neither managed entities nor configured bot accounts, agents and teams stay silent.

A thread whose history does not fit in one read, 2,000 messages or 16 MiB of stored content, whichever comes first, is not continued from an untagged message when the room has more than one possible responder, unless an agent is still answering there; mention the agent or team to continue it.

Mentions of unmanaged user IDs that are absent from the room or merely invited do not suppress normal routing or automatic responses.
Mentioning a configured agent that is not joined to the room still counts as an explicit mention for the other agents.

### Bot accounts

By default, any Matrix user that is not a MindRoom agent or team counts as a "human" for the rules above.
This includes bridge bots (Telegram, Slack, etc.) and other non-MindRoom bots.
If a bridge bot relays a message into a thread, it looks like a second human to MindRoom and triggers the mention requirement.

To prevent this, list those accounts in `bot_accounts`:

```yaml
bot_accounts:
  - "@telegram:example.com"
  - "@slackbot:example.com"
```

Accounts in this list are treated like MindRoom entities for response logic — their messages and mentions don't count toward the multi-human detection.

## Adaptive Participation

By default, threads with two or more human participants require explicit agent mentions.
Set `agents.<name>.participation` to let an agent that already replied in such a thread decide whether to answer an untagged message or stay silent.
`participation: {}` enables it with the defaults below, and omitting it or setting it to `null` keeps the default behavior.
The setting follows the agent into every room where it may reply, including ad hoc rooms, and has no room-level overrides; the retired top-level `room_participation` setting is rejected.

| Field | Type | Default | Description |
|-------|------|---------|-------------|
| `debounce_seconds` | number | `3.0` | Quiet window for eligible text; must be finite and between `0` and `30` seconds, inclusive |
| `instructions` | string | `""` | Additional guidance for deciding whether the agent should participate |
| `decline_reaction` | string or null | `null` | Reaction to a deliberate decline; nonblank and at most 64 characters, such as `"👍"`; `null` keeps declines invisible |
| `judgment` | object or null | `null` | Optional separate [judgment backend](#judgment-backends); `null` uses the agent's reply model |

```yaml
agents:
  assistant:
    display_name: Assistant
    participation:
      debounce_seconds: 3.0
      instructions: "Reply when you can help; leave human conversation uninterrupted."
      decline_reaction: "👀"
```

The check runs only when all of these hold:

- The message is text in a thread with at least two human participants, including the sender.
- The message does not mention an agent or another human; explicit mentions follow normal reply rules and end the sender's pending pause.
- This agent has already replied in the thread and may still reply to the sender.

Configured human aliases count as one participant, while registered agents, the internal service account, and `bot_accounts` do not count as humans.
Agents that are merely present in the room never join the conversation: with 50 agents in a room and two already in a thread, only those two can judge, and only if each opted in.
Each eligible agent decides separately.
A burst of messages from one sender becomes one turn after `debounce_seconds`.
Commands and scheduled work never use adaptive participation, and single-human conversations keep their usual behavior.

A decision to stay silent sends no reply.
With `decline_reaction` set, each declining agent reacts once to the latest message of the turn; failed checks never react, and a failed reaction send is only logged.
Choose the emoji with care, because 👍 can read as agreement with the message.

Without `judgment`, the agent's own reply model decides with tool execution disabled, and the agent stays quiet if that check fails.
Gemini explicit context caches, OpenAI Chat search-only requests, OpenRouter automatic web search, and Groq Compound systems cannot be checked this way and stay quiet unless a separate judgment backend approves.

### Judgment Backends

`participation.judgment` and `mid_turn.judgment` select a separate decision model while the agent's configured model still writes the reply.

| Field | Backend | Default | Description |
|-------|---------|---------|-------------|
| `provider` | both | Required | `llm` for a configured model alias, or `typesafe` for System One |
| `model` | `llm` | Required | Existing alias under `models`, which can be cheaper than the reply model |
| `threshold` | `typesafe` | `0.8` | Minimum probability from `0` to `1`; rejected for `llm` |
| `timeout_seconds` | both | `5` for `llm`, `1.5` for `typesafe` | Positive deadline of at most `30` seconds; for participation it starts after `debounce_seconds` |

Unknown fields are rejected.

```yaml
agents:
  assistant:
    display_name: Assistant
    participation:
      instructions: "Reply when you can help; leave human conversation uninterrupted."
      judgment:
        provider: llm
        model: fast
        timeout_seconds: 5
```

To use System One instead, set `TYPESAFE_API_KEY` in the process environment or config-adjacent `.env` and change the judgment:

```yaml
      judgment:
        provider: typesafe
        threshold: 0.8
        timeout_seconds: 1.5
```

The LLM backend uses the alias's normal provider credentials and receives no tools, agent system prompt, or agent memory.
A model alias whose provider adds native tools that cannot be disabled is refused.
For participation, a TypeSafe probability at or above `threshold` approves; the default `0.8` has not been calibrated on representative conversations.
The two backends get the same question and context, but their judgments can differ.

Judgments share a process-wide limit of eight concurrent calls and one per agent, without a waiting queue.
Judgment outcome logs record backend, model, decision, latency, token usage, input size, and failure category, plus probability and threshold for TypeSafe, without request text or credentials.
[Mid-Turn Coalescing](#mid-turn-coalescing) describes what its judge sees and what happens when a judgment fails.

#### Participation Context and Fallback

A participation judgment sends the agent's guidance and up to eight recent user and assistant messages to the backend, with Matrix identities replaced by aliases.
System prompts, memory, tool definitions and results, and media are not sent, but message text can still contain private information.
When the context contains media, attachment references, detected secrets, or more than 16 KB of text, it is not sent and the reply model decides instead.
Missing credentials, a full limit, a timeout, a provider error, malformed output, or an abstention falls back to the reply model's own decision, and the agent stays quiet if that also fails.
After a judgment backend approves, the agent replies normally, including with provider modes that the reply-model check cannot use.

## Mid-Turn Coalescing

When another human message arrives during an active response, MindRoom normally sends a wrap-up notice after the current tool batch, asking the agent to stop making new tool calls and summarize its progress.
Set `agents.<name>.mid_turn` to let a judge decide whether the queued messages can wait until the active task finishes.
The setting follows the agent into every authorized room, including ad hoc rooms, and has no room-level overrides; the retired `room_mid_turn` setting is rejected.
Omitting it or setting it to `null` keeps the wrap-up notice, and teams do not inherit it from their members.

| Field | Type | Default | Description |
|-------|------|---------|-------------|
| `judgment` | object | Required | LLM model alias or TypeSafe backend; see [Judgment Backends](#judgment-backends) |
| `instructions` | string | `""` | Extra guidance for the finish-or-wrap-up decision |
| `defer_reaction` | string or null | `null` | Reaction such as `"👀"` when a queued message can wait; nonblank and at most 64 characters |

```yaml
agents:
  helper:
    display_name: Helper
    mid_turn:
      instructions: Continue for acknowledgements; wrap up for corrections or changed requirements.
      defer_reaction: "👀"
      judgment:
        provider: llm
        model: fast  # An existing alias under models
        timeout_seconds: 5
```

The judge asks whether any queued message requires an immediate change, pause, or stop.
Acknowledgements, praise, thanks, "continue", and "do not interrupt" let the task continue, while corrections and relevant changes take priority even alongside praise.
Unrelated requests wait for a later turn unless the user asks for an immediate switch.
With TypeSafe, the task continues when `1 - P(interrupt) >= threshold`, so the default `0.8` tolerates interruption probabilities up to `0.2`.
With the LLM backend, an explicit `false` answer lets the task continue.
Abstentions, timeouts, missing credentials, a full judgment limit, and backend errors keep the wrap-up behavior.

The check runs between completed tool batches and never interrupts a running tool.
The judge sees the active request, the earlier public conversation, up to eight queued human messages, the configured guidance, and the agent's visible reply text as published when each message was queued.
Private tool results and arguments, system prompts, memory, and attachment contents are not sent, but message text can still contain private information.
The wrap-up notice is sent without a judgment when the conversation history is missing or incomplete, contains media, has more than 63 earlier messages, or exceeds 16 KB, when a message contains attachments or detected credentials, and for `thread_mode: room` turns.

Queued messages stay queued and are handled after the active response finishes.
A finish decision covers only the messages it saw, so a later message needs a new decision, and a wrap-up notice once sent is not reversed.
The wrap-up notice asks the model to hand off; it does not cancel tools, abort the response, or inject the queued text, and stop handling and tool approval are unchanged.
With `defer_reaction` set, each deferred message gets the reaction once; wrap-up decisions and failed judgments never react.

## Message Edits

**Message edits**: When a user edits a message that already received an agent response, the agent regenerates its response for the updated content.
The agent edits its own previous reply in place rather than sending a new message.
Edits from other agents never trigger regeneration; another agent's completed reply that mentions this entity is dispatched to it as a reply instead.

## Automatic Restart Resumption

`defaults.auto_resume_after_restart` (boolean, default `true`) makes the router post resume prompts after a restart in eligible threads whose conversations were interrupted.
Set it to `false` to suppress those prompts and resume the work manually.
Work that was superseded by later messages, or whose requester or room membership no longer applies, is not resumed.
