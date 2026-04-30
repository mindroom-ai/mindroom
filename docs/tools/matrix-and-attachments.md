---
icon: lucide/wrench
---

# Matrix & Attachments

Use these tools to work inside the active Matrix room and thread, send follow-up messages, manage thread tags and summaries, and reuse files that belong to the current conversation.

## What This Page Covers

This page documents the built-in tools in the `matrix-and-attachments` group.
Use these tools when you need to send or inspect Matrix messages, manage thread tags or summaries, or handle attachment IDs that are scoped to the current room and thread.

## Tools On This Page

- [`matrix_message`] - Send, reply, react, read, edit, or inspect Matrix conversation context.
- [`thread_tags`] - Add, remove, and inspect shared tags on a Matrix thread.
- [`thread_summary`] - Set or update a Matrix thread summary from the current room and thread context.
- [`matrix_api`] - Use a low-level Matrix event and state API with explicit room and event IDs.
- [`attachments`] - List, inspect, and register context-scoped attachment IDs for later tool calls.

## Common Setup Notes

These tools depend on the active `ToolRuntimeContext`, so they only work when an agent is running in a Matrix-connected conversation.
`matrix_message` implies `attachments` through `Config.IMPLIED_TOOLS`, so enabling `matrix_message` makes the `attachments` toolkit available even when you do not list it separately.
Attachment IDs are context-scoped `att_*` values, and the runtime only exposes IDs from the current conversation plus any IDs registered during the current tool run.
Current source in this worktree exposes `matrix_message`, `thread_tags`, `thread_summary`, `matrix_api`, and `attachments` in this area.

## [`matrix_message`]

`matrix_message` is the main Matrix-native tool for sending, reading, reacting to, editing, and inspecting conversation context.

### What It Does

`matrix_message` supports `send`, `reply`, `thread-reply`, `react`, `read`, `thread-list`, `edit`, and `context`.
`send` targets the room timeline by default, even when the current conversation is inside a thread.
When a room-level `send` includes both text and attachments, the text is posted to the room timeline and the attachments are threaded under that new text event.
When a room-level `send` includes multiple attachments and no text, the first attachment is posted to the room timeline and the remaining attachments are threaded under it.
When `send` uses an explicit `thread_id`, both text and attachments stay in that existing thread instead of creating a new attachment thread.
In `thread_mode: room`, room-level `send` stays plain room messaging and does not auto-thread attachments unless you pass an explicit `thread_id`.
`reply` and `thread-reply` inherit the current thread when one can be resolved, and they return an error when no thread target is available.
`read`, `edit`, and `context` also inherit the current thread when one can be resolved, while `thread_id="room"` forces room-level scope instead of thread inheritance.
`thread-list` uses the current thread when one is active, and it requires an explicit `thread_id` when there is no active thread context.
`react` requires `target` and uses `👍` when `message` is empty.
`read` defaults to 20 messages and caps `limit` at 50.
`thread-list` returns recent thread messages plus `edit_options` for messages that the current Matrix account can edit.
Only `send`, `reply`, and `thread-reply` accept attachments, with a combined cap of five `attachment_ids` plus `attachment_file_paths` per call.
The tool rate-limits each `(agent_name, requester_id, room_id)` combination to 12 weighted actions per 30 seconds, where each attachment increases the weight of a send or reply.

### Configuration

This tool has no tool-specific inline configuration fields.

### Example

```yaml
agents:
  assistant:
    tools:
      - matrix_message
```

```python
matrix_message(action="context")
matrix_message(action="send", message="Posting this to the room timeline.", thread_id="room")
matrix_message(
    action="reply",
    message="I reviewed the thread and attached the export.",
    attachment_file_paths=["/tmp/report.csv"],
)
matrix_message(action="react", target="$event123", message="✅")
```

### Notes

