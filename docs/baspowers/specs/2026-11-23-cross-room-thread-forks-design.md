# Cross-Room Thread Forks Design

**Date:** 2026-11-23
**Repositories:** `mindroom-ai/mindroom` (backend), `mindroom-ai/mindroom-chat` (client)

## Intent & Success Criteria

Let a user fork an existing thread (from its latest message or from any earlier message) into **another room** — for example, moving active conversations from `MindRoom-Dev` into `Neon-Dev` (which uses a different `room_models` default) without copying messages.

Success criteria:
1. From any thread (or message in a thread) in Room A, the user picks **Fork to room…**, selects an eligible joined Room B, previews Room A's conversation up to the fork point inside Room B's draft view, and sends their next message to create a new top-level thread in Room B.
2. Nothing is copied on Matrix: Room B's fork root carries `io.mindroom.fork: { room_id, thread_id, event_id }`. Room A stays unchanged.
3. MindRoom Chat renders Room A's messages (read-only, with Room A's names/avatars/media/permalinks) above a `"Forked from <title> in <Room A>"` divider that jumps back to Room A, and shows the fork badge on Room B's Compact card.
4. MindRoom backend prepends Room A's messages up to the fork point for agents in Room B when authorized, uses **Room B's** model defaults (not Room A's thread model overrides), routes cleanly using Room B's responders, scopes Room A's attachments under their true `(room_id, thread_id)`, copies Room A's title with ` (forked)`, and informs the model which room and thread it was forked from.
5. Cross-room access control is fail-closed: an unauthorized user or mismatched audience/encryption boundary cannot exfiltrate messages, titles, or attachments from Room A into Room B.

## Protocol: `io.mindroom.fork`

Extend the existing `io.mindroom.fork` object on a fork root's message content with an optional `room_id`:

```json
"io.mindroom.fork": {
  "room_id": "!sourceRoom",
  "thread_id": "$parentRoot",
  "event_id": "$forkPoint"
}
```

- `room_id` names the **immediate parent's room** and is **omitted** for same-room forks (100% backwards-compatible with existing forks).
- Validation: if `room_id` is absent, resolve in the room of the fork root currently being examined. If `room_id` is present, require a valid Matrix room ID string starting with `!` and containing no whitespace (supporting server-less v12 room IDs, matching `isRoomId` in MindRoom Chat). Reject the entire fork reference on malformed/null `room_id` rather than falling back to same-room.
- On send: omit `room_id` when `targetRoomId === sourceRoomId`.
- On edit (`keepThreadForkRef`) and long-text preview (`buildLongTextPreview`): preserve `room_id` intact.
- Chains (`A -> B -> C`): resolve each hop's omitted `room_id` relative to **that hop's own room** (not the final destination), key `visited` by `(room_id, thread_id)`, keep max depth 8, and stop cleanly on missing fork points, cycles, or denied hops.

## Backend Architecture (`mindroom-ai/mindroom`)

### 1. Cross-room authorization gate (fail-closed, single shared policy)

Every cross-room parent read (`with_fork_prefix` in `ConversationResolver`, `matrix_message(action="read")`, and provisional title reads in `thread_summary/automatic.py`) passes through one shared authorization helper before reading parent history, titles, or attachments.

For any hop crossing into `parent_room_id != target_room_id`:
1. **No contact rooms**: deny if either `target_room_id` or `parent_room_id` is a contact room.
2. **Reading agent membership**: the reading client must be currently joined to `parent_room_id` (not merely holding stale projection rows).
3. **Encryption boundary**: block encrypted (or unknown) `parent_room_id` -> unencrypted (or unknown) `target_room_id` (checked against the final output room `target_room_id` for every ancestor in a chain). Allow `encrypted -> encrypted`, `unencrypted -> encrypted`, `unencrypted -> unencrypted`.
4. **Current requester membership**: the turn's effective human requester (`is_human_requester_id`, checked across `equivalent_requester_ids`) must be currently joined to `parent_room_id`. Missing or non-human requester fails closed (`DENIED`).
5. **Fork-root author membership**: for each cross-room edge, the authenticated Matrix sender of that fork root (resolving `ORIGINAL_SENDER_KEY` only when sent by a trusted managed sender) must be a human principal currently joined to `parent_room_id`.
6. **Audience boundary**: every non-managed human member (joined or invited) of `target_room_id` must be currently joined to `parent_room_id`, and `target_room_id` must not have `world_readable` history visibility. This prevents an agent's reply, persisted session, or title in a shared target room from disclosing source-room content to an outsider.
7. **Non-blocking vs strict reads**: in `ThreadReadMode.NONBLOCKING` snapshots, if cached room membership or encryption state is incomplete, return `DEFERRED` (marking the snapshot degraded so dispatch falls back to `STRICT`). Once evaluated, a `DENIED` cross-room hop stops the walk cleanly **without** setting `is_full_history=False` or `degraded=True`, so the child thread remains fully usable in `target_room_id` without the denied prefix. On denial or strict-refresh failure, never fall back to an earlier expanded snapshot.

### 2. Data model, provenance & attachments

- Add optional `room_id: str | None = None` to `ResolvedVisibleMessage`. `with_fork_prefix` stamps `room_id=parent_room_id` on every message from a parent segment (and may leave own messages `None` or stamp `room_id`).
- Replace `fork_parent_thread_ids` scoping with `fork_parent_scopes(history, thread_id, *, room_id) -> frozenset[tuple[str, str]]`.
- Update `attachments/storage.py` (`filter_attachments_for_context`, `register_thread_history_media_attachments`, `_attachment_record_in_message_scope`), `attachments/media.py`, `inbound_turn_normalizer.py`, `execution_preparation.py` (`_ThreadAttachmentContext`), and `custom_tools/attachments.py` (`_view_attachment`):
  - Register and look up parent-room media under its true `(parent_room_id, parent_thread_id)` so on-disk attachment records never have their `room_id` overwritten.
  - Scope turn attachments to `(room_id, thread_id)` plus authorized `parent_scopes` (matching actual history messages inside the cut), and allow `_view_attachment` to view attachments already vetted in `context.attachment_ids`.

### 3. Model inheritance, routing, titles & prompt context

- **Model inheritance**: skip parent thread model copying (`_apply_root_thread_model` and `_inherit_fork_thread_models` in `turns/dispatch.py`) when the fork is cross-room (`parent_room_id != room_id`). Target room's `room_models`, room override, entity default, or explicit opening model pick governs.
- **Routing**: keep destination-local participation for cross-room prefixes:
  - `has_multiple_non_agent_users_in_thread` ignores cross-room prefix messages (messages whose `room_id` differs from the target room) so a multi-human parent forked into a 1-human target room does not suppress the sole agent or router.
  - `turn_policy.py`'s `_reply_mode_awaits_mention` checks agents available in the target room rather than counting source-room-only agents.
- **Provisional title**: read `current_thread_summary` from `parent_room_id` only when authorized by the shared cross-room gate; for a coalesced first turn whose cross-room parent is unavailable or denied, fall back to the prompt's own text.
- **Prompt context & display names**:
  - Add `CROSS_ROOM_THREAD_FORK_CONTEXT_TEMPLATE` in `prompts.py` (leaving `THREAD_FORK_CONTEXT_TEMPLATE` untouched for same-room forks and user overrides), with allowed fields `{"parent_room_id", "parent_room_label", "parent_thread_id", "fork_point_event_id"}`, explicitly noting that per-thread settings (including the model) were not carried over.
  - Emit the fork context enrichment only when the fork point event is present in `thread_history` (prefix actually loaded).
  - Merge `member_display_names` from authorized parent rooms as fallback behind `target_room` display names so source-only senders show human-readable names.
- **`matrix_message(action="read")`**: apply the same cross-room auth gate in `_thread_read_payload`, include `room_id` on cross-room prefix message payloads, and omit `edit_options` for cross-room prefix messages.

## Client Architecture (`mindroom-ai/mindroom-chat`)

### 1. Entry points & room picker

- Keep 1-click **Fork thread** (thread menu) and **Fork from here** (message menu) for same-room forks.
- Add **Fork to room…** directly beside both in `ThreadActionsMenu` and `Message` menu.
- Clicking **Fork to room…** opens a compact, keyboard-navigable searchable room picker (`ForkToRoomDialog`) over joined non-archived rooms (`navigationRoomsAtom`, ordered by recent visits via `getRecentRooms`), excluding:
  - spaces,
  - the source room itself,
  - tombstoned rooms and rooms where the user cannot send `m.room.message`,
  - `room`-thread-mode rooms (`getRoomThreadMode(room) === 'room'`),
  - unencrypted rooms when the source room is encrypted.
- If a candidate target room has MindRoom agents (`isMindroomAgentUserId`), show a subtle warning indicator when **none** of the target room's MindRoom agents are joined to the source room (so the user knows beforehand that agents there cannot read the parent prefix).

### 2. Cross-room draft route & `ThreadView`

- Picking a target room navigates to the **target room** with dedicated query params:
  `<targetRoomPath>?forkRoom=<sourceRoomId>&forkThread=<parentThreadId>&forkAt=<eventId>`
  (leaving `threadId` unset so `Room.tsx` / `RoomView.tsx` never treat `forkThread` as a thread belonging to `targetRoom`).
- Validate all three params (`isRoomId`, `isEventId`, `isEventId`) in `Room.tsx` and `nativeUniversalLinks.ts`. If invalid or `mx.getRoom(forkRoom)` is missing, do not open a broken draft.
- `ThreadView` splits read scope (`sourceRoom`, `forkThread`, `forkAt`, `sourcePowerLevels`) from send scope (`targetRoom`, `RoomInput` with `forkFrom: { roomId: sourceRoom.roomId, threadId: forkThread, eventId: forkAt }`).
- A read-only draft header shows `"Forking from <sourceRoomName>"` with a Back button (and an inline warning if no MindRoom agent in `targetRoom` is joined to `sourceRoom`).
- `ThreadForkDivider` (`variant="forking"`) shows `"Forking <title> into <targetRoomName>"`.
- Sending the first message posts a top-level event in `targetRoom` with `io.mindroom.fork: { room_id: sourceRoomId, thread_id: forkThread, event_id: forkAt }` and replaces the route with the newly created thread in `targetRoom`.

### 3. Rendering sent cross-room forks

- `loadThreadForkPrefix` and `useThreadForkPrefix` resolve `hopRoom = mx.getRoom(hopRoomId)` per hop, key `visited` by `${roomId}\0${threadId}`, load cache/backfill/root/decryption/edits/redactions from `hopRoom` (never writing foreign events to `targetRoom`'s cache), and refresh/clear on membership changes.
- `useThreadRenderState` preserves segment order (prefix before own events) so cross-homeserver clock skew cannot reorder child events ahead of prefix events.
- `forkPrefixTimelineSet.ts`, `useTimelineMessageFeature.tsx`, and `Message.tsx` resolve each prefix row's owning `eventRoom = mx.getRoom(event.getRoomId()!) ?? room` for display names, avatars, permalinks, reply quotes, media/voice/cards, URL preview encryption checks, `onOpenInOriginalThread`, sender profile clicks, quiet markers, and re-forking (`forkTargetFor` preserves `roomId`).
- `ThreadForkDivider` and `CompactThreadCard` resolve `parentRoom = parent.roomId ? mx.getRoom(parent.roomId) : room`:
  - Accessible cross-room parent: `"Forked from <title> in <parentRoomName>"` (clicking divider navigates to `(parentRoomId, parentThreadId, parentEventId)`).
  - Inaccessible cross-room parent: static `"Forked from another room"` (shown above the fork root even when prefix is empty).
- Reverse fork chips (`useThreadForks`) remain room-local in v1 (ignoring fork roots whose `room_id` names another room).

## Testing Strategy

- **Backend (`mindroom`)**: unit tests in `tests/test_thread_forks.py`, `tests/test_conversation_resolver.py`, `tests/test_thread_models.py`, `tests/test_thread_summary.py`, `tests/test_turn_dispatch_pipeline.py`, `tests/test_response_runner_agent.py`, `tests/test_bot_media_dispatch.py`, `tests/test_attachments_tool.py`, and `tests/test_matrix_message_tool.py` covering cross-room chains (`A -> B -> C` with omitted-room hop), every fail-closed auth check (agent/author/requester/outsider/encryption/contact-room/non-degrading denial), model non-inheritance, routing with source-only agents & multi-human parents, cross-room attachments without overwriting `room_id`, provisional titles, and prompt context.
- **Client (`mindroom-chat`)**: unit tests covering `parseThreadForkRef` / `threadForkContent` / `keepThreadForkRef`, `loadThreadForkPrefix` / `useThreadForkPrefix` across rooms, `useThreadRenderState` ordering, `useTimelineMessageFeature` per-row `eventRoom`, `ThreadForkDivider` / `CompactThreadCard` cross-room & inaccessible labels, `ForkToRoomDialog` filtering & agent-membership warnings, `Room`/`ThreadView`/`nativeUniversalLinks` draft params, plus Playwright e2e spec `e2e/threads/cross-room-thread-fork.spec.ts` with screenshots for the PR.
