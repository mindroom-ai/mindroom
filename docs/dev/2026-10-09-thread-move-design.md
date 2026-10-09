# Moving a Thread to Another Room

## Goal

Let a user ask an agent to move the current thread into another room, so threads can be organized by room after they started.
The request came from a user who keeps related work in separate rooms and found that thread tags alone did not help them organize it.
Success means the moved thread reads like the original in the target room, the agents in it continue the conversation there without being re-briefed, and the original thread points to the new location.

## Constraints

- Matrix cannot move events: an event's room is part of its signed hash, so a "move" is always a copy plus a pointer.
- Copies must never trigger agent replies while they are posted.
- Requester authorization follows the existing cross-room tool rule: a human requester joined to the target room and allowed to address the acting agent there.
- The feature must stay small: no new storage, no durable move journal, no changes to session storage.

## Approaches Considered

1. **Faithful copy without session copy (chosen).**
   Each managed entity's messages are re-posted by that entity's own Matrix account; all other messages are re-posted by the router with relay attribution.
   A thread without saved session history already replays its whole Matrix transcript as model history (`_prepare_execution_context_common` in `execution_preparation.py`), with each agent's own copies as assistant turns and relayed copies labeled by `com.mindroom.original_sender`.
   Agents therefore continue the conversation in the new thread with no session work.
   They lose earlier tool-call results and any compaction summary, but keep the full visible conversation.
2. **Faithful copy plus Agno session copy (rejected).**
   Copying sessions would preserve tool-call results, but every agent, team scope, and private instance has its own session database, and copied runs embed Matrix event IDs (seen-event sets, response IDs, source revisions, compaction archives) and globally unique run IDs that would all need remapping.
   Without that remapping the copied history and the copied messages are replayed twice.
   The added code and failure surface are large compared with the benefit.
3. **Single-account copy (rejected).**
   The router re-posts every message with a name prefix.
   This is the least code, but agents would see their own earlier replies as user messages and would no longer count as thread participants, so untagged follow-ups in the new thread would stop reaching them.

## Design

### Tool Surface

A new built-in tool `thread_move` exposes one function:

```python
move_thread(room_id: str, thread_id: str | None = None)
```

- `room_id` is the target room as a Matrix room ID, alias, or configured room key, resolved with `resolve_requested_room_id`.
- `thread_id` is a thread root or reply event ID in the current room; it defaults to the current thread and is normalized to the thread root like the other thread tools.
- The source is always the current room.
- Registration follows `thread_resolution`: `ToolFileAccess.NONE`, `ToolCategory.COMMUNICATION`, `requires_room_context=True`, no default approval.

### Preconditions

All checks run before anything is written, and each failure returns an error payload:

1. A runtime context exists and a source thread root resolves.
2. The target room resolves to a room ID and differs from the current room; an alias MindRoom does not manage resolves through the homeserver, and anything else is reported as an unknown room.
3. The router is running and joined to the target room, and the acting agent is joined to the target room.
   Membership comes from one `get_room_members` call on the router's client, and it is checked before access because an agent missing from the target room cannot read its members, which the access check would report as a denial.
4. `room_access_allowed(context, target_room_id)` passes.
5. `complete_thread_history(context.conversation_reader, source_room_id, root_id)` returns `is_full_history=True`; otherwise the thread is too long to move.
6. The move is refused when the source room is encrypted and the target room is not, so end-to-end encrypted content is never re-posted in plaintext.

### Copy Plan

A pure function turns the thread history into an ordered list of planned posts.
Each plan entry holds the posting entity name and the content without its thread relation.

