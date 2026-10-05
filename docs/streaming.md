---
icon: lucide/radio
---

# Streaming Responses

MindRoom streams agent responses to Matrix by progressively editing a single message, so users see text appear as the model generates it.
This page covers when streaming is used, how to tune or disable it, how streamed messages show tool calls, completion, cancellation, and errors, and how oversized responses are delivered.

<video controls playsinline preload="metadata" aria-label="Three colleagues and one agent in a shared thread, each answer streaming live" style="width: 100%" poster="https://github.com/user-attachments/assets/fa232e8f-a26b-425f-b667-433fb12da6ec" data-poster-light="https://github.com/user-attachments/assets/fa232e8f-a26b-425f-b667-433fb12da6ec" data-poster-dark="https://github.com/user-attachments/assets/54a96fc9-91dc-48a7-985c-7d4e41ad9be8">
  <source src="https://github.com/user-attachments/assets/571db927-105b-4907-9c83-ed4f9ef1fc4a" type="video/mp4" media="(prefers-color-scheme: dark)">
  <source src="https://github.com/user-attachments/assets/c913ed8a-4afb-43be-8d90-cdd2aa4158a9" type="video/mp4">
</video>

<a id="streaming-responses_1"></a>

## How It Works

1. MindRoom sends an initial message showing a `Thinking...` placeholder until text arrives.
2. As text arrives, MindRoom edits the same message with the accumulated content.
3. When the response finishes, a final edit holds the complete text.

When a response is not streamed, the placeholder is replaced with the complete response in a single edit.
Agents also show a typing indicator while they work on a response.

## Configuration

Streaming is enabled by default.
These settings are global-only under `defaults` and cannot be overridden per agent.

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

Edits also come sooner than the interval once enough new text has accumulated, and a tool-call marker appears as soon as the tool starts.
Raise `update_interval` to reduce load on the homeserver, or lower it for smoother updates.

## Presence-Based Streaming

Even when streaming is enabled, MindRoom streams a response only when the user who sent the message is `online` or `unavailable`; for an `offline` requester, the placeholder is replaced with the complete response.
Everyone in the room sees the same edits.
If the presence check fails, for example because the homeserver has presence disabled, MindRoom does not stream.
A reply that continues after a restart always streams, even when streaming is off or the requester is offline, so the part it already showed stays in the message.
When no requester can be identified, MindRoom streams.

## Presence

Running agents appear `online` (or `unavailable` when idle) and `offline` when stopped or disconnected.
Their presence status message shows the model, role, and tool count, for example `🤖 Model: anthropic/claude-sonnet-5-5 | 💼 Code assistant | 🔧 5 tools available`.

## Stream Status

Each streamed message carries its progress in the Matrix event content field `io.mindroom.stream_status`.

| Value | Meaning |
|-------|---------|
| `pending` | Initial message of the response |
| `streaming` | Response is still being generated |
| `approval_pending` | Response is paused until someone approves or denies a tool call (see [Tool Approval](tool-approval.md)) |
| `completed` | Response finished successfully |
| `cancelled` | User stopped the response |
| `error` | Response failed or was otherwise interrupted |

To confirm a response finished, inspect the latest `io.mindroom.stream_status` in a client or event view that shows event content.
MindRoom never appends an ellipsis to generated text, and clients may draw their own progress indicators, so a trailing ellipsis is not a progress signal.

## Tool Calls During Streaming

When an agent calls tools, MindRoom shows inline markers in the message text:

```
🔧 `web_search` [1] ⏳       ← tool call started
🔧 `web_search` [1]          ← tool call completed
```

The number in brackets counts tool calls within the message, and `io.mindroom.tool_trace.events[N-1]` in the event content holds the tool name, argument preview, and result preview for call `[N]`.
Tools that start in quick succession appear together in one edit.
Markers and tool-trace metadata are shown by default; to hide them, set `show_tool_calls: false` on one agent or under `defaults` for every agent that leaves it unset (see [Agent Configuration](configuration/agents.md#defaults)).
With tool calls hidden, the agent still shows typing activity, and a tool that needs an isolated worker may show generic progress such as `Preparing isolated worker... 17s elapsed.` without naming the tool.

## Cancellation and Errors

Users can cancel an in-progress response by reacting with 🛑 on the message being generated (see [Stop Button](chat-commands.md#stop-button)).
Tool calls running in a [worker](deployment/sandbox-proxy.md), such as `run_shell_command` or `run_python_code`, stop with it, apart from the exceptions listed there.
The partial text stays in the message, followed by a note explaining how it ended:

| Note | Cause |
|------|-------|
| `**[Response cancelled by user]**` | The user stopped the response. |
| `**[Response interrupted by service restart]**` | MindRoom restarted while the response was being generated; the reply continues below the note once MindRoom is back, or ends there if nothing is left to continue. |
| `**[Response interrupted]**` | The response was interrupted for another reason. |
| `**[Response interrupted by an error: <error description>]**` | Generation failed; the description says why. |

A crash, an orderly shutdown, or an agent restarted by a configuration or MCP change, including during an approved tool call, keeps the partial reply and its tool calls, and the agent continues it in the same message once MindRoom is back.
The agent is told which visible tool calls already finished so it does not repeat side effects; calls hidden by `show_tool_calls: false` cannot be passed on, so it is only warned that they may have run.

## Large Messages

A Matrix event holds at most about 64 KB, so `defaults.large_message_strategy` controls how a larger response is delivered.

| Value | Delivery |
|-------|----------|
| `sidecar` (default) | A preview with as much text as fits, plus the full message content attached as `message-content.json` (`message-content.json.enc`, encrypted, in encrypted rooms). |
| `split` | Several complete rich-text messages: the first replaces the streaming placeholder and the rest follow it in the same thread or room. |

The `sidecar` strategy applies to messages over about 55 KB and edits over about 27 KB, and the preview omits inline tool-trace metadata.
With `split`, each segment renders as standalone Markdown, text is cut at paragraph or line boundaries and never inside a fenced code block, and the segments together reproduce the full response exactly.
Even with `split`, non-text payloads, metadata that alone exceeds the size budget, and a single code block larger than one event use the sidecar.