- `ignore_mentions` defaults to `True`, which writes `com.mindroom.skip_mentions=True` so visible mentions do not wake other agents accidentally.
- Set `ignore_mentions=False` only for deliberate self-handoffs or cross-agent dispatch, because the tool will preserve normal mention handling and record `com.mindroom.original_sender` for human requesters.
- Use `action="context"` before a follow-up write when you want to inspect the resolved `room_id`, `thread_id`, and `reply_to_event_id`.
- Successful attachment sends also return `attachment_thread_id`, which identifies the thread root used for the uploaded files.
- If you need to send existing conversation files, pass `attachment_ids` from the current context or use the `attachments` tool to inspect them first.

## [`thread_tags`]

`thread_tags` lets agents add, remove, and inspect shared thread tags using Matrix room state.

### What It Does

`thread_tags` exposes `tag_thread()`, `untag_thread()`, and `list_thread_tags()`.
All three operations default to the current room and active resolved thread context.
When there is no active resolved thread context, pass `thread_id` explicitly.
The tool normalizes the supplied event into the canonical thread root before reading or writing state.
Tags are stored as `com.mindroom.thread.tags` room state.
Each `(thread_root_id, tag)` pair uses its own state event, and the state key is the JSON array `[thread_root_id, tag]`.
Writes fail unless both the running Matrix client and the human requester have enough power to send that state event in the target room.
When the requester differs from the bot account, the requester must also be joined to the target room.

### Configuration

This tool has no tool-specific inline configuration fields.

### Example

```yaml
agents:
  assistant:
    tools:
      - thread_tags
```

```python
tag_thread("blocked")
untag_thread("blocked")
list_thread_tags(thread_id="$threadRootEvent")
```

### Notes

- This tool writes shared room state, so it is stricter than `matrix_message` about Matrix permissions.
- Tag writes and removals return the updated canonical tag state for the target thread.
- `list_thread_tags()` can inspect the active thread or an explicitly provided `thread_id`.

## [`thread_summary`]

`thread_summary` lets agents set or replace the current thread summary explicitly instead of waiting for the automatic summarizer.

### What It Does

`thread_summary` exposes `set_thread_summary(summary, thread_id=None, room_id=None)`.
The tool defaults to the active room and current resolved thread from `ToolRuntimeContext`.
When there is no active resolved thread context, pass `thread_id` explicitly.
The tool normalizes the target to the canonical thread root before sending a new `m.notice` summary event with `io.mindroom.thread_summary` metadata.
Manual summaries are marked with `model_name="manual"` and update the cached last-summary count so later automatic summaries continue from the new baseline.
A per-thread async lock prevents concurrent duplicate manual summaries from racing each other.

### Configuration

This tool has no tool-specific inline configuration fields.

### Example

```yaml
agents:
  assistant:
    tools:
      - thread_summary
```

```python
set_thread_summary("Decision: ship the current plan and revisit logs tomorrow.")
set_thread_summary(
    "Summary for the import thread.",
    thread_id="$threadRoot",
    room_id="!ops:example.org",
)
```

### Notes

- `summary` must be a non-empty string up to 300 characters after whitespace normalization.
- The tool writes a normal Matrix notice event, so the updated summary remains visible in the thread timeline.
- Automatic thread summaries still exist, but this tool gives an agent an explicit override path when a human asks for a manual summary refresh.

## [`matrix_api`]

`matrix_api` exposes a small low-level Matrix API surface for explicit room, event, and state operations, including room-scoped search.

### What It Does

`matrix_api` supports `send_event`, `get_state`, `put_state`, `redact`, `get_event`, and `search`.
It defaults `room_id` to the active room, but it also supports authorized cross-room access when the requester is allowed to act there.
It never infers thread IDs, event IDs, or state keys from thread context, so callers must pass those identifiers explicitly for low-level operations.
`send_event`, `put_state`, and `redact` are rate-limited per `(agent_name, requester_id, room_id)` and audited in logs.
Dangerous state event types like `m.room.power_levels` and `m.room.encryption` are blocked by default.
Pass `allow_dangerous=true` only when you intentionally want to change critical room state.
Hard-blocked state event types like `m.room.create` remain blocked.
`search` is read-only, scopes results to one room via `room_id`, uses the top-level `limit` parameter, and rejects `filter.limit`.
When `event_context={"include_profile": true}` is requested, returned context preserves `profile_info` for matching senders.

