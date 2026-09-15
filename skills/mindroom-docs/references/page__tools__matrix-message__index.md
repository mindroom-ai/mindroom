# Matrix messages

`matrix_message` sends, reads, edits, and reacts to messages.
It uses the current conversation by default.
Use `matrix_room` for room details, available agents, members, thread discovery, and room state.
Both tools are available when `matrix_message` is enabled.

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

Discover available recipients with `matrix_room(action="agents")`.
Each result includes `name`, `matrix_user_id`, `description`, and `thread_mode`.
Pass the `name` as `recipient`; no manual Matrix mention or dispatch flag is needed.
You may address yourself for a human requester, or address an available team.
Self-messaging without a human requester returns an error because own-agent ingress ignores it; use `run_subagent` for a fresh self-run when allowed.
Only the chosen recipient is dispatched, even if the message mentions other agents.
Without `recipient`, text mentions do not start agent turns.
Recipient discovery checks the current requester, authorization, room membership, and agent availability.

Sending returns immediately with the delivered event IDs; read the conversation later for its response.
For a bounded task whose answer should return within the same tool call, use [run_subagent](https://docs.mindroom.chat/configuration/agents/#agent-delegation).
Matrix conversations and local subagent runs have separate lifecycles.

Thread-mode recipients support `new_thread=True` and explicit thread IDs.
Room-mode recipients share the room conversation: omit thread options or use `thread_id="room"`.
Requesting a separate thread from a room-mode recipient returns an error before sending.

## Conversation selection

Omitted `room_id` selects the current room.
Omitted `thread_id` selects the current conversation for both `send` and `read`.
Selecting another room never inherits the current room's thread.
Use `thread_id="room"` to explicitly select the room timeline, including for files.
Use `new_thread=True` with `send` to create a separate conversation, even when already inside a thread.
Do not combine `new_thread` with `thread_id`.

A successful send returns `room_id`, `thread_id`, and `event_id`.
Keep the returned `thread_id` to read or continue a new thread; it can differ from `event_id` when a file created the root.
A null `thread_id` means the room timeline.
For a thread-mode recipient, a task sent to the timeline starts its response thread at the task event, so the returned `thread_id` identifies that response conversation.
Room thread discovery uses `matrix_room(action="threads", limit=20)` and its returned `next_token` as `page_token` for the next page.
New roots may not appear in discovery until they have a reply.
`matrix_room(action="room-info")` includes the current thread and reply event IDs, requester ID, and agent name alongside room metadata.

## Arguments

| Argument | Type | Default | Meaning |
| --- | --- | --- | --- |
| `action` | `"send" \| "read" \| "edit" \| "react"` | `"send"` | Message operation. |
| `message` | `str \| None` | `None` | Text for send/edit; emoji for react, defaulting to 👍. |
| `recipient` | `str \| None` | `None` | Available agent/team name to request a response from; send only. |
| `room_id` | `str \| None` | `None` | Matrix room ID or configured room name; current room by default. |
| `thread_id` | `str \| None` | `None` | Thread root ID, or `"room"` for the room timeline; current conversation by default. |
| `new_thread` | `bool` | `False` | Start a separate thread; send only, mutually exclusive with `thread_id`. |
| `event_id` | `str \| None` | `None` | Required target message ID for edit/react. |
| `attachments` | `list[str] \| None` | `None` | Ordered attachment IDs or file paths; send only, maximum five. |
| `message_extras` | `list[object] \| None` | `None` | Collapsible sections for send/edit; schema below. |
| `idempotency_key` | `str \| None` | `None` | Nonblank key, at most 256 characters; durable text-only send retries. |
| `limit` | `int \| None` | `None` | Read count, clamped to 1–50; default 20. |

Workspace-backed agents may also see MindRoom's standard `mindroom_output_path` argument, which saves the tool result and returns a file receipt.
It controls the returned tool output, not where the Matrix message is delivered.

`send` requires text, attachments, or both.
`edit` requires non-empty text and can only edit the sending account's messages.
`read` returns message event IDs; thread reads also include edit options for editable messages.
Room access checks apply before cross-room operations.
Calls are rate limited to 12 actions per 30 seconds per agent, room, and requester; each file costs one additional action.

## Durable send retries

Pass `idempotency_key` on a text-only `send` to retry safely after a timeout or interrupted tool call.
Keys are scoped to the canonical requester, acting agent, and resolved destination room; room and requester aliases share the same scope.
The first prepared payload, recipient, and thread target are saved before delivery, including formatting, mentions, and `message_extras`.
Retrying that key reuses the saved payload and Matrix transaction ID even if the text, recipient, or current thread changes.
The destination room remains part of the key scope: using a different room creates a separate send.

```python
matrix_message(message="Review this result.", recipient="code", new_thread=True, idempotency_key="review-42")
```

A successful result keeps the normal `status="ok"`, `event_id`, `room_id`, and `thread_id` fields.
Only `status="ok"` confirms both Matrix delivery and a durable local receipt; retain the key and retry after any error or lost response.
An error does not prove that Matrix rejected the message.
Authorization for the sender and the original recipient is checked again on every retry, including completed receipts.
A changed Matrix sender or device fails closed because its transaction IDs may no longer deduplicate the original send.
These calls need durable control storage and a known Matrix sender and device.

Completed receipts are retained for eight days, and the prepared message body is discarded after receipt storage.
Pending sends never expire; retrying after eight days still uses their original payload.
After a completed receipt expires, reusing its key may create a new event.
Pruning happens when the requester/agent/room store is next used.
Keep the storage across restarts and use the same Matrix device for retries.
Rate limits also apply to retries; a rate-limit error requires waiting before retrying the same key.
Keys cannot be used for `read`, `edit`, `react`, or attachment sends.
There is no automatic retry worker; the caller controls retries.

## Attachments

Each `attachments` entry is a context-scoped `att_*` ID or a local file path.
Relative paths resolve from the agent workspace when configured.
Use `./att_filename` for a local filename that starts with `att_`.
All references are resolved before any message is sent.
Files retain their input order.

When `recipient` is set, files arrive before the text that requests the agent's response.
The task message also carries trusted attachment references, allowing room-mode recipients to access registered files without thread history.
Turn-scoped encrypted media, such as ephemeral screenshots, requires a threaded recipient conversation; room-timeline recipient sends reject it before delivery and suggest a registered local file instead.
For a new thread, the first file can become its root.
Without a recipient, text precedes its attachments.
Files share the selected thread; a room-mode or explicit room-timeline send keeps files in the timeline.
A thread-mode send outside an existing thread groups text and files under the text, or multiple files under the first file.

Results include `attachment_event_ids`, `resolved_attachment_ids`, and `newly_registered_attachment_ids`.
Delivery can fail after some files have arrived; error results retain those IDs.
If any file fails before recipient dispatch, the tool does not send the task text.

## Collapsible sections

Each `message_extras` object has this schema:

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
A call supports up to eight sections; titles are limited to 120 characters and content to 16,384 characters per section.
Sections require a non-empty main message body.
HTML supports basic fragments only, with no scripts, styles, images, forms, media, SVG, math, or interactive elements; links permit `http`, `https`, and `mailto`.
Direct send/edit calls reject interactive prompts; use normal agent response delivery for interactive prompts.
