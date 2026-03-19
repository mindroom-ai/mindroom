---
icon: lucide/radio
---

# Streaming Responses

MindRoom streams agent responses to Matrix by progressively editing a single message.
Instead of waiting for the full response, users see text appear in real time as the model generates it.

## How It Works

1. Agent starts generating a response.
2. MindRoom sends an initial message with the first chunk of text plus an in-progress marker (`⋯`).
3. As more text arrives, MindRoom edits the same message with the accumulated content.
4. When the response is complete, the final edit removes the `⋯` marker.

```
User sends message
       │
       ▼
┌──────────────┐     presence check
│ Agent starts │ ──────────────────▶ Is user online?
│ generating   │                          │
└──────┬───────┘                    ┌─────┴─────┐
       │                           Yes          No
       ▼                            │            │
  Stream chunks                     ▼            ▼
  via edits                    Streaming     Single message
  with ⋯ marker               (progressive   (sent when
       │                       edits)          complete)
       ▼
  Final edit
  (⋯ removed)
```

## Configuration

Streaming is enabled by default.
Disable it globally in `config.yaml`:

```yaml
defaults:
  enable_streaming: false   # Default: true
```

`enable_streaming` is a global-only setting under `defaults` and cannot be overridden per agent.

## Presence-Based Streaming

Even when streaming is enabled, MindRoom only streams to users who are currently online.
This is checked via `should_use_streaming()` which queries the Matrix presence API.

| Presence State | Streaming Used? |
|----------------|-----------------|
| `online`       | Yes             |
| `unavailable`  | Yes             |
| `offline`      | No (single message sent when complete) |

If the presence check fails, MindRoom defaults to non-streaming (safer, fewer API calls).
When no requester user ID is available, MindRoom defaults to streaming.

## In-Progress Marker

While a response is being generated, the message ends with ` ⋯` followed by zero to two dots that cycle as edits arrive.
This gives users a visual indicator that the agent is still working.

```
Hello! I can help you with that ⋯
Hello! I can help you with that ⋯.
Hello! I can help you with that ⋯..
Hello! I can help you with that ⋯
```

If no text has arrived yet, a `Thinking...` placeholder is shown with the marker.
The marker is removed on the final edit.

## Throttling

MindRoom throttles edits to avoid overwhelming the Matrix homeserver:

- **Time-based**: Edits are spaced by a configurable interval (default: 5 seconds steady-state).
- **Character-based**: An edit is also triggered when enough new characters have accumulated.
  The character threshold ramps from 48 characters (fast start) to 240 characters (steady-state) over the ramp-up period.
- **Ramp-up**: Early in the stream, both time intervals and character thresholds start low and ramp up to steady-state values over 15 seconds (time intervals from 0.5s to 5s, character threshold from 48 to 240).
- **Minimum interval**: A hard floor (0.35s) prevents edit spam even when character thresholds are met.

## Tool Calls During Streaming

When an agent calls tools during a streamed response, MindRoom shows inline markers in the message text:

```
🔧 `web_search` [1] ⏳       ← tool call started (pending)
🔧 `web_search` [1]          ← tool call completed
```

The number in brackets (`[N]`) is a 1-indexed counter per message.
Each marker maps to `io.mindroom.tool_trace.events[N-1]` in the message metadata.

When `show_tool_calls` is disabled for an entity, tool markers are omitted from the message text and tool-trace metadata is not attached.
The agent still shows typing activity during hidden tool calls.

## Cancellation and Errors

Users can cancel an in-progress response by reacting with 🛑 on the message being generated (see [Stop Button](chat-commands.md#stop-button)).
When cancelled, the streamed message is finalized with:

```
<partial text so far>

**[Response cancelled by user]**
```

If an error occurs during streaming, the message is finalized with:

```
<partial text so far>

**[Response interrupted by an error: <error description>]**
```

## Large Streamed Messages

If a streamed response exceeds the Matrix event size limit (55KB for new messages, 27KB for edits), the large message system automatically uploads a JSON sidecar and includes a preview in the event body.
See [Matrix Integration — Large Messages](architecture/matrix.md#large-messages) for details.

## Visibility Toggles

Two global defaults control what users see during streaming:

```yaml
defaults:
  show_tool_calls: true     # Default: true — show inline tool markers and tool-trace metadata
  show_stop_button: true    # Default: true — add 🛑 reaction for cancellation
```

When `show_tool_calls` is `false`, inline tool markers (`🔧 tool_name [N]`) are omitted from the message text and `io.mindroom.tool_trace` metadata is not attached.
The agent still shows typing activity during hidden tool calls.
`show_tool_calls` can also be overridden per agent in the agent config.

When `show_stop_button` is `false`, the 🛑 reaction is not added to in-progress messages.
Streaming itself still works — only the cancellation affordance is removed.
`show_stop_button` is a global-only setting under `defaults`.

`enable_streaming` is also global-only and cannot be overridden per agent.

## Room Mode

When an agent operates in `thread_mode: room` (see [Thread Mode Resolution](configuration/agents.md#thread-mode-resolution)), streaming skips all thread relations and sends plain room messages.
This is used for bridges and mobile clients that don't support Matrix threads.

## Replacement Streaming

MindRoom also supports a `ReplacementStreamingResponse` variant where each chunk replaces the entire message content instead of appending to it.
This is used for structured live rendering where the full document is rebuilt on each tick.
