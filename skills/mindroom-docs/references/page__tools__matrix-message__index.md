# Matrix messages

`matrix_message` lets an agent send, read, edit, and react to Matrix messages, and ask other agents to respond in a visible conversation.
It acts on the current room and thread unless the call selects another one.
For room details, available agents, members, thread discovery, and room state, use [`matrix_room`](https://docs.mindroom.chat/tools/matrix-and-attachments/#matrix_room).
To get another agent's answer back inside the same tool call, use [`run_subagent`](https://docs.mindroom.chat/tools/agent-orchestration/#agent-delegation) instead.

## Setup

Add the tool to an agent; it has no tool-specific configuration fields.
Enabling `matrix_message` also enables `matrix_room` and [`attachments`](https://docs.mindroom.chat/attachments/#the-attachments-tool).

```yaml
agents:
  assistant:
    tools:
      - matrix_message
```

## Common calls

```python
# Post an update in the current conversation.
matrix_message(message="The checks passed.")

# Ask another agent to respond in the current conversation.
matrix_message(recipient="code", message="Review the failing check.")

# Start a separate conversation; keep the returned thread_id.
matrix_message(recipient="code", message="Investigate the regression.", new_thread=True)

# Read or continue that conversation later.
matrix_message(action="read", thread_id="$returned-root")
matrix_message(recipient="code", message="Also check the retry path.", thread_id="$returned-root")

# Send files in their listed order.
matrix_message(message="Results", attachments=["reports/results.txt", "att_screenshot"])

# Edit a message you sent, or react to a message.
matrix_message(action="edit", event_id="$message", message="Corrected results")
matrix_message(action="react", event_id="$message", message="✅")
```

## Agent conversations

<video controls playsinline preload="metadata" aria-label="One agent rolls back a broken deploy and hands the customer update to another" style="width: 100%" poster="https://github.com/user-attachments/assets/a5eb5a08-bd1e-4533-a8ec-71bd462d072c" data-poster-light="https://github.com/user-attachments/assets/a5eb5a08-bd1e-4533-a8ec-71bd462d072c" data-poster-dark="https://github.com/user-attachments/assets/dcd0bb4a-c51b-420f-8e32-f8ce140b1037">
  <source src="https://github.com/user-attachments/assets/2dce4510-bb8a-440b-9bb3-0bcb0d324955" type="video/mp4" media="(prefers-color-scheme: dark)">
  <source src="https://github.com/user-attachments/assets/c4093417-247c-4961-b6bc-85f73f2e9b55" type="video/mp4">
</video>

List possible recipients with `matrix_room(action="agents")` and pass a result's `name` as `recipient`; no Matrix mention is needed.
Only the chosen recipient responds, even if the text or a sent file's name mentions other agents, and without `recipient` no agent is started.
An unknown or unavailable name returns `Recipient '<name>' is not available in this room. Available recipients: ...`.
Recipients can be agents or teams.
An agent can address itself only when a human requested the current turn; otherwise use `run_subagent` for a fresh self-run.

Sending returns immediately with the delivered event IDs; read the conversation later to see the response.

Thread-mode recipients accept `new_thread=True` or an explicit `thread_id`.
A task sent to a thread-mode recipient on the room timeline starts the response thread at the task message, and the returned `thread_id` identifies it.
Room-mode recipients share the room timeline: omit thread options or pass `thread_id="room"`; asking for a separate thread returns an error before anything is sent.

## Choosing the conversation

Without `room_id`, the call targets the current room; without `thread_id`, `send` and `read` use the current conversation.
Selecting another room never carries over the current thread.
Pass `thread_id="room"` to target the room timeline, or `new_thread=True` on `send` to start a separate thread, even from inside a thread.
`new_thread` and `thread_id` cannot be combined.
Calls to other rooms follow the [room access rules](https://docs.mindroom.chat/tools/matrix-and-attachments/#tool-runtime-context).

A successful send returns `room_id`, `thread_id`, and `event_id`; a null `thread_id` means the room timeline.
Keep the returned `thread_id` to read or continue a new thread, because it differs from `event_id` when a file became the thread root.
A new thread may not appear in `matrix_room(action="threads")` until it has a reply.

## Arguments

| Argument | Type | Default | Meaning |
| --- | --- | --- | --- |
| `action` | `"send" \| "read" \| "edit" \| "react"` | `"send"` | Message operation. |
| `message` | `str \| None` | `None` | Text for send/edit; emoji for react, defaulting to 👍. |
| `recipient` | `str \| None` | `None` | Available agent or team name to request a response from; send only. |
| `room_id` | `str \| None` | `None` | Matrix room ID or configured room name; current room by default. |
| `thread_id` | `str \| None` | `None` | Thread root ID, or `"room"` for the room timeline; current conversation by default. |
| `new_thread` | `bool` | `False` | Start a separate thread; send only, cannot be combined with `thread_id`. |
| `event_id` | `str \| None` | `None` | Target message ID; required for edit and react. |
| `attachments` | `list[str] \| None` | `None` | Ordered attachment IDs or file paths; send only, at most five. |
| `message_extras` | `list[object] \| None` | `None` | [Collapsible sections](#collapsible-sections) for send/edit. |
| `idempotency_key` | `str \| None` | `None` | Nonblank key of at most 256 characters for [safe send retries](#safe-send-retries); text-only sends. |
| `limit` | `int \| None` | `None` | Messages to read, clamped to 1–50; default 20. |

Agents with a workspace also see the standard [`mindroom_output_path`](https://docs.mindroom.chat/tools/execution-and-coding/#workspace-and-tool-output-files) argument, which saves the tool result to a file; it does not change where the message goes.

`send` requires text, attachments, or both.
`edit` requires non-empty text and can only edit messages the agent's own account sent.
`read` returns recent messages with their event IDs, and thread reads also list which messages can be edited.
Room-timeline reads skip encrypted messages the agent cannot decrypt, and `limit` counts fetched events, so edits and unreadable messages can leave fewer visible messages than `limit`.
Each agent, room, and requester may make 12 calls per 30 seconds, and each attached file counts as one more call.

## Attachments

Each `attachments` entry is an [attachment ID](https://docs.mindroom.chat/attachments/#attachment-ids) (`att_*`) or a local file path, and files are sent in the listed order.
Local paths are registered as new attachments and follow the same [path rules and limits as `register_attachment`](https://docs.mindroom.chat/attachments/#registering-local-files).
Use `./att_filename` for a local file whose name starts with `att_`.
Every reference is checked before anything is sent.

With a `recipient`, the files arrive before the task text, and the recipient can open them even in a room-mode conversation.
Turn-scoped media, such as ephemeral screenshots, can only go to a recipient in a thread; sending it to a recipient on the room timeline returns an error suggesting `new_thread=True` with a thread-mode recipient or a registered local file.
Without a `recipient`, text comes before its files.
Files go to the selected thread, and room-mode or `thread_id="room"` sends keep them on the room timeline.
When a thread-mode agent sends outside an existing thread, text and files are grouped in a thread under the text, and several files without text are grouped under the first file.

Results include `attachment_event_ids`, `resolved_attachment_ids`, and `newly_registered_attachment_ids`.
If delivery fails partway, the error result still lists the files that arrived, and when a file fails before a recipient is addressed, the task text is not sent.

## Collapsible sections

`message_extras` adds collapsible sections below the main text of a `send` or `edit`:

```json
{
  "title": "Details",
  "content": "Additional information",
  "content_type": "text/markdown",
  "collapsed": true
}
```

`title` and `content` are required strings.
`content_type` defaults to `text/markdown` and also accepts `text/plain` or `text/html`.
`collapsed` defaults to `true`.
A call accepts up to eight sections, with titles of at most 120 characters and content of at most 16,384 characters per section.
Sections require a non-empty main message.
HTML allows basic formatting only, with no scripts, styles, images, forms, media, SVG, math, or interactive elements; links may use `http`, `https`, and `mailto`.

[Interactive prompts](https://docs.mindroom.chat/interactive/) cannot be sent or edited through this tool and return `Interactive prompts are only supported in normal agent responses.`; put them in the agent's normal reply instead.

## Safe send retries

Pass `idempotency_key` on a text-only `send` so that retrying after a timeout or lost tool result never posts a duplicate.

```python
matrix_message(message="Review this result.", recipient="code", new_thread=True, idempotency_key="review-42")
```

A key is scoped to the requester, the sending agent, and the destination room; using it in another room creates a separate send.
A retry with the same key delivers the first call's message, recipient, and thread target, even if the retry's text, recipient, or current thread differs.
Only `status="ok"` confirms delivery; after any error or lost result, keep the key and retry, because an error does not prove the message was not delivered.
Errors that end in `retry with the same idempotency_key.`, such as the 60-second `Idempotent Matrix send timed out; retry with the same idempotency_key.`, are safe to retry.
Retries recheck authorization for the sender and the original recipient, and they still count toward the rate limit.
There is no automatic retry; the agent decides when to retry.

Keys cannot be used with `read`, `edit`, `react`, or attachments.
They need persistent MindRoom storage and the same Matrix device across retries, so keep the storage across restarts.
A send whose device changed fails with `Idempotent Matrix send belongs to another sender or device; refusing unsafe replay.`, and a runtime without the required storage returns `Idempotent Matrix sends require durable control storage and a known Matrix sender and device.`.

Completed sends are remembered for eight days, after which reusing the key may post a new message.
Unfinished sends never expire, so retrying one later still delivers its original message.
Each requester, agent, and room keeps at most 10,000 remembered sends, 1,024 of them unfinished; beyond that, new keys fail with `Matrix message send capacity is exhausted; retry existing keys or wait for completed receipts to expire.` while existing keys keep working.