- **Skipped messages:** `m.notice` messages (thread summaries, compaction and other runtime notices, earlier move notices) and messages whose `io.mindroom.stream_status` is `pending`, `streaming`, or `approval_pending` (replies still in progress, including the acting agent's current reply).
- **Poster selection:** a sender that maps to a managed entity (`entity_identity_registry(...).current_entity_name_for_user_id`) whose bot is running and joined to the target room posts its own copy.
  Every other sender, including humans and entities missing from the target room, is posted by the router.
- **Content allowlist:** only `msgtype`, `body`, `format`, `formatted_body`, `url`, `file`, `info`, `filename`, `geo_uri`, the voice-note markers `org.matrix.msc3245.voice` and `org.matrix.msc1767.audio`, and `io.mindroom.tool_trace` are copied, so run metadata, stream state, delivery IDs, attachment IDs, interactive and model-selection payloads, and relay keys never carry over.
- **Trigger guard:** every copy gets `com.mindroom.skip_mentions: true` and an empty `m.mentions`.
  Agent-posted copies are additionally ignored as unmentioned managed-sender messages, and a bot ignores its own messages.
- **Router copies:** they carry `com.mindroom.original_sender` set to the original sender, without any `com.mindroom.source_kind`, so they are attributed in prompts but never treated as a human turn.
- **Managed relays:** a managed sender's message that already carries `com.mindroom.original_sender`, such as the router's voice transcript, keeps that attribution on its copy and names that person visibly, like every other copy that speaks for someone other than its poster; a human's message cannot claim another author.
  Because clients do not render `original_sender`, text copies get a visible `Name: ` prefix in `body` and a bold name prefix in `formatted_body`.
  Media copies keep their media fields, set `filename` to the original filename (or the original body when there is none), and use `Name: <caption or filename>` as the body, which Matrix clients show as the caption.
  Names come from `room_member_display_names` for the source room, falling back to the Matrix user ID.

### Execution

1. Post the plan in order.
   The first post is the new thread root and has no relation; every later post gets `build_thread_relation(new_root_id, latest_thread_event_id=<previous copy's event ID>)`.
   Sends use `send_message_result` with the posting entity's client, which handles encryption and re-uploads oversized bodies as sidecars.
2. Copy every tag of the source thread to the new root with `set_thread_tag(context.client, target_room_id, new_root_id, tag, set_by=requester_id, data=...)`.
3. Post an `m.notice` in the source thread from the acting agent: `Moved to <permalink>`.
4. Tag the source thread `resolved` with `set_thread_tag`.

Permalinks are `https://matrix.to/#/<room_id>/<event_id>?via=<server>`, percent-encoded, with `via` taken from the acting agent's homeserver because room IDs in recent room versions carry no server name.

### Clients for Other Entities

`OrchestratorRuntime` gains one wiring method, `running_entity_client(entity_name) -> nio.AsyncClient | None`, which returns the client of a running managed bot.
The tool reaches it through `ToolRuntimeContext.orchestrator`; no tool imports `bot.py`.

### Errors

- A send failure stops the move and returns an error naming how many messages were copied and the partial new thread's link; the source thread is left untouched.
- A failure in steps 2 to 4 (a `ThreadTagsError` or a failed notice send) does not fail the move, because the copy is already complete and retrying would duplicate it; the result lists each failure in `warnings`.
- A thread whose messages are all skipped is refused before anything is posted.
- No durable move journal exists, so a crash mid-copy leaves a partial copy and an untouched source thread.

### Result Payload

`status`, `room_id` and `thread_id` of the new thread, `link`, `copied` and `skipped` counts, `tags_copied`, and `warnings`.

## What Does Not Move

Saved agent sessions (tool-call results and compaction summaries), per-thread model choices, agent modes, todos, scheduled tasks, external triggers, pending approvals, and attachment records stay with the source thread.
Media messages are re-posted and registered again as attachments when agents read the new thread.
Messages posted in the source thread after the move starts are not copied.

## Known Limitations

- Copies have new timestamps, edits are flattened to their latest revision, and reactions and in-thread reply targets are not copied.
- Router-posted human copies do not count as distinct humans, so a moved thread that had several humans behaves like a single-human thread until those humans post again.

## Testing

- Copy plan unit tests: skip rules, poster selection, content allowlist, attribution prefixes for text and media, trigger guard keys.
- Tool tests in the style of `tests/test_thread_resolution_tool.py`: registration, no context, no thread, same room, access denied, incomplete history, encrypted-to-plain refusal, router or acting agent missing from the target room, a full success path asserting send order, poster, content, and relations, tag copy, source notice, and `resolved` tag, a mid-copy send failure, and a tag failure reported as a warning.
- `running_entity_client` test on the orchestrator.
- Live test with the `live-test` skill: move a thread with a human and two agents between two rooms, confirm no agent replies to the copies, confirm an untagged follow-up in the new thread reaches the original agent with the earlier conversation in context, and confirm the source thread shows the link and the `resolved` tag.

## Documentation

- `docs/tools/matrix-and-attachments.md`: a `thread_move` section stating the function, preconditions a user can hit, and what does not move.
- `docs/tools/index.md` and `docs/dev/agent_configuration.md`: list the tool.
- `docs/architecture/code-map.md`: add the new module row.
