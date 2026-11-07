# Threads, Replies & Participation

This page covers how agents group replies into threads or one room conversation, whether a message must mention an agent or team to start a conversation, when agents answer in threads with several people, how agents can opt into judging untagged messages, what happens to messages sent while an agent is still working, and how edits and restarts affect replies.
See also [Routing & Responder Selection](https://docs.mindroom.chat/configuration/router/), [Access Control](https://docs.mindroom.chat/authorization/), and [Logs & Monitoring](https://docs.mindroom.chat/deployment/operational-log-events/).

## Thread Mode Resolution

Each agent's `thread_mode` and `room_thread_modes` fields (see [Agent Configuration](https://docs.mindroom.chat/configuration/agents/)) choose between Matrix threads and one continuous conversation per room.
A room override set with [`!thread_mode`](#thread_mode) takes precedence for every agent, team, and the router in that room.
Without one, an agent uses the `room_thread_modes` entry matching the current room by Matrix room ID, room key, or a managed room's full alias, and otherwise its `thread_mode`.

A team uses its members' mode for the room when all members agree, and `thread` when they differ.
The router uses the mode shared by all agents relevant to the room, including agents that join through `teams.<name>.rooms`, and `thread` when they differ.

MindRoom publishes each managed room's effective reply placement in room state event `io.mindroom.thread_mode`, with an empty state key and content `{"mode": "room"}`, `{"mode": "thread"}`, or `{"mode": "mixed"}`.
`room` means all joined responding agents and teams use the main timeline, `thread` means all use threads, and `mixed` means both modes are present.
Rooms without joined responders are left unchanged.
When a room has configured responders, other visiting agents and teams do not affect its mode.
The router updates this state during setup and reconciliation, after configuration or `!thread_mode` changes, when it joins a room by any path, including an invite, and when responders join or leave, so clients can choose the appropriate conversation view.
MindRoom writes the event only when its current value differs from the effective mode.
Setting or resetting `!thread_mode` reconciles the resulting mode; `show` only reads the override.
Unavailable room-state permissions do not prevent setup or replies.

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
Room-mode conversations are not included in [thread exports](https://docs.mindroom.chat/thread-exports/).
`!thread_mode thread` uses Matrix threads for separate conversations in this room.
`!thread_mode reset` (also `clear`) removes the room override so agents use their configured `thread_mode` and `room_thread_modes` again.
Only Matrix room admins can set or reset the override; others get `Room admin only.`
The override is stored in MindRoom's runtime state under `mindroom_data/tracking`, not in `config.yaml`, so it works when config is static or read-only.

## Reply Mode

A room's reply mode decides whether a message that mentions no agent or team can start a conversation.
Set it with `rooms.<key>.reply_mode` (see [Room policy](https://docs.mindroom.chat/rooms/#room-policy)) or with [`!reply_mode`](#reply_mode).

- `auto` (default): the router or the room's only agent or team answers such a message, as described in [Routing & Responder Selection](https://docs.mindroom.chat/configuration/router/#how-routing-works) and [Multi-Human Thread Protection](#multi-human-thread-protection).
- `mentions`: neither the router nor any agent or team answers such a message, unless it is in a thread an agent or team already replied in or is still answering, where the rules below apply as in `auto`.
  The router still shows the transcript, or the fallback text, of such a voice message ([router echo](https://docs.mindroom.chat/voice/#voice-message-processing)) unless `voice.visible_router_echo` is `false`.

```yaml
rooms:
  lobby:
    reply_mode: mentions
```

A message that mentions an agent or team is answered the same way in both modes, and mentions count as described in [Multi-Human Thread Protection](#multi-human-thread-protection).
Editing a message that got no answer to add a mention does not get one; send a new message that mentions the agent or team. A message still waiting behind a reply in progress is read with its latest edit, so an edit there that adds a mention does count.
Scheduled tasks, hooks, and external triggers are not affected, and a direct-message room follows its reply mode like any other room.
With `thread_mode: room`, every message is room-level, so each one needs a mention in a `mentions` room.

### `!reply_mode`

Show or switch the reply mode of the current Matrix room.

```
!reply_mode
!reply_mode show
!reply_mode mentions
!reply_mode auto
!reply_mode reset
```

`!reply_mode` and `!reply_mode show` (also `list` or `status`) show the mode the room uses and whether it is an override.
`!reply_mode mentions` and `!reply_mode auto` set an override that takes precedence over `rooms.<key>.reply_mode`.
`!reply_mode reset` (also `clear`) removes the override so the configured mode applies again.
Only Matrix room admins can set or reset the override; others get `Room admin only.`
The override is stored in MindRoom's runtime state under `mindroom_data/tracking`, not in `config.yaml`, so it also sets the mode of a room without a `rooms` entry.
The router answers `!reply_mode`, so in a room without the router, such as a direct-message room with one agent, set the mode with a `rooms` entry keyed by the room's `!` room ID instead, for example `rooms: {"!abc123:example.com": {reply_mode: mentions}}`; the router then also joins that room when it can.

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
A hook or tool send with [`mentions`](https://docs.mindroom.chat/hooks/#sending-messages) mentions only the users listed there.
Mentions of users who are not in the room or only invited do not suppress normal responses.
Mentioning a configured agent that has not joined the room still counts as an explicit mention for the other agents.

A thread MindRoom cannot read in full, because it holds more than 2,000 messages or 16 MiB of stored content or because reading it failed, is not continued from an untagged message when the room has more than one possible responder, unless an agent is still answering there; mention the agent or team to continue it.
In a `mentions` room such a thread needs a mention even when the room has one possible responder.

### Bot accounts

Any Matrix user that is not a MindRoom agent or team counts as a human for these rules, including bridge bots (Telegram, Slack, etc.) and other non-MindRoom bots.
A bridge bot relaying messages into a thread therefore looks like a second human and makes agents require mentions.
List such accounts in the top-level [`bot_accounts`](https://docs.mindroom.chat/authorization/#bot-accounts) field so their messages and mentions do not count toward multi-human detection.

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
| `provider` | all | Required | `llm` for a configured model alias, `typesafe` for System One, or `openai_decisions` for the OpenAI Decisions API |
| `model` | `llm` | Required | Existing alias under `models`, which can be cheaper than the reply model |
| `threshold` | `typesafe`, `openai_decisions` | `0.8` | Minimum probability from `0` to `1`; rejected for `llm` |
| `credentials_service` | `openai_decisions` | `null` | Credential service holding the OpenAI API key, instead of the OpenAI model credential |
| `timeout_seconds` | all | `5` for `llm`, otherwise `1.5` | Positive deadline of at most `30` seconds; for participation it starts after `debounce_seconds` |

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

To use the OpenAI Decisions API with `gpt-6-luna`, set `provider: openai_decisions` instead.
It uses the same OpenAI API key as OpenAI models, from `OPENAI_API_KEY` or the dashboard, and always calls `api.openai.com`, regardless of any model `base_url`.
When OpenAI models go through a proxy, that credential holds the proxy's key, which `api.openai.com` rejects; set `credentials_service` to a service holding a real OpenAI key instead.
A named service that has no key skips the backend; it never falls back to the OpenAI model credential.
No environment variable fills a custom service name, so add its `api_key` in the dashboard or through [credential seeds](https://docs.mindroom.chat/oauth-framework/#credential-seeds).

The LLM backend uses the alias's normal provider credentials and receives no tools, agent system prompt, or agent memory.
A model alias whose provider adds native tools that cannot be disabled is refused.
For participation, a TypeSafe or OpenAI Decisions probability at or above `threshold` approves; the default `0.8` has not been calibrated on representative conversations.
All backends get the same question and context, but their judgments can differ.

At most eight judgments run at once across the process, including [router judgments](https://docs.mindroom.chat/configuration/router/#responder-selection-judgments), and one per agent; a judgment that finds the limit full is not queued and is treated as failed.
Judgment outcome logs record backend, model, decision, latency, token usage, input size, and failure category, plus probability and threshold for TypeSafe and OpenAI Decisions, without request text or credentials.

#### Participation Context and Fallback

A participation judgment sends the agent's guidance and up to eight recent user and assistant messages, with Matrix identities replaced by aliases.
System prompts, memory, tool definitions and results, and media are not sent, but message text can still contain private information.
When the context contains media, attachment references, detected secrets, or more than 16 KB of text, the backend is skipped and the reply model decides instead.
Missing credentials, a full limit, a timeout, a provider error, malformed output, or an abstention also fall back to the reply model's decision, and the agent stays quiet if that fails too.

## Mid-Turn Coalescing

When another human message arrives while an agent is responding, MindRoom normally sends the agent a wrap-up notice after its current tool batch, asking it to stop making new tool calls and summarize its progress.
A "thanks" or "is this still running?" can therefore cut a long task short.
These messages send the agent no wrap-up notice and get no ⏳ reaction from it: one that mentions only other agents or people who have joined the room (unless it asks for a team the agent answers for), and one outside any thread that a `mentions` room leaves unanswered, because it mentions no agent or team (see [Reply Mode](#reply-mode)).
A person's edit of a text message still waiting in the coalescing queue decides it again, though a ⏳ already on it stays; an edited media caption, or a message not yet queued, is not decided again.
A voice message counts as waiting while it is transcribed, so a tool batch that ends before its transcript is ready still gets the notice; once the transcript shows it is for someone else, a notice not yet sent is dropped and no ⏳ appears.
Set `agents.<name>.mid_turn` to let a judge decide whether the queued messages can wait until the task finishes.
The setting applies in every room where the agent may reply and has no per-room override; teams do not inherit it from their members.

| Field | Type | Default | Description |
|-------|------|---------|-------------|
| `judgment` | object | Required | LLM model alias, TypeSafe, or OpenAI Decisions backend; see [Judgment Backends](#judgment-backends) |
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
With TypeSafe or OpenAI Decisions, the task continues only when the probability that no interruption is needed is at least `threshold` (default `0.8`).

The judge sees the active request, recent earlier messages in the thread, up to eight queued messages, the reply text the agent had published when each message arrived, and your `instructions`.
Long earlier messages are shortened and the oldest drop out first, so the request fits the judge's 16 KB limit.
Earlier files and images appear only as their file name and any caption, such as `[file: report.pdf]`.
Tool calls appear as `<tool name="..." input="..." output="..."/>` tags built from the previews the chat shows in its tool details: up to 1,200 characters of input and 500 of output per call, with recognized secrets already removed.
Attachment contents, full tool output, system prompts, and memory are never sent, but message text and tool previews can still contain private information.

The agent gets the wrap-up notice without a judgment when:

- the thread's history could not be read completely;
- the active request or a queued message has an attachment or is not typed text, such as a scheduled task or a voice message;
- the active request and queued messages alone exceed 16 KB, or more than eight messages are queued;
- anything the judge would see looks like a credential;
- the agent replies in `thread_mode: room`.

Each skip logs `Mid-turn judgment skipped` with a `reason` such as `history_unavailable` or `essential_input_too_large`, and judge timeouts and errors log `Mid-turn continuation evaluated` with a `failure`.

The check runs between completed tool batches and never interrupts a running tool.
With background tool jobs enabled, the judge also decides whether a queued message moves a waiting tool call to the background; see [When a call becomes a job](https://docs.mindroom.chat/tools/agent-orchestration/#when-a-call-becomes-a-job).
Queued messages are handled after the active response finishes either way.
A reply waiting for a [tool approval](https://docs.mindroom.chat/tool-approval/) counts as responding, so messages queue behind it until the approval ends.
A decision to continue covers only the messages the judge saw, so a later message gets its own decision, and a wrap-up notice once sent is not taken back.
The wrap-up notice asks the agent to hand off without new tool calls; it does not cancel running tools or show the agent the queued text, tool calls the agent makes in answer to it end the response without running, and stopping a response and tool approval work as usual.
With `defer_reaction` set, each message that can wait gets that reaction once.

## Message Edits

When a user edits the latest message of a conversation, the agent regenerates its reply to that message for the updated content and edits the reply in place.
If that reply is still streaming, the edit stops it first and the regenerated answer replaces it.
A text message edited while it still waits to be gathered with the messages sent around it is answered as edited; an edit made after that, before the agent starts sending its reply, leaves the original text as the one answered.
Once someone other than an agent writes again in the conversation, an edit of an earlier message changes no reply.
While a reply waits for a tool approval, an edit of its message cancels that approval and regenerates the reply.
Edits by agents never trigger regeneration, and an agent's message edited while it still waits is answered with its original text.
When another agent's reply finishes with a mention of this agent, it reaches this agent as a new message.
