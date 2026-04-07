# Matrix & Attachments

Use these tools to work inside the active Matrix room and thread, send follow-up messages, mark threads resolved, and reuse files that belong to the current conversation.

## What This Page Covers

This page documents the built-in tools in the `matrix-and-attachments` group. Use these tools when you need to send or inspect Matrix messages, manage thread resolution or summary state, or handle attachment IDs that are scoped to the current room and thread.

## Tools On This Page

- \[`matrix_message`\] - Send, reply, react, read, edit, or inspect Matrix conversation context.
- \[`thread_resolution`\] - Mark a Matrix thread resolved or unresolved with shared room-state markers.
- \[`thread_summary`\] - Set or update a Matrix thread summary from the current room and thread context.
- \[`attachments`\] - List, inspect, and register context-scoped attachment IDs for later tool calls.

## Common Setup Notes

All four tools depend on the active `ToolRuntimeContext`, so they only work when an agent is running in a Matrix-connected conversation. `matrix_message` implies `attachments` through `Config.IMPLIED_TOOLS`, so enabling `matrix_message` makes the `attachments` toolkit available even when you do not list it separately. Attachment IDs are context-scoped `att_*` values, and the runtime only exposes IDs from the current conversation plus any IDs registered during the current tool run. Current source on this branch exposes `matrix_message`, `thread_resolution`, `thread_summary`, and `attachments` as the registered tools in this area. The issue references `thread_tags.py` and `matrix_api.py`, but those files are not present in this worktree, so they are not documented as standalone tools on this page.

## \[`matrix_message`\]

`matrix_message` is the main Matrix-native tool for sending, reading, reacting to, editing, and inspecting conversation context.

### What It Does

`matrix_message` supports `send`, `reply`, `thread-reply`, `react`, `read`, `thread-list`, `edit`, and `context`. `send` targets the room timeline by default, even when the current conversation is inside a thread. `reply` and `thread-reply` inherit the current thread when one can be resolved, and they return an error when no thread target is available. `read`, `edit`, and `context` also inherit the current thread when one can be resolved, while `thread_id="room"` forces room-level scope instead of thread inheritance. `thread-list` uses the current thread when one is active, and it requires an explicit `thread_id` when there is no active thread context. `react` requires `target` and uses `👍` when `message` is empty. `read` defaults to 20 messages and caps `limit` at 50. `thread-list` returns recent thread messages plus `edit_options` for messages that the current Matrix account can edit. Only `send`, `reply`, and `thread-reply` accept attachments, with a combined cap of five `attachment_ids` plus `attachment_file_paths` per call. The tool rate-limits each `(agent_name, requester_id, room_id)` combination to 12 weighted actions per 30 seconds, where each attachment increases the weight of a send or reply.

### Configuration

This tool has no tool-specific inline configuration fields.

### Example

```
agents:
  assistant:
    tools:
      - matrix_message
```

```
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
- If you need to send existing conversation files, pass `attachment_ids` from the current context or use the `attachments` tool to inspect them first.

## \[`thread_resolution`\]

`thread_resolution` lets agents mark a thread resolved or reopened using shared Matrix state instead of only plain text conventions.

### What It Does

`thread_resolution` exposes `resolve_thread()` and `unresolve_thread()`. Both operations default to the current room and current thread context, and both return an error when no thread can be resolved for the target room. The tool normalizes the supplied event into the canonical thread root before writing state, so replying to any event in the thread still targets the same resolution marker. Resolution state is stored as `com.mindroom.thread.resolution` room state keyed by the canonical thread root event ID. `unresolve_thread(canonical=True)` skips live canonicalization and treats the provided `thread_id` as an already-normalized state key, which is useful when the original event is gone. Writes fail unless both the running Matrix client and the human requester have enough power to send that state event in the target room. When the requester differs from the bot account, the requester must also be joined to the target room.

### Configuration

This tool has no tool-specific inline configuration fields.

### Example

```
agents:
  assistant:
    tools:
      - thread_resolution
