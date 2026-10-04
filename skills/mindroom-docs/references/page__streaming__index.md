# Streaming Responses

MindRoom streams agent responses to Matrix by progressively editing a single message, so users see text appear as the model generates it.
This page covers when streaming is used, how to tune or disable it, and how streamed messages show tool calls, completion, cancellation, and errors.

## How It Works

1. MindRoom sends an initial message, showing a plain `Thinking...` placeholder until text arrives.
2. As text arrives, MindRoom edits the same message with the accumulated content.
3. When the response finishes, a final edit holds the complete text.

When a response is not streamed, the placeholder is replaced with the complete response in a single edit.

## Configuration

Streaming is enabled by default.
The settings below are global-only under `defaults` and cannot be overridden per agent.

```yaml
defaults:
  enable_streaming: true         # Set false to disable progressive edits
  streaming:
    update_interval: 5.0
    min_update_interval: 0.5
    interval_ramp_seconds: 15.0
    max_idle: 2.0
```

| Field | Type | Default | Description |
|-------|------|---------|-------------|
| `enable_streaming` | bool | `true` | Stream responses via progressive message edits. When `false`, the placeholder is replaced with the complete response in a single edit. |
| `streaming.update_interval` | float, > 0 | `5.0` | Steady-state seconds between edits. |
| `streaming.min_update_interval` | float, > 0 | `0.5` | Seconds between edits at the start of a response. |
| `streaming.interval_ramp_seconds` | float, >= 0 | `15.0` | Seconds over which the interval ramps from `min_update_interval` to `update_interval`. Set `0` to use `update_interval` from the start. |
| `streaming.max_idle` | float, > 0 | `2.0` | When no new text has arrived for this many seconds, the buffered text is shown with the next streaming update. |

Edits also happen sooner than the interval once enough new text has accumulated, starting at 48 characters and rising to 240 over `interval_ramp_seconds`.
Edits triggered by new text or by `max_idle` are at least 0.35 seconds apart.
A tool-call marker appears as soon as the tool starts, and tools that start in quick succession appear together in one edit.
Raise `update_interval` to reduce load on the homeserver, or lower it for smoother updates.

## Presence-Based Streaming

Even when streaming is enabled, MindRoom streams a response only when the user who sent the message is online; everyone in the room sees the same edits.

| Requester presence | Streaming used? |
|--------------------|-----------------|
| `online` | Yes |
| `unavailable` | Yes |
| `offline` | No, the placeholder is replaced with the complete response |

If the presence check fails, for example because the homeserver has presence disabled, MindRoom does not stream.
When no requester can be identified, MindRoom streams.

## Stream Status

Each streamed message carries its progress in the Matrix event content field `io.mindroom.stream_status`.

| Value | Meaning |
|-------|---------|
| `pending` | Initial message of the response |
| `streaming` | Response is still being generated |
| `completed` | Response finished successfully |
| `cancelled` | User stopped the response |
| `error` | Response failed or was otherwise interrupted |

To confirm a response finished, inspect the latest `io.mindroom.stream_status` in a client or event view that shows event content.
MindRoom never appends an ellipsis to generated text, and clients may draw their own progress indicators, so a trailing ellipsis is not a progress signal.

## Tool Calls During Streaming

When an agent calls tools during a streamed response, MindRoom shows inline markers in the message text:

```
🔧 `web_search` [1] ⏳       ← tool call started
🔧 `web_search` [1]          ← tool call completed
```

The number in brackets counts tool calls within the message, and `io.mindroom.tool_trace.events[N-1]` in the event content holds the tool name, argument preview, and result preview for call `[N]`.
To hide markers and tool-trace metadata, set `show_tool_calls: false` (see [Agent Configuration](https://docs.mindroom.chat/configuration/agents/)).
With tool calls hidden, the agent still shows typing activity, and a tool that needs an isolated worker may show generic progress such as `Preparing isolated worker... 17s elapsed.` without naming the tool.

## Cancellation and Errors

Users can cancel an in-progress response by reacting with 🛑 on the message being generated (see [Stop Button](https://docs.mindroom.chat/chat-commands/#stop-button)).
Tool calls running in a [worker](https://docs.mindroom.chat/deployment/sandbox-proxy/), such as `run_shell_command` or `run_python_code`, stop with it, apart from the exceptions listed there.
The partial text stays in the message, followed by a note explaining how it ended:

| Note | Cause |
|------|-------|
| `**[Response cancelled by user]**` | The user stopped the response. |
| `**[Response interrupted by service restart]**` | MindRoom stopped or restarted while the response was being generated. If MindRoom was stopped, the note is added when it starts again. |
| `**[Response interrupted]**` | The response was interrupted for another reason. |
| `**[Response interrupted by an error: <error description>]**` | Generation failed; the description says why. |

## Large Streamed Messages

A response too large for one Matrix event is delivered as a preview with the full content attached, or as several complete messages with `defaults.large_message_strategy: split`.
See [Matrix Integration — Large Messages](https://docs.mindroom.chat/architecture/matrix/#large-messages) for details.
