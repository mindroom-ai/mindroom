---
icon: lucide/radio
---

# Streaming Responses

MindRoom streams agent responses to Matrix by progressively editing a single message.
Instead of waiting for the full response, users see text appear in real time as the model generates it.

## How It Works

1. Agent starts generating a response.
2. MindRoom sends an initial message with `io.mindroom.stream_status: pending`, using a plain `Thinking...` placeholder if no text has arrived.
3. As more text arrives, MindRoom edits the same message with the accumulated content and status `streaming`.
4. When the response completes successfully, the final edit sets status `completed`.

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
  status: streaming            (progressive   (sent when
       │                       edits)          complete)
       ▼
  Final edit
  status: completed
```

## Configuration

Streaming is enabled by default.
Disable it globally in `config.yaml`:

```yaml
defaults:
  enable_streaming: false   # Default: true
```

`enable_streaming` is a global-only setting under `defaults` and cannot be overridden per agent.

Tune the streaming edit cadence globally under `defaults.streaming`:

```yaml
defaults:
  enable_streaming: true
  streaming:
    update_interval: 5.0         # Default: 5.0 steady-state seconds between edits
    min_update_interval: 0.5     # Default: 0.5 fast-start seconds between early edits
    interval_ramp_seconds: 15.0  # Default: 15.0; set 0 to disable ramping
    max_idle: 2.0                # Default: 2.0 event-driven idle ceiling before the next edit
    max_live_chars: 1000000      # Default: 1000000; stop progressive edits past this many characters
```

These streaming settings are global-only. Agents inherit them from `defaults` and cannot override them individually.

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

## Stream Status

MindRoom records progress in the Matrix event content field `io.mindroom.stream_status`.
The initial non-final event uses `pending`, and subsequent in-progress edits use `streaming`.
A successful final edit uses `completed`; user cancellation uses `cancelled`, and failures or other interruptions use `error`.
Inspect the latest message content in a client or event view that exposes this metadata to verify completion.

If no text has arrived yet, the message body is a plain `Thinking...` placeholder.
The backend does not append a cycling ellipsis to generated text; clients may render their own progress indicators.

## Throttling

MindRoom throttles edits to avoid overwhelming the Matrix homeserver:

- **Time-based**: `defaults.streaming.update_interval` sets the steady-state interval between edits (default: 5 seconds).
- **Character-based**: An edit is also triggered when enough new characters have accumulated.
  The character threshold ramps from 48 characters (fast start) to 240 characters (steady-state) over the ramp-up period.
- **Ramp-up**: `defaults.streaming.min_update_interval` and `defaults.streaming.interval_ramp_seconds` control how quickly the time-based interval ramps from a fast start to the steady-state value. By default it ramps from 0.5s to 5s over 15 seconds. Setting `interval_ramp_seconds: 0` disables the ramp and uses the steady-state interval immediately.
- **Shared ramp window**: The same ramp window also controls the built-in character threshold ramp from 48 characters (fast start) to 240 characters (steady-state).
- **Minimum interval**: A hard floor (0.35s) applies to character-triggered and idle-triggered throttled edits.
  Time-triggered edits still follow the current ramped interval.
- **Idle flush**: `defaults.streaming.max_idle` triggers an edit on the next streaming event after 2.0s without a new delta, but only once `min_char_update_interval` has also elapsed.
  This is event-driven and does not run on a background timer.
- **Tool-start boundary refresh**: Visible tool-start markers request an immediate refresh so the marker can surface without waiting for later text.
  Rapid back-to-back tool starts are coalesced by the single delivery owner instead of forcing one Matrix edit per tool.
- **Live-update ceiling**: once a streamed response exceeds `defaults.streaming.max_live_chars` characters (default: 1,000,000), MindRoom stops progressive edits and delivers the complete response when the turn ends.
  While the turn keeps running, an ordinary edit still goes out once the previous one is 30 minutes old, so restart recovery can still find the in-progress message.

## Tool Calls During Streaming

When an agent calls tools during a streamed response, MindRoom shows inline markers in the message text:

```
🔧 `web_search` [1] ⏳       ← tool call started (pending)
🔧 `web_search` [1]          ← tool call completed
```

The number in brackets (`[N]`) is a 1-indexed counter per message.
Each marker maps to `io.mindroom.tool_trace.events[N-1]` in the message metadata.

When `show_tool_calls` is disabled for an entity, tool markers are omitted from the message text and tool-trace metadata is not attached.
If a routed tool needs an isolated worker, streaming may still show a generic worker warmup line such as `Preparing isolated worker...`.
That hidden-tool warmup copy never includes tool names or tool-trace metadata.

## Cancellation and Errors

Users can cancel an in-progress response by reacting with 🛑 on the message being generated (see [Stop Button](chat-commands.md#stop-button)).
An explicit user stop finalizes the streamed message with:

```
<partial text so far>

**[Response cancelled by user]**
```

Other non-error interruptions finalize the streamed message with one of these notes:

```
<partial text so far>

**[Response interrupted]**
```

```
<partial text so far>

**[Response interrupted by service restart]**
```

If an error occurs during streaming, the message is finalized with:

```
<partial text so far>

**[Response interrupted by an error: <error description>]**
```

## Large Streamed Messages

If a streamed response exceeds the Matrix event size limit (55KB for new messages, 27KB for edits), the large message system automatically uploads a JSON sidecar and includes a preview in the event body.
With `defaults.large_message_strategy: split`, a splittable final text response is instead delivered as several complete rich-text messages so the whole Markdown answer stays visible without a sidecar.
Payloads that cannot be safely segmented still use the sidecar path.
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
If a routed tool needs an isolated worker, users may still see generic worker progress copy such as `Preparing isolated worker...` or `Preparing isolated worker... 17s elapsed.`.
Hidden-tool mode never includes tool identifiers or tool-trace metadata in that worker progress text.
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