```

```
resolve_thread()
unresolve_thread()
resolve_thread(room_id="!ops:example.org", thread_id="$threadRootEvent")
```

### Notes

- This tool writes shared room state, so it is stricter than `matrix_message` about Matrix permissions.
- The returned payload includes `resolved_by`, `resolved_at`, and `updated_at` so a caller can surface who closed or reopened the thread.
- Use `canonical=True` on `unresolve_thread()` only when you already have the canonical state key and do not want the tool to fetch the original event again.

## \[`thread_summary`\]

`thread_summary` lets agents set or replace the current thread summary explicitly instead of waiting for the automatic summarizer.

### What It Does

`thread_summary` exposes `set_thread_summary(summary, thread_id=None, room_id=None)`. The tool defaults to the active room and current thread from `ToolRuntimeContext`. When the agent is replying at room scope, it can still target the correct thread through the current reply context. The tool normalizes the target to the canonical thread root before sending a new `m.notice` summary event with `io.mindroom.thread_summary` metadata. Manual summaries are marked with `model_name="manual"` and update the cached last-summary count so later automatic summaries continue from the new baseline. A per-thread async lock prevents concurrent duplicate manual summaries from racing each other.

### Configuration

This tool has no tool-specific inline configuration fields.

### Example

```
agents:
  assistant:
    tools:
      - thread_summary
```

```
set_thread_summary("Decision: ship the current plan and revisit logs tomorrow.")
set_thread_summary(
    "Summary for the import thread.",
    thread_id="$threadRoot",
    room_id="!ops:example.org",
)
```

### Notes

- `summary` must be a non-empty string up to 500 characters after whitespace normalization.
- The tool writes a normal Matrix notice event, so the updated summary remains visible in the thread timeline.
- Automatic thread summaries still exist, but this tool gives an agent an explicit override path when a human asks for a manual summary refresh.

## \[`attachments`\]

`attachments` lets agents inspect and register files that are scoped to the current Matrix conversation.

### What It Does

`attachments` exposes `list_attachments()`, `get_attachment()`, and `register_attachment()`. `list_attachments()` returns the attachment IDs currently available in tool runtime context, the resolved metadata payloads, and any `missing_attachment_ids`. `get_attachment()` returns a single attachment record, including the local file path that other tools can use. `register_attachment()` turns a local file path into a new context-scoped `att_*` ID and appends that ID to the current runtime context so later tool calls in the same run can reuse it. Attachment records include kind, filename, MIME type, room ID, thread ID, sender, creation time, and an `available` flag that reports whether the local file still exists. This tool does not send files by itself, but its IDs can be passed to `matrix_message` for `send`, `reply`, or `thread-reply`.

### Configuration

This tool has no tool-specific inline configuration fields.

### Example

```
agents:
  assistant:
    tools:
      - attachments
```

```
list_attachments()
get_attachment("att_abc123")
register_attachment("/tmp/plan.pdf")
matrix_message(action="reply", message="Sharing the plan here.", attachment_ids=["att_abc123"])
```

### Notes

- `attachment_id` values must be non-empty `att_*` IDs that are already present in the current tool runtime context.
- Registering a new file attaches it to the current `room_id` and `thread_id`, which prevents accidental reuse across unrelated conversations.
- For the full attachment lifecycle, media kinds, retention rules, and Matrix ingestion flow, use the dedicated [Attachments](https://docs.mindroom.chat/attachments/index.md) guide.

## Related Matrix Runtime Features

Automatic thread summaries are still implemented in `src/mindroom/thread_summary.py` as bot runtime behavior. The summarizer posts one `m.notice` summary after a thread reaches five messages, and then again every ten additional messages, using `defaults.thread_summary_model` or `default`. The `thread_summary` tool complements that automatic behavior by letting an agent publish a manual summary immediately and advance the stored summary baseline.

## Related Docs

- [Tools Overview](https://docs.mindroom.chat/tools/index.md)
- [Attachments](https://docs.mindroom.chat/attachments/index.md)
- [Per-Agent Tool Configuration](https://docs.mindroom.chat/configuration/agents/#per-agent-tool-configuration)
