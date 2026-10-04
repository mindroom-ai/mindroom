---
icon: lucide/wrench
---

# Matrix & Threads

These built-in tools let an agent work inside its current Matrix room and thread: inspect rooms, send voice notes, tag, resolve, and summarize threads, make low-level Matrix API calls, and reuse conversation attachments.
This page also covers automatic thread summaries.

## Tools On This Page

- [`matrix_message`](matrix-message.md#matrix-messages) - Send, read, edit, and react in Matrix conversations.
- [`matrix_room`] - Inspect room metadata, available agents, members, thread roots, and room state.
- [`matrix_voice_message`] - Generate speech from text and send it as a Matrix voice note.
- [`thread_tags`] - Add, remove, and list shared tags on Matrix threads.
- [`thread_resolution`] - Resolve or reopen threads in the current room.
- [`thread_summary`] - Set or replace a thread's summary.
- [`thread_model`](../configuration/models.md#thread_model) - List models or show, switch, and reset the current thread's model override.
- [`matrix_api`] - Low-level Matrix event, state, redaction, and search calls with explicit IDs.
- [`attachments`](../attachments.md#the-attachments-tool) - List, inspect, and register attachment IDs for later tool calls.

None of these tools has tool-specific configuration fields except [`matrix_voice_message`].
Enable them by listing them in an agent's `tools`, as described in [Per-Agent Tool Configuration](index.md#per-agent-tool-configuration).
Enabling `matrix_message` also enables `attachments` and `matrix_room`.

## Tool Runtime Context

These tools work only while an agent is responding in a Matrix conversation.
Unless the call passes explicit IDs, they act on the current room and thread.
`thread_tags`, `thread_resolution`, and `thread_summary` accept a reply event ID as `thread_id` and act on its thread root; other tools need the thread root ID itself.
Tools that accept `room_id` can target another room only when the requester has access to the agent and is currently joined to that room.
`matrix_api` is the exception: it defaults `room_id` to the current room but never infers event IDs, thread IDs, or state keys from the conversation.
Attachment IDs (`att_*`) are limited to the current conversation and IDs registered earlier in the same tool run; see [Attachments](../attachments.md#attachment-ids).

## [`matrix_room`]

`matrix_room(action="room-info", room_id=None, limit=None, event_type=None, state_key=None, page_token=None)` reads room data and never writes.
Use `matrix_message` or `matrix_api` for writes.

| Action | Returns |
|--------|---------|
| `room-info` | Room name, topic, encryption, member count, join rule, canonical alias, room version, guest access, creator, power-level summary, and the current `thread_id`, `reply_to_event_id`, `requester_id`, and `agent_name` (the thread is omitted when inspecting another room) |
| `members` | Joined users with display names, avatar URLs, and power levels |
| `agents` | Agents that can currently answer this requester in the room, each with `name`, `matrix_user_id`, `description`, and `thread_mode` (`thread` or `room`) |
| `threads` | Thread-root previews with sender, timestamp, reply count, and latest activity; `limit` defaults to 20 (range 1-50), and `next_token` plus `has_more` support paging through `page_token` |
| `state` | With `event_type`, one state event (`state_key` defaults to empty); without it, a summary of up to 100 non-member state events |

Pass an `agents` result's `name` as `recipient` in [`matrix_message`](matrix-message.md#agent-conversations) to address that agent.
The list covers conversation targets only; the `run_subagent` tool describes the caller's allowed subagents.
Each agent, requester, and room combination may make 20 calls per 30 seconds.

```python
matrix_room(action="agents")
matrix_room(action="threads", limit=10)
matrix_room(action="threads", page_token="next-page-token")
matrix_room(action="state", event_type="m.room.topic")
```

## [`matrix_voice_message`]

`matrix_voice_message(text, room_id=None, thread_id=None, caption=None, companion_message=None)` converts text to speech and sends it as one Matrix voice note.
It targets the current room and thread by default; pass `thread_id="room"` to post at room level.
`caption` sets the audio event's body, and `companion_message` sends a separate readable text message to the same place.
The result includes `event_id` and, when companion text was sent, `companion_event_id`.
Each agent, requester, and room combination may send six voice messages per 30 seconds.

| Field | Default | Meaning |
|-------|---------|---------|
| `api_key` | none | Speech API key; when empty, OpenAI models use `OPENAI_API_KEY` (or `OPENAI_API_KEY_FILE`, or a stored credential) and OpenRouter models use `OPENROUTER_API_KEY` |
| `model` | `gpt-4o-mini-tts` | Plain IDs use OpenAI; provider-prefixed IDs such as `hexgrad/kokoro-82m` route through OpenRouter |
| `base_url` | none | Any OpenAI-compatible speech endpoint, such as a local Kokoro server; also settable through the `TTS_URL` secret |
| `voice` | `alloy` | Voice name passed to the endpoint |
| `response_format` | `opus` | Format the endpoint returns: `aac`, `flac`, `mp3`, `opus`, or `wav`; OpenRouter always uses `mp3` |

`base_url` accepts a bare host, a `/v1` URL, or a full `/audio/speech` URL, which is used as given.
A custom non-OpenRouter `base_url` without `api_key` never receives your OpenAI key.
Formats other than `opus` require `ffmpeg` and `ffprobe` on the backend host; `opus` works without them but the voice note then has no duration.

```yaml
agents:
  assistant:
    tools:
      - matrix_voice_message:
          base_url: http://localhost:8880/v1
```

```python
matrix_voice_message(
    "The build finished successfully.",
    companion_message="The build finished successfully.",
)
```

## [`thread_tags`]

`thread_tags` adds, removes, and lists tags that every member sees on a thread, such as on MindRoom Chat thread cards.

- `tag_thread(tag, thread_id=None, room_id=None, note=None, data=None)` adds or updates one tag and returns the thread's updated tags.
- `untag_thread(tag, thread_id=None, room_id=None)` removes one tag and returns the updated tags.
- `list_thread_tags(thread_id=None, room_id=None, tag=None, include_tag=None, exclude_tag=None, include_untagged=False)` lists tags for one thread or the whole room.

Without `thread_id`, `tag_thread()` and `untag_thread()` use the current thread; outside a thread, pass `thread_id`.
Without `thread_id`, `list_thread_tags()` lists the current thread, or the whole room when called outside a thread or with `room_id`.

Tag names that are already valid IDs of up to 50 characters are kept; other input is lowercased and hyphenated into a tag of at most 25 characters.
The `tag_thread()` description shows the agent up to 20 of the room's most-used tags, refreshed daily, so it reuses existing vocabulary.
Some tags validate their `data`: `blocked.data.blocked_by` must be a list of strings, `waiting.data.waiting_on` a non-empty string, `priority.data.level` one of `high`, `medium`, or `low`, and `due.data.deadline` an ISO-8601 timestamp.

`resolved` is thread lifecycle state, so `tag_thread()` and `untag_thread()` reject it; use [`thread_resolution`] instead.
Listing and filtering by `resolved` still works.

Filters for `list_thread_tags()`:

- `tag` keeps only that tag in the output.
- `include_tag` returns only threads that have the tag, and `exclude_tag` returns only threads without it; they can be combined.
- `include_untagged=True` lists the whole room, including threads that have no tags, and cannot be combined with `thread_id`.
  It reads at most 2,000 thread roots, so check the `truncated` field before treating the list as complete.

```python
tag_thread("blocked", data={"blocked_by": ["$otherThread"]})
untag_thread("blocked")
list_thread_tags(exclude_tag="resolved", include_untagged=True)  # unresolved threads
```

Writing a tag requires both the agent's Matrix account and the requesting user to have enough power level to send room state in the target room, and the requesting user must be joined to it.

## [`thread_resolution`]

`thread_resolution` lets an agent resolve or reopen threads in the current room.
It is not in starter configs or default tool sets, so agents cannot resolve threads unless an operator grants it or an agent with [`self_config`](agent-orchestration.md#self_config) adds it to its own tools.

`resolve_thread(thread_id=None)` adds the `resolved` state, and `reopen_thread(thread_id=None)` removes it.
Without `thread_id`, both require an active thread.
Pass a thread root or reply event ID to target another thread in the current room, including from the room timeline; an ID that does not resolve to a thread returns an error and changes nothing.

```yaml
agents:
  triage:
    tools:
      - thread_tags
      - thread_resolution
```

To close out finished threads, find candidates with `list_thread_tags(exclude_tag="resolved", include_untagged=True)`, check `truncated`, then call `resolve_thread(thread_id=...)` for each one.

## [`thread_summary`]

`set_thread_summary(summary, thread_id=None, room_id=None, pin=True)` posts a new summary notice in the thread, which MindRoom Chat shows as the thread title.
`summary` must be plain text of 1-300 characters after whitespace normalization.
Without `thread_id`, it uses the current thread; outside a thread, pass `thread_id`.

By default a manual summary pins the thread, so [automatic summaries](#automatic-thread-summaries) stop updating it.
Pass `pin=False` for a summary that later automatic summaries may replace; this also releases an earlier pin.
Editing a summary in MindRoom Chat also pins it when the editor can address an agent in that room.
A pinned thread receives no automatic summaries or automatic topic tags; add tags with [`thread_tags`] instead.

The tool returns an error without posting when it cannot read the thread's complete history, which always happens for threads longer than 2,000 messages or 16 MiB of content.

```python
set_thread_summary("Decision: ship the current plan and revisit logs tomorrow.")
set_thread_summary("Routine status update.", pin=False)
set_thread_summary("Summary for the import thread.", thread_id="$threadRoot", room_id="!ops:example.org")
```

## [`matrix_api`]

`matrix_api` gives exact control over Matrix events and room state when `matrix_message` is not enough.
Grant it only to agents that need raw Matrix access.
Every action returns a JSON payload, including errors.

| Action | Required arguments |
|--------|--------------------|
| `send_event` | `event_type`, `content` |
| `get_state` | `event_type`, optional `state_key` |
| `put_state` | `event_type`, `content`, optional `state_key` |
| `redact` | `event_id`, optional `reason` |
| `get_event` | `event_id` |
| `search` | `search_term`, optional `keys`, `order_by`, `limit`, `next_batch`, `filter`, `event_context` |

`room_id` defaults to the current room; see [Tool Runtime Context](#tool-runtime-context) for other rooms.
`dry_run=true` validates `send_event`, `put_state`, and `redact` without writing.
Writes are logged and limited to 8 units per 60 seconds for each agent, requester, and room, where `send_event` costs 1 and `put_state` and `redact` cost 2.
A message sent with `send_event` for a human requester triggers the agents it mentions as that requester, like an ordinary agent reply.

Write restrictions:

- `m.room.create` is always blocked.
- `m.room.power_levels`, `m.room.encryption`, `m.room.server_acl`, `m.room.join_rules`, `m.room.history_visibility`, `m.room.guest_access`, `m.room.member`, `m.room.canonical_alias`, `m.room.tombstone`, and `m.room.third_party_invite` are dangerous: `put_state` needs `allow_dangerous=true`, and the requesting user (or one of their bridge aliases) must be joined to the room with power level 100.
- Redacting a state event follows the same rules as writing that state type.
- The `com.mindroom.*` and `io.mindroom.*` namespaces are reserved: no event type in them can be written, and `content` cannot contain keys in them at any depth.

`search` is read-only full-text search in one room.
It uses the top-level `limit` (1-50, default 10), rejects `filter.limit`, and `keys` may include `content.body`, `content.name`, and `content.topic`.
Set `event_context={"include_profile": true}` to include sender profiles in the surrounding context.

```python
matrix_api(action="get_event", event_id="$event123")
matrix_api(action="put_state", event_type="com.example.marker", state_key="status", content={"value": "ready"})
matrix_api(action="redact", event_id="$event123", reason="Cleanup")
matrix_api(
    action="search",
    search_term="deployment incident",
    keys=["content.body"],
    event_context={"before_limit": 1, "after_limit": 1, "include_profile": True},
)
```

## Related Matrix Runtime Features

### Automatic Thread Summaries

MindRoom posts a summary notice in a thread after a response brings it to `defaults.thread_summary_first_threshold` messages (default `1`), then again after every `defaults.thread_summary_subsequent_interval` further messages (default `10`).
Threads that are pinned with [`thread_summary`] or marked `resolved` are skipped, as are threads whose full history cannot be read, which always includes threads longer than 2,000 messages or 16 MiB of content.
For threads with more than 50 messages, the summary model sees only the first three and last three.

The summary uses the first match of:

1. `room_thread_summary_models`, keyed by managed room alias or Matrix room ID.
2. `room_thread_summary_models`, keyed by the responding agent or team name, which covers ad-hoc rooms without a managed alias.
3. `defaults.thread_summary_model`.
4. The `default` model.

```yaml
defaults:
  thread_summary_model: luna
room_thread_summary_models:
  dev: sonnet
```

`defaults.thread_summary_temperature` (default `0.2`, `null` for provider defaults) applies when the provider supports a temperature override.
Models that accept only their provider's default sampling always use it: Vertex AI Claude, Claude Fable, Opus, and Sonnet 5 and 5.x models, GPT-6 Astra, Sol, and Luna, and direct Gemini API Gemini 3.8 Flash, 3.6 Flash, and 3.5 Flash-Lite.

The first automatic refresh of a thread that already has a summary also adds up to three topic tags, but only when the thread has never had tags.
Later summaries never change tags, so tags someone removed are not re-added.

## Related Docs

- [Tools Overview](index.md)
- [Attachments](../attachments.md)
- [Per-Agent Tool Configuration](index.md#per-agent-tool-configuration)
