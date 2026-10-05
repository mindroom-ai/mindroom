---
icon: lucide/messages-square
---

# Threads, Replies & Participation

This page covers how agents group replies into threads or one room conversation, when agents answer in threads with several people, how agents can opt into judging untagged messages, what happens to messages sent while an agent is still working, and how edits and restarts affect replies.
See also [Routing & Responder Selection](router.md), [Access Control](../authorization.md), and [Logs & Monitoring](../deployment/operational-log-events.md).

## Thread Mode Resolution

Each agent's `thread_mode` and `room_thread_modes` fields (see [Agent Configuration](agents.md)) choose between Matrix threads and one continuous conversation per room.
A room override set with [`!thread_mode`](#thread_mode) takes precedence for every agent, team, and the router in that room.
Without one, an agent uses the `room_thread_modes` entry matching the current room by Matrix room ID, room key, or alias, and otherwise its `thread_mode`.

A team uses its members' mode for the room when all members agree, and `thread` when they differ.
The router uses the mode shared by all agents relevant to the room, including agents that join through `teams.<name>.rooms`, and `thread` when they differ.

### `!thread_mode`

Show or switch how future agent replies are grouped in the current Matrix room.

```
!thread_mode
!thread_mode show
!thread_mode room
!thread_mode thread
!thread_mode reset
```

`!thread_mode` and `!thread_mode show` (also `list` or `status`) show the current room override.
`!thread_mode room` uses one continuous conversation for the whole room.
Room-mode conversations are not included in [thread exports](../thread-exports.md).
`!thread_mode thread` uses Matrix threads for separate conversations in this room.
`!thread_mode reset` (also `clear`) removes the room override so agents use their configured `thread_mode` and `room_thread_modes` again.
Only Matrix room admins can set or reset the override; others get `Room admin only.`
The override is stored in MindRoom's runtime state under `mindroom_data/tracking`, not in `config.yaml`, so it works when config is static or read-only.

## Threading (MSC3440)

Agents reply in [MSC3440](https://github.com/matrix-org/matrix-spec-proposals/blob/main/proposals/3440-threading-via-relations.md) threads.
In `thread` mode, an agent answering a room-level message starts a new thread on it.
Clients and bridges that send plain replies instead of thread replies still work: a plain reply whose reply chain reaches a thread joins that thread, and one that never reaches a thread stays room-level.
Edits, reactions, and redactions belong to the same thread as the message they target.

## Multi-Human Thread Protection

When two or more people have posted in a thread, agents answer only when explicitly mentioned, so they do not interrupt a human conversation.
The router stays silent in such threads, and they never automatically form ad hoc teams.
Agents that already replied there can opt into [Adaptive Participation](#adaptive-participation) to judge untagged messages.

The rules are:

1. **Mentioned eligible agents or teams respond**: an explicit `@agent` or `@team` bypasses AI routing, but room configuration and reply permissions still apply.
2. **Non-thread messages**: a single eligible agent or team can auto-respond, regardless of how many humans are present.
3. **Threads with one human**: normal auto-response behavior applies, so the agent or team continues the conversation.
4. **Threads with two or more humans**: explicit targeting is required, except for agents using adaptive participation.
5. **Mentioning another joined room participant**: if a message tags only joined users who are neither agents, teams, nor configured bot accounts, agents and teams stay silent.

MindRoom reads a message's mentions from `m.mentions.user_ids`, or when that is empty from HTML mention pills in `formatted_body`, or otherwise from plain `@name` text in the body, so bridged messages without mention metadata still count.
Mentions of users who are not in the room or only invited do not suppress normal responses.
Mentioning a configured agent that has not joined the room still counts as an explicit mention for the other agents.

A thread too long to read at once, more than 2,000 messages or 16 MiB of stored content, is not continued from an untagged message when the room has more than one possible responder, unless an agent is still answering there; mention the agent or team to continue it.

### Bot accounts

Any Matrix user that is not a MindRoom agent or team counts as a human for these rules, including bridge bots (Telegram, Slack, etc.) and other non-MindRoom bots.
A bridge bot relaying messages into a thread therefore looks like a second human and makes agents require mentions.
List such accounts in the top-level [`bot_accounts`](../authorization.md#bot-accounts) field so their messages and mentions do not count toward multi-human detection.

## Adaptive Participation

Set `agents.<name>.participation` to let an agent that already replied in a multi-human thread decide whether to answer an untagged message or stay silent.
`participation: {}` enables it with the defaults below; omitting it or setting it to `null` keeps the mention requirement.
The setting applies in every room where the agent may reply, including ad hoc rooms, and has no per-room override.

| Field | Type | Default | Description |
|-------|------|---------|-------------|
| `debounce_seconds` | number | `3.0` | Quiet window before deciding, from `0` to `30` seconds; a burst of messages from one sender is judged as one turn |
| `instructions` | string | `""` | Additional guidance for deciding whether the agent should participate |
| `decline_reaction` | string or null | `null` | Reaction to a deliberate decline, such as `"👀"`; nonblank and at most 64 characters; `null` keeps declines invisible |
| `judgment` | object or null | `null` | Separate [judgment backend](#judgment-backends); `null` lets the agent's reply model decide |

```yaml
agents:
  assistant:
    display_name: Assistant
    participation:
      debounce_seconds: 3.0
      instructions: "Reply when you can help; leave human conversation uninterrupted."
      decline_reaction: "👀"
```

An agent judges a message only when all of these hold:

- The message is text in a thread with at least two human participants, including the sender.
- The message mentions neither an agent nor another human; mentioned messages follow the normal rules.
- This agent has already replied in the thread and may still reply to the sender.

Configured human aliases count as one participant, while agents, the internal service account, and `bot_accounts` do not count as humans.
Agents merely present in the room never join: with 50 agents in a room and two already in the thread, only those two can judge, and only if each opted in.
Each eligible agent decides separately.
Commands and scheduled work never use adaptive participation.

An agent that declines sends no reply; with `decline_reaction` set, it reacts once to the latest message of the turn.
Choose the emoji with care, because 👍 can read as agreement with the message.
A failed check never reacts and the agent stays quiet.

Without `judgment`, the agent's own reply model decides with tools disabled.
Gemini explicit context caches, OpenAI Chat search-only requests, OpenRouter automatic web search, and Groq Compound systems cannot decide this way, so agents using them stay quiet unless a separate judgment backend approves.
After a judgment backend approves, the agent replies normally with its own model, including those provider modes.

### Judgment Backends

`participation.judgment` and `mid_turn.judgment` select a separate decision model, while the agent's configured model still writes the reply.

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
Both backends get the same question and context, but their judgments can differ.

At most eight judgments run at once across the process, including [router judgments](router.md#responder-selection-judgments), and one per agent; a judgment that finds the limit full is not queued and is treated as failed.
Judgment outcome logs record backend, model, decision, latency, token usage, input size, and failure category, plus probability and threshold for TypeSafe, without request text or credentials.

#### Participation Context and Fallback

A participation judgment sends the agent's guidance and up to eight recent user and assistant messages, with Matrix identities replaced by aliases.
System prompts, memory, tool definitions and results, and media are not sent, but message text can still contain private information.
When the context contains media, attachment references, detected secrets, or more than 16 KB of text, the backend is skipped and the reply model decides instead.
Missing credentials, a full limit, a timeout, a provider error, malformed output, or an abstention also fall back to the reply model's decision, and the agent stays quiet if that fails too.

## Mid-Turn Coalescing

When another human message arrives while an agent is responding, MindRoom normally sends the agent a wrap-up notice after its current tool batch, asking it to stop making new tool calls and summarize its progress.
A "thanks" or "is this still running?" can therefore cut a long task short.
Set `agents.<name>.mid_turn` to let a judge decide whether the queued messages can wait until the task finishes.
The setting applies in every room where the agent may reply and has no per-room override; teams do not inherit it from their members.

| Field | Type | Default | Description |
|-------|------|---------|-------------|
| `judgment` | object | Required | LLM model alias or TypeSafe backend; see [Judgment Backends](#judgment-backends) |
| `instructions` | string | `""` | Extra guidance for the continue-or-wrap-up decision |
| `defer_reaction` | string or null | `null` | Reaction such as `"👀"` on a queued message that can wait |

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

The judge lets the task continue for acknowledgements, thanks, "continue", "do not interrupt", and unrelated requests, which wait for a later turn.
It asks for a wrap-up when a message stops, pauses, or corrects the task, changes a relevant requirement, or asks to switch tasks now, even alongside praise.
When the judge is unsure, times out, or fails, the agent gets the wrap-up notice.
With TypeSafe, the task continues only when the probability that no interruption is needed is at least `threshold` (default `0.8`).

The judge sees the active request, recent earlier messages in the thread, up to eight queued messages, the reply text the agent had published when each message arrived, and your `instructions`.
Long earlier messages are shortened and the oldest drop out first, so the request fits the judge's 16 KB limit.
Earlier files and images appear only as their file name and any caption, such as `[file: report.pdf]`.
Attachment contents, tool arguments and results, system prompts, and memory are never sent, but message text can still contain private information.

The agent gets the wrap-up notice without a judgment when:

- the thread's history could not be read completely;
- the active request or a queued message has an attachment or is not typed text, such as a scheduled task or a voice message;
- the active request and queued messages alone exceed 16 KB, or more than eight messages are queued;
- anything the judge would see looks like a credential;
- the agent replies in `thread_mode: room`.

Each skip logs `Mid-turn judgment skipped` with a `reason` such as `history_unavailable` or `essential_input_too_large`, and judge timeouts and errors log `Mid-turn continuation evaluated` with a `failure`.

The check runs between completed tool batches and never interrupts a running tool.
Queued messages are handled after the active response finishes either way.
A decision to continue covers only the messages the judge saw, so a later message gets its own decision, and a wrap-up notice once sent is not taken back.
The wrap-up notice asks the agent to hand off; it does not cancel tools, abort the response, or show the agent the queued text, and stopping a response and tool approval work as usual.
With `defer_reaction` set, each message that can wait gets that reaction once.

## Message Edits

When a user edits a message that already received an agent response, the agent regenerates its reply for the updated content and edits its previous reply in place.
Edits by agents never trigger regeneration.
When another agent's reply finishes with a mention of this agent, it reaches this agent as a new message.
