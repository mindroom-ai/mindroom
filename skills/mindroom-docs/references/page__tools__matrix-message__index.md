# Matrix Message Full Semantics

This page is the complete reference for the `matrix_message` tool.
The model-facing tool description is a condensed summary of these semantics.

Send, reply, react to, read, edit, or inspect Matrix messages using current room and thread context defaults.

Actions:

- send: Send text and optional attachments to a room.
  It defaults to the current room.
  When the effective target is room-level, text+attachment sends post the text to the room timeline and thread attachments under that text event.
  When the effective target is room-level and you send multiple attachments without text, the first attachment is posted to the room timeline and the remaining attachments are threaded under it.
  In `thread_mode: room`, room-level sends stay plain room messages and do not auto-thread attachments unless you pass an explicit `thread_id`.
- reply: Send text and optional attachments into a thread.
  It defaults to the current thread when one can be resolved and errors if no thread is available.
- thread-reply: Same threading behavior as `reply`, kept as a separate action name for agent convenience.
- react: React to `target` with `message` as the emoji, defaulting to thumbs-up when `message` is empty.
- read: Read recent messages from the current thread when one is active, otherwise from the room timeline.
- room-threads: List thread roots in a room with pagination support via `page_token`.
- thread-list: List messages in a thread and include edit options keyed by event ID.
  It uses the current thread when one is active, otherwise you must pass `thread_id`.
- edit: Edit a previously sent message identified by `target`.
  It uses the current thread by default when editing from threaded context.
- context: Return room, thread, reply target, requester, and agent metadata so you can plan a later tool call.

Thread targeting:

- `send` is room-level by default even if the current conversation is inside a thread.
- `send` only creates a new attachment thread when its effective thread target is room-level.
  If you pass an explicit `thread_id`, both text and attachments stay in that existing thread.
- `thread_mode: room` disables implicit attachment auto-threading for room-level sends.
  Pass an explicit `thread_id` when you intentionally want threaded output from the tool.
- `reply` and `thread-reply` inherit the current thread when possible.
- `read`, `edit`, and `context` also inherit the current thread when possible.
- `thread_id="room"` is a sentinel meaning "force room-level scope and do not inherit the current thread."
  Use it when you want the room timeline instead of the active thread.

Mention handling with `ignore_mentions`:

- This flag only affects text sends for `send`, `reply`, and `thread-reply`.
- Default `True`: the tool writes `com.mindroom.skip_mentions=True` into the outgoing event content.
  The bot runtime checks that flag and suppresses mention-triggered agent dispatch, so visible mentions do not page agents.
- `False`: the tool does not set the skip flag, so normal mention handling stays active.
  When the requester is a human rather than the sending bot, the tool also writes `com.mindroom.original_sender=<human requester id>`, not the bot ID.
  Downstream authorization and reply-permission checks then treat the event as coming from the original human requester.
- self-trigger: an agent can mention itself with `ignore_mentions=False` to intentionally create a new turn.
  Use the same pattern for deliberate cross-agent handoffs when another agent should actually wake up and respond.

Safety:

- The default `ignore_mentions=True` exists to prevent accidental infinite loops and noisy mutual paging between agents.
- Set `ignore_mentions=False` only for intentional dispatch.
  Prefer one deliberate handoff message over repeated self-mentions or agent-to-agent pings.

Attachments:

- Attachments are only supported for `send`, `reply`, and `thread-reply`.
- `attachment_ids` are context-scoped `att_*` IDs.
- `attachment_file_paths` are local file paths that will be registered into the current attachment context before sending.
  Relative paths resolve from the agent workspace, the same root used as `HOME` in worker-routed tools.
- The combined limit of `attachment_ids` plus `attachment_file_paths` is 5 per call.
- A send or reply call may include text, attachments, or both, but not neither.

Message extras:

- `message_extras` adds collapsible MindRoom sections to send, reply, thread-reply, and edit events.
- Keep the visible `message` brief; put supporting evidence in extras.
- Each section has `title`, `content`, optional `content_type`, and optional `collapsed`.
- Supported `content_type` values are `text/plain`, `text/markdown`, and `text/html`; default is `text/markdown`.
- HTML content may use sanitized rich fragments: paragraphs, headings, lists, tables, blockquotes, code/pre blocks, basic inline formatting, and links.
  Do not include scripts, styles, images, forms, media, SVG/math, or interactive elements; links should use `http`, `https`, or `mailto`.
- Example: `message_extras=[{"title": "Evidence", "content_type": "text/html", "content": "<table><tr><td>42</td></tr></table>", "collapsed": true}]`.

Args:

- `action` (`str`): Supported actions are `send`, `reply`, `thread-reply`, `react`, `read`, `room-threads`, `thread-list`, `edit`, and `context`; they send text or attachments, react to an event, read messages, list room thread roots or thread messages, edit a prior event, or return targeting metadata.
- `message` (`str | None`): Text body for `send`, `reply`, `thread-reply`, and `edit`; reaction emoji for `react` with a thumbs-up default when empty; use `None` for `read`, `room-threads`, `thread-list`, and `context`.
- `attachment_ids` (`list[str] | None`): Context-scoped `att_*` attachment IDs; only valid for `send`, `reply`, and `thread-reply`, and the combined total with `attachment_file_paths` cannot exceed 5.
- `attachment_file_paths` (`list[str] | None`): Local file paths to register and send in the current context; relative paths resolve from the agent workspace.
  It is only valid for `send`, `reply`, and `thread-reply`, and the combined total with `attachment_ids` cannot exceed 5.
- `room_id` (`str | None`): Optional target room ID or alias; defaults to the current room context when omitted.
- `target` (`str | None`): Event ID to react to for `react` or to edit for `edit`.
- `thread_id` (`str | None`): Optional explicit thread target; `thread_id="room"` forces room-level scope instead of inheriting the current thread.
- `ignore_mentions` (`bool`): Text-send safety flag for `send`, `reply`, and `thread-reply`; default `True` writes `com.mindroom.skip_mentions=True` to suppress mention-triggered agent dispatch, while `False` keeps mentions active and also writes `com.mindroom.original_sender=<human requester id>` when the requester is not the sending bot.
- `message_extras` (`list[dict[str, object]] | None`): Optional collapsible MindRoom sections for supporting evidence.
  Each section supports title, content, content_type (`text/plain`, `text/markdown`, or sanitized `text/html`), and collapsed.
- `limit` (`int | None`): Maximum messages returned for `read` or `thread-list`, or thread roots returned for `room-threads`; defaults to 20 and is capped at 50.
- `page_token` (`str | None`): Pagination token for `room-threads`, returned by a previous `room-threads` call to fetch the next page of thread roots.
