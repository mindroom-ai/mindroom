---
icon: lucide/wrench
---

# Matrix & Attachments

Use these tools to work inside the active Matrix room and thread, send follow-up messages, manage thread tags, resolution, summaries, and model overrides, and reuse files that belong to the current conversation.

## What This Page Covers

This page documents the built-in tools in the `matrix-and-attachments` group.
Use these tools when you need to send or inspect Matrix messages, manage thread tags, resolution, summaries, or model overrides, or handle attachment IDs that are scoped to the current room and thread.

## Tools On This Page

- [`matrix_message`](matrix-message.md#matrix-messages) - Send, read, edit, and react in Matrix conversations.
- [`matrix_room`] - Inspect Matrix room metadata, available agents, members, thread roots, and room state.
- [`matrix_voice_message`] - Generate speech from text and send it as a Matrix voice note.
- [`thread_tags`] - Add, remove, and inspect shared tags on a Matrix thread.
- [`thread_resolution`] - Explicitly resolve or reopen Matrix threads in the current room.
- [`thread_summary`] - Set or update a Matrix thread summary from the current room and thread context.
- [`thread_model`](../configuration/models.md#thread_model) - List models or show, switch, and reset the model override for the current Matrix thread.
- [`matrix_api`] - Use a low-level Matrix event and state API with explicit room and event IDs.
- [`attachments`](../attachments.md#the-attachments-tool) - List, inspect, and register context-scoped attachment IDs for later tool calls.

## Common Setup Notes

These tools depend on the active `ToolRuntimeContext`, so they only work when an agent is running in a Matrix-connected conversation.
`matrix_message` implies `attachments` and `matrix_room` through `Config.IMPLIED_TOOLS`, so enabling it makes both companion toolkits available even when you do not list them separately.
Attachment IDs are context-scoped `att_*` values, and the runtime only exposes IDs from the current conversation plus any IDs registered during the current tool run.
Current source in this worktree exposes `matrix_message`, `matrix_room`, `matrix_voice_message`, `thread_tags`, `thread_resolution`, `thread_summary`, `thread_model`, `matrix_api`, and `attachments` in this area.

## Tool Runtime Context

Tools like `matrix_message`, `matrix_room`, `chat_ui`, `thread_tags`, `thread_resolution`, and `matrix_api` use this context to act on the correct room and canonical thread without the caller passing explicit IDs.
`thread_tags` can also target another authorized room, but it still checks the target room's canonical thread root and requester membership before writing the shared tag state.
`thread_tags.tag_thread()` and `thread_tags.untag_thread()` still use the active thread when the caller explicitly repeats the current `room_id`.
`thread_tags.list_thread_tags()` uses the active thread by default, but passing `room_id` without `thread_id` forces room-wide listing even from inside an active thread.
`thread_tags.list_thread_tags(tag=...)` narrows both thread-specific and room-wide responses to the requested tag only.
`thread_tags.list_thread_tags(include_tag=..., exclude_tag=...)` filters which threads are returned: `include_tag` keeps only threads with that tag, `exclude_tag` removes threads with that tag.
Both can be combined.
Unlike `tag` (which narrows the output payload), these filter which threads appear at all.
`thread_tags.list_thread_tags(exclude_tag="resolved", include_untagged=True)` lists unresolved room threads, including threads that have no tag state yet.
`include_untagged=True` forces a room-wide query and cannot be combined with `thread_id`.
It enumerates Matrix `/threads` and may stop at the 2000-root safety cap.
The response includes `include_untagged: bool` and `truncated: bool`.
Callers must check `truncated` before claiming the unresolved list is complete.
`thread_tags` also validates and normalizes predefined payload schemas for `blocked.data.blocked_by`, `waiting.data.waiting_on`, `priority.data.level`, and `due.data.deadline`.
`thread_resolution` is an explicit opt-in capability backed by the `resolved` thread tag and does not read the removed experimental `com.mindroom.thread.resolution` event type.
It is absent from starter configs and default tool sets, while `thread_tags` can list but cannot mutate `resolved` state.
`matrix_api` defaults `room_id` to the active room, supports cross-room targeting for rooms the requester is joined to, never infers event IDs or state keys from thread context, and now also supports room-scoped full-text search through `action="search"`.

See [`matrix_message`](matrix-message.md#matrix-messages).

## [`matrix_room`]

`matrix_room` provides read-only room introspection through `matrix_room(action="room-info", room_id=None, limit=None, event_type=None, state_key=None, page_token=None)`.

### What It Does

The supported actions are `room-info`, `members`, `agents`, `threads`, and `state`.
`room-info` returns cached room metadata including name, topic, encryption status, membership count, join rule, canonical alias, version, guest access, creator, and a power-level summary.
It also includes current `thread_id`, `reply_to_event_id`, `requester_id`, and `agent_name`; inspecting another room does not expose the origin thread.
`members` returns joined users with display names, avatar URLs, and power levels.
`agents` returns the agents currently eligible to answer this requester in the selected room as a sorted `agents` array with `name`, `matrix_user_id`, `description`, and `thread_mode` (`thread` or `room`).
It includes the caller when eligible and applies current authorization, configured room scope, and live responder availability.
These are conversation targets; the separate `run_subagent` tool describes the caller's allowed subagents.
Pass each returned `name` as `recipient` in [matrix_message](matrix-message.md#agent-conversations) to address that agent.
`threads` returns paginated thread-root previews with sender, timestamp, reply count, and latest activity when available; it defaults `limit` to 20, clamps it from 1 through 50, and returns `next_token` plus `has_more` for pagination.
`state` returns one exact state event when `event_type` is supplied, using an empty `state_key` by default.
Without `event_type`, `state` returns a room-state summary with at most 100 non-member event previews and elides `m.room.member` events.
`room_id` defaults to the active Matrix room.
An alternate room is allowed only when the requester has access to the agent under the configured room-access policy and is currently joined to that room.
The tool requires an active Matrix `ToolRuntimeContext` and rate-limits each `(agent_name, requester_id, room_id)` combination to 20 actions per 30 seconds.

### Configuration

This tool has no tool-specific inline configuration fields.
It can be listed explicitly, and `matrix_message` also enables it automatically through `Config.IMPLIED_TOOLS`.

### Example

```yaml
agents:
  assistant:
    tools:
      - matrix_room
```

```python
matrix_room(action="room-info")
matrix_room(action="members")
matrix_room(action="agents")
matrix_room(action="threads", limit=10)
matrix_room(action="threads", page_token="next-page-token")
matrix_room(action="state", event_type="m.room.topic")
```

### Notes

- `page_token` applies to `threads`, while `event_type` and `state_key` apply to `state`.
- This tool reads room data only; use `matrix_message` or `matrix_api` for supported writes.

## [`matrix_voice_message`]

`matrix_voice_message` lets agents generate speech from text and send it as a Matrix voice message in one tool call.

### What It Does

`matrix_voice_message(text, room_id=None, thread_id=None, caption=None, companion_message=None)` calls the configured text-to-speech endpoint and sends one Opus `m.audio` event with Matrix voice-note metadata.
When both `room_id` and `thread_id` are omitted, it targets the active Matrix room and active thread.
Pass `thread_id="room"` to force room-level delivery.
Use `caption` for the audio event body and `companion_message` for a separate readable text event in the same target.

### Configuration

The optional tool configuration fields are `api_key`, `model`, `base_url`, `voice`, and `response_format`.
By default `matrix_voice_message` uses OpenAI text-to-speech through an explicit or stored credential, or `OPENAI_API_KEY` / `OPENAI_API_KEY_FILE`.
Provider-prefixed model IDs such as `hexgrad/kokoro-82m` route through OpenRouter and use an explicit `api_key` or `OPENROUTER_API_KEY`; OpenRouter output is always requested as MP3.
Set the `base_url` tool config (or the `TTS_URL` secret) to target any OpenAI-compatible speech endpoint instead, such as a local Kokoro server.
When a non-OpenRouter `base_url` is configured without an explicit `api_key`, the tool sends a dummy API key so the real OpenAI credential never leaves the machine.
`base_url` accepts a bare host, a `/v1` URL, or a full `/audio/speech` endpoint URL, and full endpoint URLs are honored verbatim.
Defaults: `model=gpt-4o-mini-tts`, `voice=alloy`, `response_format=opus`.
`response_format` must be one of `aac`, `flac`, `mp3`, `opus`, or `wav`, matching what the endpoint returns.
Generated audio is probed with `ffprobe` and, when not already Opus/Ogg, transcoded with `ffmpeg` into a Matrix voice payload with duration and waveform metadata.
Non-opus response formats therefore require `ffmpeg` and `ffprobe` on the backend host; opus output still works without them but is sent without duration metadata.

### Example

```yaml
agents:
  assistant:
    tools:
      - matrix_voice_message
```

```python
matrix_voice_message("Here is the quick audio version.")
matrix_voice_message(
    "The build finished successfully.",
    companion_message="The build finished successfully.",
)
```

### Notes

- The tool returns `event_id` for the voice event and `companion_event_id` when companion text was sent.
- The tool rate-limits each `(agent_name, requester_id, room_id)` combination to six voice sends per 30 seconds.

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
list_thread_tags(exclude_tag="resolved", include_untagged=True)
```

### Notes

- This tool writes shared room state, so it is stricter than `matrix_message` about Matrix permissions.
- Tag writes and removals return the updated canonical tag state for the target thread.
- `tag_thread()`, `untag_thread()`, and every `list_thread_tags()` tag filter share one normalizer: valid canonical IDs up to 50 characters are preserved, while other free-form input is coerced to a short lowercase hyphenated tag.
- `resolved` is lifecycle state rather than a topic tag, so `tag_thread()` and `untag_thread()` reject it.
- Use the separately configured `thread_resolution` tool when an agent should be allowed to resolve or reopen threads.
- `list_thread_tags()` can inspect the active thread or an explicitly provided `thread_id`.
- `list_thread_tags(include_tag=..., exclude_tag=...)` filters which threads are returned: `include_tag` keeps only threads with that tag, `exclude_tag` removes threads with that tag.
- Both filters can be combined.
- For full filter semantics, see [`tools`](./index.md).
- `list_thread_tags(exclude_tag="resolved", include_untagged=True)` lists unresolved room threads, including threads that have no tag state yet.
- `include_untagged=True` forces a room-wide query and cannot be combined with `thread_id`.
- It enumerates Matrix `/threads` and may stop at the 2000-root safety cap.
- The response includes `include_untagged: bool` and `truncated: bool`.
- Callers must check `truncated` before claiming the unresolved list is complete.
- The `tag_thread()` description includes up to 20 of the current room's most-used tags that are short enough for model output, from a cache-stable daily snapshot, so agents prefer existing vocabulary.
- The vocabulary is room-scoped, so tags from other rooms are never included.

## [`thread_resolution`]

`thread_resolution` gives an agent explicit permission to resolve or reopen Matrix threads in the current room.

### What It Does

`thread_resolution` exposes `resolve_thread(thread_id=None)` and `reopen_thread(thread_id=None)`.
Without `thread_id`, both functions require an active thread and target its canonical thread root.
Pass a thread root or reply event ID to target another thread in the current room, including when calling from the room timeline.
Explicit IDs are normalized to their canonical thread root, and unresolved targets return an error without changing any thread.
`resolve_thread()` adds the `resolved` lifecycle tag, while `reopen_thread()` removes it.

### Configuration

This tool has no tool-specific inline configuration fields.
It is not included in starter configs or default tool sets, so ordinary agents cannot resolve threads.
Operators may grant it directly, and agents explicitly granted `self_config` may add it to their own tool list.

### Example

```yaml
agents:
  triage:
    tools:
      - thread_tags
      - thread_resolution
```

```python
resolve_thread()
reopen_thread()
resolve_thread(thread_id="$completed-thread:example.org")
reopen_thread(thread_id="$completed-thread:example.org")
```

To clear completed project threads, use `list_thread_tags(exclude_tag="resolved", include_untagged=True)` to find candidates, then call `resolve_thread(thread_id=...)` for each selected thread.
Check the listing's `truncated` flag before treating it as complete.

### Notes

- Resolution uses the same `com.mindroom.thread.tags` state as thread-card filtering and does not restore the removed experimental resolution event type.
- The generic `thread_tags` tool can still list and filter by `resolved`, but it cannot add or remove that lifecycle state.
- Low-level Matrix state APIs remain separate broad capabilities and should only be granted to agents that need raw Matrix access.

## [`thread_summary`]

`thread_summary` lets agents set or replace the current thread summary explicitly instead of waiting for the automatic summarizer.

### What It Does

`thread_summary` exposes `set_thread_summary(summary, thread_id=None, room_id=None, pin=True)`.
The tool defaults to the active room and current resolved thread from `ToolRuntimeContext`.
When there is no active resolved thread context, pass `thread_id` explicitly.
The tool normalizes the target to the canonical thread root before sending a new `m.notice` summary event with `io.mindroom.thread_summary` metadata.
Manual summaries are marked with `model_name="manual"` and pin the thread by default, which stops automatic summaries from overwriting the title.
Pass `pin=False` to write a summary that later automatic summaries may replace; that also releases a thread pinned by an earlier call.
A per-thread async lock prevents concurrent duplicate manual summaries from racing each other.
Manual tool writes require complete projected thread history within one read, 2,000 messages or 16 MiB of stored content, whichever comes first.
If history is incomplete or unavailable, the tool returns an error before publishing the summary.
A history read can be incomplete below those limits.

Manual edits from MindRoom Chat also pin the summary immediately using a version 1 `m.notice` with `model="manual"` and `pinned=true` in `io.mindroom.thread_summary` metadata.
The sender must have responder access to the updating agent or another eligible responder in the room, so a shared thread title stays pinned across agents.
Human notices can establish a pin, but their message counts and enrichment markers are never trusted.
Pins are recovered from thread history after restart and checked again before an in-flight automatic summary is delivered.
Unavailable shared-responder authorization or a failed final history recheck defers automatic delivery; completed generation attempts keep the normal retry interval.
Ad-hoc rooms defer an unproven pin decision until member discovery is complete; an already proven grant still pins immediately.
Pin decisions follow Matrix event time, including the edit time when replacement content reasserts a pin, so a later human pin can override an older release that a cold client has not loaded.
MindRoom Chat orders displayed titles by Matrix event or edit time, with `generated_at` breaking equal-time ties.
Older clients and cached summaries without event timestamps use `generated_at` ordering.
Explicit tool writes and later automatic updates advance past authorized predecessor timestamps, including client clock skew.
If an existing timestamp cannot be advanced within the supported date range, a manual write returns an error instead of silently failing to replace the title.

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
set_thread_summary("Routine status update.", pin=False)
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
- Pin state lives in the summary notice's `io.mindroom.thread_summary` metadata, so it survives restarts and every runtime reads the same decision.
- Pinning stops the whole automatic pass, not just the title. A thread pinned before its automatic topic tags are inferred will not receive them; tag it explicitly with `thread_tags` instead.
- Automatic summaries are also skipped on threads carrying the `resolved` tag.

See [`thread_model`](../configuration/models.md#thread_model).

## [`matrix_api`]

`matrix_api` exposes a small low-level Matrix API surface for explicit room, event, and state operations, including room-scoped search.

### What It Does

`matrix_api` supports `send_event`, `get_state`, `put_state`, `redact`, `get_event`, and `search`.
It defaults `room_id` to the active room, but it also supports cross-room access when the requester has access to the agent and is currently joined to that other room.
It never infers thread IDs, event IDs, or state keys from thread context, so callers must pass those identifiers explicitly for low-level operations.
`send_event`, `put_state`, and `redact` are rate-limited per `(agent_name, requester_id, room_id)` and audited in logs.
An `m.room.message` that `send_event` sends for a human or configured bot-account requester names that requester, so the agents and teams it mentions act for that requester, as they do for the agent's ordinary replies.
Dangerous state event types like `m.room.power_levels` and `m.room.encryption` are blocked by default.
Pass `allow_dangerous=true` only when you intentionally want to change critical room state.
A dangerous write also requires the human requester, or one of their configured bridge aliases, to be joined to the target room with room admin power (power level 100), so the model's flag alone never authorizes it.
Hard-blocked state event types like `m.room.create` remain blocked.
Redacting a state event rewrites that room state, so `redact` applies the same dangerous, hard-blocked, and reserved state rules to a state event target as `put_state`.
The `com.mindroom.*` and `io.mindroom.*` namespaces are reserved for runtime metadata: `content` may not set keys in them at any depth, and `send_event` and `put_state` may not write event types in them.
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

See [`attachments`](../attachments.md#the-attachments-tool).

## Related Matrix Runtime Features

Automatic thread summaries are still implemented in `src/mindroom/thread_summary.py` as bot runtime behavior.
The summarizer posts one `m.notice` summary after a successful response brings a thread to `defaults.thread_summary_first_threshold` messages (default `1`), and then again every `defaults.thread_summary_subsequent_interval` additional messages (default `10`), using `defaults.thread_summary_model` or `default`.
Set `room_thread_summary_models` to override the automatic summary model for a managed room alias or raw Matrix room ID.
MindRoom uses `defaults.thread_summary_temperature` for automatic summaries when the provider supports runtime temperature overrides.
MindRoom always uses provider temperature defaults for GPT-6 Astra, Sol, and Luna, Vertex Claude, Claude Opus 5.5, Sonnet 5.5, Opus 5, Sonnet 5, Fable 5.1, and direct Google Gemini 3.8 Flash and Gemini 3.5 Flash-Lite summaries.
The `thread_summary` tool complements that automatic behavior by letting an agent publish a manual summary immediately and advance the stored summary baseline.
When no trusted prior summary exists, the first automatic summary is summary-only so a useful thread title appears early.
The next scheduled refresh uses one structured model call to update the summary and produce up to three normalized topic tags, whether the prior summary was automatic or manual.
The background task reads fresh authoritative history for counting and includes the delivered response.
The model prompt excludes trusted summary notices and messages with empty bodies.
If more than 50 messages remain, it includes only the first three and last three plus an omission notice; at 50 or fewer, it includes all remaining messages.
Automatic refreshes require complete projected thread history within one read, 2,000 messages or 16 MiB of stored content, whichever comes first, just like manual tool writes.
Incomplete or unavailable history skips an automatic refresh, and incompleteness can occur below those limits.
The read's message limit counts projected messages, while prompt sampling counts body-bearing non-summary messages; neither is a raw Matrix event count.
Existing tags win, including tags observed after the model call finishes.
MindRoom serializes automatic and tool-driven tag mutations per thread within one running process, and persisted removal tombstones prevent a later automatic batch from repopulating a deliberately untagged thread.
The initial tags use the same summary model, room override, temperature, prompt, lock, and background lifecycle as the refreshed summary.
Expected Matrix write failures are isolated so a failed tag write does not block the summary and a failed summary write does not undo tags.
Failed initial tag reads or all-failed tag writes leave the summary's durable enrichment marker incomplete, so the next summary threshold retries structured enrichment.
Once existing tags, prior tag-state history, or newly written tags mark initial enrichment complete, later summary refreshes use a summary-only schema and never regenerate or replace tags.
Each room has its own vocabulary snapshot built only from that room's tag state.
The first successful post-response check after midnight in the configured timezone refreshes a stale room snapshot for the day.
The refresh reads Matrix tag state and writes a durable local snapshot under `mindroom_data/tracking/thread_tag_vocabulary/`.
Initial enrichment uses the summary call that would already run and can add up to three Matrix state writes.

## Related Docs

- [Tools Overview](index.md)
- [Attachments](../attachments.md)
- [Per-Agent Tool Configuration](index.md#per-agent-tool-configuration)