### Configuration

This tool has no tool-specific inline configuration fields.

### Example

```yaml
agents:
  assistant:
    tools:
      - matrix_api
```

```python
matrix_api(action="get_event", event_id="$event123")
matrix_api(action="get_state", event_type="m.room.topic")
matrix_api(
    action="put_state",
    event_type="com.example.marker",
    state_key="status",
    content={"value": "ready"},
)
matrix_api(action="redact", event_id="$event123", reason="Cleanup")
matrix_api(
    action="search",
    search_term="deployment incident",
    keys=["content.body"],
    event_context={"before_limit": 1, "after_limit": 1, "include_profile": True},
)
```

### Notes

- Use this tool when you need exact Matrix event or state control rather than the higher-level `matrix_message` convenience actions.
- Use `action="search"` when you need one-room full-text event search without falling back to homeserver-wide or ad-hoc history scans.
- The tool returns structured JSON payloads for both success and error cases.
- Because it is intentionally low-level, it requires explicit IDs instead of deriving them from reply or thread context.

## [`attachments`]

`attachments` lets agents inspect and register files that are scoped to the current Matrix conversation.

### What It Does

`attachments` exposes `list_attachments()`, `get_attachment()`, and `register_attachment()`.
`list_attachments()` returns the attachment IDs currently available in tool runtime context, the resolved metadata payloads, and any `missing_attachment_ids`.
`get_attachment()` returns a single attachment record, including the runtime-local path, when called with only an attachment ID.
`get_attachment(attachment_id, mindroom_output_path="relative/path")` saves the attachment bytes into the agent workspace and returns a `mindroom_tool_output` save receipt with the saved path, byte count, binary format, and SHA256 digest.
Use `mindroom_output_path` before handing attachments to worker-routed workspace tools such as `file`, `coding`, `python`, or `shell`, because the runtime-local path may not exist inside the worker workspace.
The path must be relative to the workspace and must not be empty, absolute, point at the workspace root, contain `..` or NUL bytes, or use environment or user expansion.
`register_attachment()` turns a local file path into a new context-scoped `att_*` ID and appends that ID to the current runtime context so later tool calls in the same run can reuse it.
Attachment records include kind, filename, MIME type, room ID, thread ID, sender, creation time, and an `available` flag that reports whether the local file still exists.
This tool does not send files by itself, but its IDs can be passed to `matrix_message` for `send`, `reply`, or `thread-reply`.

### Configuration

This tool has no tool-specific inline configuration fields.

### Example

```yaml
agents:
  assistant:
    tools:
      - attachments
```

```python
list_attachments()
get_attachment("att_abc123")
get_attachment("att_abc123", mindroom_output_path="incoming/plan.pdf")
register_attachment("/tmp/plan.pdf")
matrix_message(action="reply", message="Sharing the plan here.", attachment_ids=["att_abc123"])
```

### Notes

- `attachment_id` values must be non-empty `att_*` IDs that are already present in the current tool runtime context.
- Registering a new file attaches it to the current `room_id` and `thread_id`, which prevents accidental reuse across unrelated conversations.
- For the full attachment lifecycle, media kinds, retention rules, and Matrix ingestion flow, use the dedicated [Attachments](../attachments.md) guide.

## Related Matrix Runtime Features

Automatic thread summaries are still implemented in `src/mindroom/thread_summary.py` as bot runtime behavior.
The summarizer posts one `m.notice` summary after a thread reaches the configured first threshold (one message by default), and then again every ten additional messages by default, using `defaults.thread_summary_model` or `default`.
MindRoom uses `defaults.thread_summary_temperature` for automatic summaries when the provider supports runtime temperature overrides, and always omits temperature for Vertex Claude summaries.
The `thread_summary` tool complements that automatic behavior by letting an agent publish a manual summary immediately and advance the stored summary baseline.

## Related Docs

- [Tools Overview](index.md)
- [Attachments](../attachments.md)
- [Per-Agent Tool Configuration](../configuration/agents.md#per-agent-tool-configuration)
