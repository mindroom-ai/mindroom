# Hooks

Hooks are async functions in a [plugin](https://docs.mindroom.chat/plugins/) that run when MindRoom events happen.
Use them to add context to prompts, rewrite or suppress replies, allow or decline tool calls, and react to messages, reactions, schedules, room membership, sessions, compaction, startup, shutdown, and config reloads.
Each hook has a timeout, and a failing hook is logged without crashing the bot.
The [Plugins](https://docs.mindroom.chat/plugins/) page covers manifests, `plugins:` entries, installation, and hot reload.

## Quick start

Create a plugin directory with a manifest and a hook:

```
plugins/location-context/
  mindroom.plugin.json
  plugin.py
```

```json
{"name": "location-context", "tools_module": "plugin.py"}
```

```python
# plugin.py
from mindroom.hooks import hook


@hook("message:enrich", priority=20)
async def enrich_with_location(ctx):
    location = await fetch_location(ctx.settings["dawarich_url"])
    if location:
        ctx.add_metadata("location", f"User is at {location}", persist=False)
```

```yaml
# config.yaml
plugins:
  - path: ./plugins/location-context
    settings:
      dawarich_url: http://dawarich.local
```

Whenever an agent is about to answer a message, this hook adds the user's location to that turn's context, and `persist=False` keeps it out of session history.

MindRoom finds `@hook` functions in the manifest's `hooks_module`, or in `tools_module` when `hooks_module` is omitted; see [Manifest format](https://docs.mindroom.chat/plugins/#manifest-format).

## Configure hooks per deployment

Plugin `settings` reach every hook in that plugin as `ctx.settings`.
The `hooks:` map disables a hook or overrides its `priority` or `timeout_ms` by hook name, without changing code:

```yaml
plugins:
  - path: ./plugins/personal-context
    settings:
      dawarich_url: http://dawarich.local
      weather_api_key: ${OPENWEATHER_API_KEY}
    hooks:
      enrich_with_weather:
        enabled: false
      enrich_with_location:
        priority: 10
        timeout_ms: 500
```

A per-hook override takes precedence over the value in the `@hook` decorator.
[Entry formats](https://docs.mindroom.chat/plugins/#entry-formats) lists the plugin entry and override fields.
If a name under `hooks:` matches no hook in that plugin, MindRoom logs a warning and ignores the override.

## The `@hook` decorator

```python
from mindroom.hooks import hook


@hook(
    "message:enrich",
    name="enrich_weather",       # Hook name (defaults to function name)
    priority=20,                 # Lower runs first (default: 100)
    timeout_ms=500,              # Override default timeout for this event
    agents=["code", "research"], # Only run for these agents
    rooms=["!room:localhost"],   # Only run in these rooms
)
async def enrich_weather(ctx):
    ...
```

| Parameter | Type | Default | Description |
| --- | --- | --- | --- |
| `event` | `str` | *required* | Event name to listen for |
| `name` | `str` | function name | Hook name used by `hooks:` overrides; must be unique within a plugin, and duplicates are skipped with a warning |
| `priority` | `int` | `100` | Execution order; lower values run first |
| `timeout_ms` | `int \| None` | per-event default | Override the event's default timeout |
| `required` | `bool` | `False` | For `agent:started` only: the bot does not start if this hook fails or times out |
| `agents` | `Iterable[str] \| None` | `None` (all) | Only fire for these agent or team names |
| `rooms` | `Iterable[str] \| None` | `None` (all) | Only fire for these room IDs |

Hook functions must be `async`.
Hooks with equal priority run in `plugins:` list order, then in source order.
A hook with `agents` set never fires for events that carry no agent: `schedule:fired`, `reaction:received`, `config:reloaded`, and custom events.
A hook with `rooms` set never fires for `config:reloaded` or for custom events emitted outside a room.

## Execution modes

Each event runs its hooks in one of four modes.

### Observer

Hooks run one after another and only observe, except for the event's mutable fields such as `suppress`.
A failing hook is skipped and the next hook still runs; side effects it already made are not undone.
Custom events and most built-in events are observers.

```python
from mindroom.hooks import hook


@hook("message:received")
async def log_inbound(ctx):
    ctx.logger.info("Message received", body=ctx.envelope.body)


@hook("message:after_response")
async def track_response(ctx):
    save_metric(ctx.result.response_event_id, ctx.result.delivery_kind)
```

### Collector

Used by `message:enrich` and `system:enrich`.
Hooks run concurrently, at most 10 at a time, so total latency is roughly that of the slowest hook rather than the sum.
Each hook contributes `EnrichmentItem` entries, and a failing or timed-out hook loses only its own items.
Items are merged in hook order.

### Transformer

Used by `message:before_response` and `message:final_response_transform`.
Hooks run one after another on a mutable `ctx.draft`, and each may replace `ctx.draft.response_text`.
Only `message:before_response` may suppress the reply.
When a `message:before_response` hook fails, changes it made to the draft before failing are kept.
When a `message:final_response_transform` hook fails, its changes are discarded and the next hook receives the previous draft.

```python
from mindroom.hooks import hook


@hook("message:before_response", priority=10)
async def add_disclaimer(ctx):
    ctx.draft.response_text += "\n\n*Generated automatically.*"


@hook("message:before_response", priority=20)
async def redact_secrets(ctx):
    ctx.draft.response_text = scrub_api_keys(ctx.draft.response_text)


@hook("message:final_response_transform", priority=10)
async def add_links(ctx):
    ctx.draft.response_text = linkify_references(ctx.draft.response_text)
```

### Gate

Used by `tool:before_call`.
Hooks run one after another, and the first hook that calls `ctx.decline(reason)` stops the chain.
The tool does not run, and the model receives the reason in the tool result.
A gate hook that fails or times out does not block the tool call.

```python
from mindroom.hooks import hook


@hook("tool:before_call", priority=10)
async def block_secret_reads(ctx):
    if ctx.tool_name == "read_file" and "secret" in str(ctx.arguments.get("path", "")):
        ctx.decline("Sensitive files must stay unread.")
```

## Built-in events

| Event | Mode | Context type | When it fires | Mutable fields | Default timeout (ms) |
| --- | --- | --- | --- | --- | --- |
| `message:received` | Observer | `MessageReceivedContext` | After authorization, deduplication, and voice transcription; before command parsing, routing, and attachment registration | `suppress` | 15000 |
| `message:enrich` | Collector | `MessageEnrichContext` | After routing picks the agent or team; before AI generation | `add_metadata()` | 2000 |
| `system:enrich` | Collector | `SystemEnrichContext` | After `message:enrich`; before AI generation | `add_instruction()` | 2000 |
| `message:before_response` | Transformer | `BeforeResponseContext` | After AI generation; before the reply first becomes visible in Matrix | `draft.response_text`, `draft.suppress` | 200 |
| `message:final_response_transform` | Transformer | `FinalResponseTransformContext` | After a streamed reply finishes successfully, before its final edit | `draft.response_text` | 200 |
| `message:after_response` | Observer | `AfterResponseContext` | After the final Matrix send or edit | None | 3000 |
| `message:cancelled` | Observer | `CancelledResponseContext` | After a response ends any way other than clean success: cancellation, interruption, suppression, or delivery failure | None | 3000 |
| `agent:started` | Observer | `AgentLifecycleContext` | After the bot logs in, sets presence, and registers callbacks | None | 5000 |
| `bot:ready` | Observer | `AgentLifecycleContext` | After the bot finishes room joins and initial sync | None | 5000 |
| `agent:stopped` | Observer | `AgentLifecycleContext` | During orderly shutdown | None | 5000 |
| `session:started` | Observer | `SessionHookContext` | Once when a response creates a new persisted session | None | 5000 |
| `compaction:before` | Observer | `CompactionHookContext` | After the messages to compact are chosen; before the compacted session is saved | None | 15000 |
| `compaction:after` | Observer | `CompactionHookContext` | After compaction is saved, with token counts and the generated summary | None | 5000 |
| `schedule:fired` | Observer | `ScheduleFiredContext` | Before a scheduled task posts its message | `message_text`, `suppress` | 1000 |
| `reaction:received` | Observer | `ReactionReceivedContext` | For reactions not consumed by a built-in handler (tool approval, stop, config confirmation, interactive question) | None | 500 |
| `room:member_joined` | Observer | `RoomMemberJoinedContext` | On the router, after a human joins a room | None | 3000 |
| `room:member_left` | Observer | `RoomMemberLeftContext` | On the router, after a human leaves a room | None | 3000 |
| `config:reloaded` | Observer | `ConfigReloadedContext` | After a new config is applied and affected entities restart or update in place | None | 5000 |
| `tool:before_call` | Gate | `ToolBeforeCallContext` | Immediately before each tool call | `decline()` | 200 |
| `tool:after_call` | Observer | `ToolAfterCallContext` | After each tool call returns, raises, or is declined | None | 300 |

Custom events default to 1000 ms.

### Event notes

- **Replies**: `message:before_response` runs only for AI-generated replies, before any reply text is visible.
  Streamed replies usually show text before they finish, so this hook does not see them; set `defaults.enable_streaming: false` when every reply must pass through it, as with the disclaimer and redaction examples above.
  Once a streamed reply is visible it cannot be suppressed; use `message:final_response_transform` for one text-only replacement after a streamed reply succeeds.
  `message:final_response_transform` cannot suppress, redact, or delete the reply, or change its metadata.
- **`message:cancelled`**: `ctx.info.failure_reason` tells cancellation, interruption, suppression, and delivery failure apart.
- **`agent:started` with `required=True`**: use a short, separate required hook for initialization the bot must not run without, and keep optional work such as backfill or welcome messages in ordinary hooks.
  A failure or timeout stops the remaining startup hooks and fails that bot's startup like any other startup error.
- **Sessions and compaction**: `ctx.scope.key` identifies the history scope, and `ctx.session_id` identifies the persisted session within it.
  In compaction hooks, `ctx.messages` holds the raw `agno.models.message.Message` objects being compacted, unsanitized, including attachments, media, tool calls and arguments, provider metadata, citations, reasoning, and metrics.
- **`schedule:fired`**: `ctx.thread_id` is the thread the message will be delivered to, which can differ from `ctx.workflow.thread_id` when the task starts a new thread or posts at room level.
  The event fires for visible and silent schedules.
  Set `ctx.suppress = True` to cancel the fire; an empty or whitespace-only `ctx.message_text` produces a visible scheduled-task failure notice instead.
  The hook can run more than once for the same occurrence, so side-effecting hooks should use `ctx.correlation_id`, which is stable per occurrence, as an idempotency key.
- **Room membership**: both events exclude configured agents, the internal `mindroom_user`, and `bot_accounts`.
  `room:member_joined` fires only for live joins, not for members already present at startup or for profile changes; `room:member_left` fires when a joined user leaves on their own, not when they are kicked or banned.
  `room:member_left` reports the `display_name` and `avatar_url` the user had while joined.
  Either event can be delivered more than once for the same join or leave after a restart, so handlers that create rooms, send invites, or write state must be idempotent.

## Message enrichment (`message:enrich`)

`message:enrich` adds context to the user's message for the current turn.
Add items with `ctx.add_metadata()`:

```python
@hook("message:enrich")
async def enrich_with_profile(ctx):
    profile = load_profile(ctx.envelope.requester_id)
    ctx.add_metadata(
        "user_profile",
        f"Name: {profile.name}, Timezone: {profile.tz}",
        cache_policy="stable",
    )
```

Hooks can also return an `EnrichmentItem` or a list of them:

```python
from mindroom.hooks import EnrichmentItem, hook


@hook("message:enrich")
async def enrich_with_time(ctx):
    return EnrichmentItem(key="time", text=f"Current time: {now()}", persist=False)
```

`EnrichmentItem` fields are `key`, `text`, `cache_policy` (`"volatile"`, the default, for values that change often such as weather or time, or `"stable"` for values that rarely change such as a profile or timezone), `persist` (default `True`), and `minimal_required` (default `False`).

Persisted items are appended to the user message and kept in session history, so later turns see them exactly as rendered:

```xml
<mindroom_message_context>
<item key="user_profile" cache_policy="stable">Prefers concise answers, timezone Europe/Amsterdam</item>
</mindroom_message_context>
```

Items with `persist=False` go into a separate context message for the current turn only.
Use `persist=False` for live data such as location or weather, so session history and provider prompt caches stay reusable.
Use stable keys and deterministic text for persisted items.

## System prompt enrichment (`system:enrich`)

`system:enrich` runs after `message:enrich` and adds instructions to the agent's system prompt for the current turn.
For teams, the same instructions go to the team and to each member agent.
Add items with `ctx.add_instruction()`, or return `EnrichmentItem` objects as with `message:enrich`:

```python
from mindroom.hooks import SystemEnrichContext, hook


@hook("system:enrich", priority=40)
async def inject_room_tags(ctx: SystemEnrichContext) -> None:
    tags = await get_room_tags(ctx.envelope.room_id)
    if tags:
        ctx.add_instruction(
            "room_tags",
            f"Existing thread tags in this room: {', '.join(sorted(tags))}",
            cache_policy="stable",
        )
```

Items are rendered in a `<mindroom_system_context>` block, with `"stable"` items first and `"volatile"` items last, each group sorted by key.
`persist` has no effect here because system enrichment always applies to the current turn only.
Changing the system prompt invalidates more of the provider's prompt cache, so use `system:enrich` only for room- or turn-scoped instructions that need system-level priority, and `message:enrich` with `persist=False` for live data.
The key `matrix_message_target` is reserved; MindRoom replaces hook items that use it.

## Enrichment in minimal mode

In [minimal mode](https://docs.mindroom.chat/tools/agent-cli/), system enrichment and non-persisted message enrichment are available to the agent through the CLI's enrichment context document instead of the prompt.
Return an `EnrichmentItem` with `minimal_required=True` when the item must stay in the prompt without a tool call; `ctx.add_metadata()` and `ctx.add_instruction()` cannot set this flag.
Persisted message enrichment and standard mode are unaffected.

```python
@hook("system:enrich")
async def required_instruction(ctx):
    return EnrichmentItem(
        key="required_policy",
        text="Use the approved project directory for all file changes.",
        minimal_required=True,
    )
```

## Custom events

Plugins can define their own namespaced events and emit them from tool code:

```python
from mindroom.hooks import hook


@hook("todo:item_completed")
async def audit_completion(ctx):
    append_jsonl(ctx.state_root / "events.jsonl", {"item_id": ctx.payload["item_id"]})
```

```python
from mindroom.tool_system.runtime_context import emit_custom_event

# Inside a tool method:
await emit_custom_event("my-plugin", "todo:item_completed", {"item_id": "123"})
```

- Event names must match `^[a-z0-9_.-]+(:[a-z0-9_.-]+)+$`, so they need at least one colon.
- The namespaces `message`, `system`, `agent`, `bot`, `compaction`, `schedule`, `reaction`, `room`, `config`, `session`, and `tool` are reserved.
- Custom events run as observers.
- Only code running during a tool call in the primary MindRoom process can emit them, which includes `tool:before_call` and `tool:after_call` hooks and custom-event hooks triggered by that call.
- Hooks running outside a tool call and tools running in workers cannot emit them.
- Emissions nested more than 3 levels deep are dropped with a warning.

## Errors and timeouts

Each hook runs under its timeout.
A timeout cannot interrupt blocking code such as a CPU-bound loop, so a hook stuck in one stalls MindRoom until it returns.
An exception or timeout is logged with the plugin and hook name and affects only that hook for that event, as described per mode under [Execution modes](#execution-modes), except for a required `agent:started` hook.
MindRoom does not retry a failed hook, so implement retries inside the hook if it needs them.
A failing hook is invoked again on the next matching event, with no quarantine or cooldown, so fixing the code under [hot reload](https://docs.mindroom.chat/plugins/#live-development-hot-reload) takes effect on the next event.

## Plugin state

`ctx.state_root` is a per-plugin directory at `<storage root>/plugins/<plugin name>/` (by default `mindroom_data/plugins/<plugin name>/`), created on first access.
Organizing it per room or per user is up to the plugin.

```python
import json

from mindroom.hooks import hook


@hook("reaction:received")
async def pin_message(ctx):
    if ctx.reaction_key != "\U0001f4cc":
        return
    pins_file = ctx.state_root / "pins.json"
    pins = json.loads(pins_file.read_text()) if pins_file.exists() else []
    pins.append({"room": ctx.room_id, "event": ctx.target_event_id})
    pins_file.write_text(json.dumps(pins))
```

## Context reference

### Base fields

All contexts except the two tool contexts include these fields:

| Field | Type | Description |
| --- | --- | --- |
| `event_name` | `str` | The event that triggered this hook |
| `plugin_name` | `str` | Name of the plugin owning this hook |
| `settings` | `dict[str, Any]` | Plugin `settings` from `config.yaml` |
| `config` | `Config` | Current MindRoom config (read-only) |
| `runtime_paths` | `RuntimePaths` | Storage paths and environment values |
| `logger` | `BoundLogger` | Plugin-scoped structured logger |
| `correlation_id` | `str` | Unique ID per inbound event |
| `runtime_started_at` | `float \| None` | Unix timestamp of the latest bot start, useful for ignoring plugin state recorded before it |
| `state_root` | `Path` | Plugin state directory |

`ToolBeforeCallContext` and `ToolAfterCallContext` have the same fields and helpers except `runtime_started_at` and `get_latest_agent_message_snapshot()`, and their `config` and `runtime_paths` can be `None`.

### Event-specific fields

| Context | Fields |
| --- | --- |
| `MessageReceivedContext` | `envelope`, `suppress` |
| `MessageEnrichContext`, `SystemEnrichContext` | `envelope`, `target_entity_name`, `target_member_names` (team members, or `None` for an agent) |
| `BeforeResponseContext`, `FinalResponseTransformContext` | `draft` |
| `AfterResponseContext` | `result` |
| `CancelledResponseContext` | `info` with `envelope`, `visible_response_event_id`, `response_kind`, `failure_reason` |
| `AgentLifecycleContext` | `entity_name`, `entity_type` (`"agent"`, `"team"`, or `"router"`), `rooms` (configured), `matrix_user_id`, `joined_room_ids`, `stop_reason` (`"restart"`, `"entity_removed"`, or `"shutdown"` on `agent:stopped`) |
| `SessionHookContext` | `agent_name`, `scope`, `session_id`, `room_id`, `thread_id` |
| `CompactionHookContext` | `agent_name`, `scope`, `session_id`, `room_id`, `thread_id`, `messages`, `token_count_before`, `token_count_after`, `compaction_summary` |
| `ScheduleFiredContext` | `task_id`, `workflow`, `room_id`, `thread_id`, `created_by`, `message_text`, `suppress` |
| `ReactionReceivedContext` | `room_id`, `event_id`, `sender_id`, `reaction_key`, `target_event_id`, `thread_id` |
| `RoomMemberJoinedContext`, `RoomMemberLeftContext` | `agent_name`, `room_id`, `event_id`, `user_id`, `sender_id`, `display_name`, `avatar_url`, `membership`, `prev_membership` |
| `ConfigReloadedContext` | `changed_entities`, `added_entities`, `removed_entities`, `plugin_changes` |
| `CustomEventContext` | `payload`, `source_plugin`, `room_id`, `thread_id`, `sender_id` |
| `ToolBeforeCallContext` | `tool_name`, `arguments`, `agent_name`, `room_id`, `thread_id`, `requester_id`, `session_id`, `declined`, `decline_reason`, `decline(reason)` |
| `ToolAfterCallContext` | `tool_name`, `arguments`, `agent_name`, `room_id`, `thread_id`, `requester_id`, `session_id`, `result`, `error`, `blocked`, `duration_ms` |

Tool hooks receive copies of `arguments`, and `tool:after_call` receives copies of `result` and `error`, so changing them does not affect the call.
`blocked` is true when the tool did not run, for example because a gate hook declined it.

### Message objects

```python
MessageEnvelope(
    source_event_id: str,
    target: MessageTarget,
    body: str,
    attachment_ids: tuple[str, ...],
    mentioned_agents: tuple[str, ...],
    agent_name: str,
    origin: TurnOrigin,
    hook_source: str | None,                 # "<plugin>:<event>" for hook-sent messages
    dispatch_policy_source_kind: str | None,
)
# Derived properties: room_id (from target), requester_id, sender_id, source_kind (from origin).
# target.source_thread_id is the raw inbound thread ID, target.resolved_thread_id the delivery thread,
# and target.session_id the conversation's persistence key.

TurnOrigin(
    transport_sender_id: str,
    requester_id: str,
    sender_entity_name: str | None,
    requester_entity_name: str | None,
    sender_kind: SenderKind,       # "user" or "managed_entity"
    requester_kind: SenderKind,
    intent: TurnIntent,            # see below
    source_kind: str,
    trust: TurnTrust,              # "external", "trusted_internal", or "trusted_user_relay"
)

ResponseDraft(
    response_text: str,
    response_kind: str,  # "ai", "team", "router", "system"
    tool_trace: list[ToolTraceEntry] | None,
    extra_content: dict[str, Any] | None,
    envelope: MessageEnvelope,
    suppress: bool = False,
)

FinalResponseDraft(
    response_text: str,
    response_kind: str,  # "ai", "team", "router", "system"
    envelope: MessageEnvelope,
)

ResponseResult(
    response_text: str,
    response_event_id: str,
    delivery_kind: str,  # "sent" or "edited"
    response_kind: str,
    envelope: MessageEnvelope,
)
```

`TurnIntent` is one of `"user_message"`, `"managed_message"`, `"router_handoff"`, `"router_notice"`, `"scheduled_fire"`, `"external_trigger"`, `"hook_message"`, `"hook_dispatch"`, or `"trusted_internal_relay"`.
`origin.may_dispatch_without_mention` is true for `hook_dispatch`, `external_trigger`, `router_handoff`, and `scheduled_fire` turns.
`origin.may_answer_interactive_prompt` is true only for human-requested `user_message`, `router_handoff`, and `trusted_internal_relay` turns.
`dispatch_policy_source_kind` is usually `None`; when it is `"active_thread_follow_up"`, `source_kind` still holds the original modality such as `"message"` or `"voice"`.
`TurnOrigin`, `TurnIntent`, `SenderKind`, `TurnTrust`, `ACTIVE_THREAD_FOLLOW_UP_SOURCE_KIND`, and `TRUSTED_INTERNAL_RELAY_SOURCE_KIND` are exported from `mindroom.hooks` for comparisons.

### Sending messages

**`await ctx.send_message(room_id, text, *, thread_id=None, extra_content=None, trigger_dispatch=False)`** sends a Matrix message and returns its event ID, or `None` when no sender is available.

- For contexts that come from a message, the original human or configured bot-account requester is carried along, so routing, permissions, and memory attribution use that requester rather than the relaying bot.
- In `schedule:fired`, omitting `thread_id` posts to `ctx.thread_id`, while an explicit `thread_id=None` posts at room level.
- A plain send triggers agents only when it would as a normal message, for example when it mentions an agent.
- `trigger_dispatch=True` sends the message as `source_kind` `"hook_dispatch"`, which agents may answer without a mention, subject to normal permissions and routing.
- A hook-sent message passes through `message:received` once, skipping the plugin that sent it if the send came from `message:received`.
  Messages sent by hooks in response to hook-sent messages are still posted, but they skip `message:received` and do not trigger commands or agent replies, which prevents feedback loops.

### Room state and recent messages

**`await ctx.query_room_state(room_id, event_type, state_key=None)`** reads Matrix room state.
With a `state_key`, it returns that event's content `dict`, or `None` when the event is missing or Matrix returns an error.
Without one, it returns a `{state_key: content}` dict for every event of that type, or `None` on a Matrix error.

**`await ctx.put_room_state(room_id, event_type, state_key, content)`** writes one room state event and returns `True` on success or `False` on a Matrix error.

Both return `None` or `False` when no Matrix client is available.
When the current bot gets a Matrix error, MindRoom retries the call as the router.
Network errors raise in the hook.

**`await ctx.get_latest_agent_message_snapshot(room_id, sender, *, thread_id=None)`** returns the latest visible message from `sender` in the given thread, with `content` and `origin_server_ts`.
`thread_id=None` reads only the unthreaded room conversation and ignores threaded replies.
It reads local conversation data without contacting the homeserver and looks only at the 50 most recent messages in that scope.
It returns `None` when the sender has no visible message in that window or no reader is available.

### Matrix admin

`ctx.matrix_admin` is a limited admin interface backed by the router account, or `None` when unavailable.
Hooks never get the raw Matrix client.

| Method | Returns |
| --- | --- |
| `get_joined_rooms()` | The account's joined room IDs, or `None` on failure |
| `retain_room(room_id)` | Nothing; synchronous, see below |
| `resolve_alias(alias)` | Room ID, or `None` if the alias does not exist |
| `create_room(name=..., alias_localpart=..., topic=..., power_user_ids=...)` | New room ID, or `None` on failure |
| `invite_user(room_id, user_id)` | `bool` success |
| `force_join_user(room_id, user_id)` | `bool` success; joins a local user through the homeserver admin API |
| `kick_user(room_id, user_id, reason=None)` | `bool` success |
| `get_room_members(room_id)` | Set of joined user IDs, or `None` when the room cannot be read |
| `get_profile_avatar(user_id)` | Avatar content URI, or `None` |
| `get_room_state_event(room_id, event_type, state_key)` | `(True, content)` when found, `(True, None)` when Matrix confirms it is missing, `(False, None)` on other errors |
| `add_room_to_space(space_room_id, room_id)` | `bool` success |
| `put_room_state(room_id, event_type, state_key, content)` | `bool` success |

Network errors raise in the hook.
Rooms created with `create_room` stay with the creating bot across room cleanup and restarts.
When reconciling a room the plugin created earlier, verify the bot is still a member and call `retain_room(room_id)` to keep it the same way; it changes no Matrix membership and raises `OSError` if it cannot save.
Retention applies only to bots whose [`accept_invites`](https://docs.mindroom.chat/configuration/agents/) policy is enabled.

## Testing

Hook functions are plain async functions, so a test can await one with a stub context:

```python
from types import SimpleNamespace

import pytest

from mindroom.hooks import hook


@hook("message:received")
async def suppress_spam(ctx):
    if "spam" in ctx.envelope.body:
        ctx.suppress = True


@pytest.mark.asyncio
async def test_suppress_spam():
    ctx = SimpleNamespace(envelope=SimpleNamespace(body="buy spam now"), suppress=False)
    await suppress_spam(ctx)
    assert ctx.suppress is True
```

## Limits

- Hooks cannot replace core routing, authorization, or deduplication.
- Hooks never receive the Matrix client directly.
