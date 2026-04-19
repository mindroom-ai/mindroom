---
icon: lucide/webhook
---

# Hooks

Hooks let plugins observe, enrich, and transform messages as they flow through MindRoom.
A single `@hook("event")` decorator turns any async function into a typed event handler that runs with per-hook timeouts, circuit-breaker fault isolation, and zero risk of crashing the bot.
Hooks integrate with the existing [plugin system](plugins.md) and are configured through `config.yaml`.

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
        ctx.add_metadata("location", f"User is at {location}")
```

```yaml
# config.yaml
plugins:
  - path: ./plugins/location-context
    settings:
      dawarich_url: http://dawarich.local
```

When any agent receives a message, this hook runs concurrently with other enrichment hooks and injects the user's location into the AI prompt.
The enrichment is stripped from session history after the response completes.

## Hook types

The hook system has four execution modes, determined by the event, not by individual hooks.

### Observer (`emit`)

Hooks run serially.
Each hook sees the context as read-only (except designated mutable fields like `suppress`).
Failures lose only that hook's side effects; the next hook still runs.

```python
from mindroom.hooks import hook


@hook("message:received")
async def log_inbound(ctx):
    ctx.logger.info("Message received", body=ctx.envelope.body)


@hook("message:after_response")
async def track_response(ctx):
    save_metric(ctx.result.response_event_id, ctx.result.delivery_kind)
```

### Collector (`emit_collect`)

Hooks run concurrently with isolated per-hook state.
Each hook contributes structured `EnrichmentItem` entries.
A failing hook loses only its items; other hooks' items are preserved.
Results merge in hook-order after all hooks complete.

```python
from mindroom.hooks import hook


@hook("message:enrich", priority=10)
async def enrich_with_weather(ctx):
    weather = await fetch_weather(ctx.settings["api_key"])
    if weather:
        ctx.add_metadata("weather", f"Current weather: {weather}")


@hook("message:enrich", priority=20)
async def enrich_with_calendar(ctx):
    events = await fetch_calendar(ctx.settings["calendar_url"])
    if events:
        ctx.add_metadata("calendar", f"Upcoming: {events}")
```

### Transformer (`emit_transform`)

Hooks run serially.
Each hook receives a mutable `ResponseDraft` and may modify or replace it.
Failures skip that hook's changes; the previous draft continues to the next hook.

```python
from mindroom.hooks import hook


@hook("message:before_response", priority=10)
async def add_disclaimer(ctx):
    ctx.draft.response_text += "\n\n*Generated automatically.*"


@hook("message:before_response", priority=20)
async def redact_secrets(ctx):
    ctx.draft.response_text = scrub_api_keys(ctx.draft.response_text)
```

### Gate (`emit_gate`)

Hooks run serially.
Each hook receives a mutable `ToolBeforeCallContext`.
Failures fail open, so a broken or timed-out gate hook does not block the real tool call.
The first hook that calls `ctx.decline(reason)` stops the chain and replaces the real tool call with a declined result.

```python
from mindroom.hooks import hook


@hook("tool:before_call", priority=10)
async def block_secret_reads(ctx):
    if ctx.tool_name == "read_file" and "secret" in str(ctx.arguments.get("path", "")):
        ctx.decline("Sensitive files must stay unread.")
```

## Built-in events

| Event | Mode | Context type | When it fires | Key mutable fields |
| --- | --- | --- | --- | --- |
| `message:received` | Observer | `MessageReceivedContext` | After authorization, dedup, and voice normalization; before command parsing, routing, and image/file/video attachment registration | `suppress` |
| `message:enrich` | Collector | `MessageEnrichContext` | After routing resolves target agent/team; before AI generation | `add_metadata()` |
| `system:enrich` | Collector | `SystemEnrichContext` | After message enrichment; before AI generation | `add_instruction()` |
| `message:before_response` | Transformer | `BeforeResponseContext` | After AI generation; before Matrix send (streaming: after stream completes, before final edit) | `draft.response_text`, `draft.suppress` |
| `message:after_response` | Observer | `AfterResponseContext` | After final Matrix send or edit | None (frozen) |
| `message:cancelled` | Observer | `CancelledResponseContext` | After a response is cancelled before final delivery completes | None (frozen) |
| `agent:started` | Observer | `AgentLifecycleContext` | After bot starts (Matrix login, presence, callbacks registered) | None (frozen) |
| `agent:stopped` | Observer | `AgentLifecycleContext` | During orderly shutdown | None (frozen) |
| `bot:ready` | Observer | `AgentLifecycleContext` | After bot completes room joins and initial sync | None (frozen) |
| `session:started` | Observer | `SessionHookContext` | Once per persisted session, after the response path confirms a new backing session was created for that history scope, during response finalization and before later cleanup such as persisted response-event IDs or transient-enrichment stripping | None (frozen) |
| `compaction:before` | Observer | `CompactionHookContext` | After the compacted message set is prepared and before the compacted session is persisted | None (frozen) |
| `compaction:after` | Observer | `CompactionHookContext` | After compaction is persisted, with before/after token counts and the generated summary | None (frozen) |
| `schedule:fired` | Observer | `ScheduleFiredContext` | Before scheduled task posts its synthetic message | `message_text`, `suppress` |
| `reaction:received` | Observer | `ReactionReceivedContext` | After built-in reaction handlers (stop, config, interactive) | None (frozen) |
| `config:reloaded` | Observer | `ConfigReloadedContext` | After orchestrator applies new config and restarts affected entities | None (frozen) |
| `tool:before_call` | Gate | `ToolBeforeCallContext` | Immediately before each tool call runs | `decline()` |
| `tool:after_call` | Observer | `ToolAfterCallContext` | After each tool call returns, raises, or is declined | None (observer result snapshot) |

For `compaction:before` and `compaction:after`, `ctx.messages` contains raw `agno.models.message.Message` objects from the compacted session payload.
MindRoom does not sanitize attachments, media, tool calls, tool args, provider metadata, citations, reasoning fields, metrics, references, or extra Pydantic fields before these hooks run.
For `message:cancelled`, inspect `ctx.info.failure_reason` to distinguish explicit cancellation from a streaming or delivery failure.

### Default timeouts

| Event | Default timeout (ms) |
| --- | --- |
| `message:received` | 15000 |
| `message:enrich` | 2000 |
| `system:enrich` | 2000 |
| `message:before_response` | 200 |
| `message:after_response` | 3000 |
| `message:cancelled` | 3000 |
| `reaction:received` | 500 |
| `schedule:fired` | 1000 |
| `agent:started` | 5000 |
| `agent:stopped` | 5000 |
| `bot:ready` | 5000 |
| `session:started` | 5000 |
| `compaction:before` | 15000 |
| `compaction:after` | 5000 |
| `config:reloaded` | 5000 |
| `tool:before_call` | 200 |
| `tool:after_call` | 300 |
| Custom events | 1000 |

For `session:started`, `compaction:before`, and `compaction:after`, `ctx.scope.key` identifies the persisted history scope rather than one unique session row.
Use `ctx.session_id` as the unique persisted session identifier within that scope.

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
| `name` | `str` | function name | Hook identifier (unique within a plugin) |
| `priority` | `int` | `100` | Execution order; lower values run first |
| `timeout_ms` | `int \| None` | per-event default | Override the event's default timeout |
| `agents` | `Iterable[str] \| None` | `None` (all) | Only fire for these agent names |
| `rooms` | `Iterable[str] \| None` | `None` (all) | Only fire for these room IDs |

The decorator is annotation-only.
It stores metadata on the function and has no side effects on import.
Hook callbacks must be `async`.

## Plugin manifest

Add `hooks_module` to `mindroom.plugin.json` to point to a dedicated hooks file:

```json
{
  "name": "my-plugin",
  "tools_module": "tools.py",
  "hooks_module": "hooks.py",
  "skills": ["skills"]
}
```

If `hooks_module` is omitted, MindRoom auto-scans `tools_module` for `@hook`-decorated functions.
If both fields point at the same file, MindRoom imports it once and reuses it for tool registration and hook discovery.

## Config

### String form (unchanged)

```yaml
plugins:
  - ./plugins/my-plugin
```

### Object form (settings and hook overrides)

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

Both forms can be mixed in the same `plugins` list.
Environment variable substitution works through MindRoom's existing config loading.

### Hook override fields

| Field | Type | Default | Description |
| --- | --- | --- | --- |
| `enabled` | `bool` | `true` | Disable a hook without removing code |
| `priority` | `int \| null` | `null` (use decorator value) | Override the decorator priority |
| `timeout_ms` | `int \| null` | `null` (use decorator value) | Override the decorator timeout |

### Override precedence

1. Decorator defaults in code
2. Plugin-level `settings` (available to all hooks as `ctx.settings`)
3. Per-hook overrides: `enabled`, `priority`, `timeout_ms`

If a hook name appears in `hooks:` but the plugin has no hook with that name, MindRoom logs a startup warning and ignores the override.

## Enrichment pipeline

The `message:enrich` event powers a full enrichment pipeline that injects live context into AI prompts while preserving exact replayed history for prompt caching.

### How it works

1. **Collect**: After routing decides the target agent, MindRoom runs `emit_collect("message:enrich")` which executes all matching enrichment hooks concurrently.
2. **Render**: Collected `EnrichmentItem` entries are rendered into an XML block appended to the user turn:

    ```xml
    <mindroom_message_context>
    <item key="location" cache_policy="stable">User is at Home (San Francisco)</item>
    <item key="weather" cache_policy="volatile">Current weather: 18C, partly cloudy</item>
    </mindroom_message_context>
    ```

3. **AI sees it**: The model receives the enrichment block as part of the current user message, so it has live context for its response.
4. **Preserve replay fidelity**: MindRoom keeps the exact enriched user turn everywhere it is replayed or summarized, so later requests can reuse the same prefix byte-for-byte for prompt caching.

### Enrichment policy

Each enrichment item has a `cache_policy`:

- `"volatile"` (default): The item may change on every message (e.g., weather, time).
- `"stable"`: The item changes rarely (e.g., user profile, timezone).

MindRoom still computes a stable digest for the merged enrichment set.
That digest is used internally for enrichment bookkeeping and cache-aware prompt shaping.

### Adding enrichment items

Use `ctx.add_metadata()` in any `message:enrich` hook:

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

Hooks can also return `EnrichmentItem` objects directly:

```python
from mindroom.hooks import EnrichmentItem, hook


@hook("message:enrich")
async def enrich_with_time(ctx):
    return EnrichmentItem(key="time", text=f"Current time: {now()}")
```

### Performance

Enrichment hooks run concurrently with per-hook timeouts.
A slow weather API does not block a fast calendar lookup.
Total enrichment latency equals max(individual hook latencies), not the sum.
A bounded semaphore (default 10) prevents one plugin from flooding the event loop.

## System enrichment pipeline

The `system:enrich` event powers a parallel enrichment pipeline for the system prompt.
Use it when room-scoped or turn-scoped instructions should live in `agent.additional_context` instead of the current user message.

### How it works

1. **Collect**: After `message:enrich` finishes, MindRoom runs `emit_collect("system:enrich")` with a `SystemEnrichContext`, which executes all matching system-enrichment hooks concurrently.
2. **Render**: Collected `EnrichmentItem` entries are rendered into an XML block for the system prompt:

    ```xml
    <mindroom_system_context>
    <item key="room_tags" cache_policy="stable">Existing thread tags in this room: backend, urgent</item>
    <item key="active_focus" cache_policy="volatile">Current focus: triage the incident thread before suggesting new work.</item>
    </mindroom_system_context>
    ```

3. **Apply**: For agent runs, MindRoom renders the block into `agent.additional_context` before AI generation.
4. **Apply to teams**: For team runs, MindRoom assigns the same rendered block to both `team.additional_context` and each member agent's `additional_context`.

### Adding system enrichment items

Use `ctx.add_instruction()` in any `system:enrich` hook:

```python
from mindroom.hooks import SystemEnrichContext, hook


@hook("system:enrich", priority=40)
async def inject_room_tags(ctx: SystemEnrichContext) -> None:
    """Inject existing room thread tags into system prompt."""
    tags = await get_room_tags(ctx.envelope.room_id)
    if tags:
        tag_list = ", ".join(sorted(tags))
        ctx.add_instruction(
            "room_tags",
            f"Existing thread tags in this room: {tag_list}",
            cache_policy="stable",
        )
```

Hooks can also return `EnrichmentItem` objects directly, the same way `message:enrich` hooks can.

### System cache policy

Each item still carries a `cache_policy`, but system enrichment uses it to control deterministic ordering for prompt caching:

- `"stable"`: Sorted first by key so long-lived instructions stay grouped at the front of the block.
- `"volatile"` (default): Sorted last by key so frequently changing instructions stay grouped at the end of the block.

### Key differences from `message:enrich`

- `system:enrich` injects into the system prompt via `agent.additional_context`, while `message:enrich` injects into the current user turn.
- `system:enrich` renders `<mindroom_system_context>` blocks, while `message:enrich` renders `<mindroom_message_context>` blocks.
- `system:enrich` uses `ctx.add_instruction()`, while `message:enrich` uses `ctx.add_metadata()`.
- `system:enrich` is intended for room- or turn-scoped instructions, while `message:enrich` is intended for user-prompt conversational context.

## Custom events

Plugins can define and emit namespaced custom events.
Built-in namespaces (`message:*`, `system:*`, `agent:*`, `bot:*`, `schedule:*`, `reaction:*`, `config:*`, `tool:*`) are reserved.

### Defining a custom event hook

```python
from mindroom.hooks import hook


@hook("todo:item_completed")
async def audit_completion(ctx):
    append_jsonl(ctx.state_root / "events.jsonl", {"item_id": ctx.payload["item_id"]})
```

### Emitting from tool code

Tools emit custom events through the runtime context:

```python
from mindroom.tool_system.runtime_context import emit_custom_event

# Inside a tool method:
await emit_custom_event("my-plugin", "todo:item_completed", {"item_id": "123"})
```

Hook contexts do not expose a `hook_registry`, so hook callbacks cannot emit custom events directly through `ctx`.
If you are writing internal code or tests and already have an explicit `HookRegistry`, you can still call `emit(registry, event_name, context)` manually.

### Event name rules

- Pattern: `^[a-z0-9_.-]+(:[a-z0-9_.-]+)+$`
- Must contain at least one colon separator
- Reserved namespaces: `message`, `system`, `agent`, `bot`, `schedule`, `reaction`, `config`, `tool`
- Custom events run in observer mode (`emit()`)
- Recursion guard: nested emissions stop at depth 3

## Error handling

### Fault isolation

Every hook invocation runs inside an `asyncio.timeout()` with structured error logging.
No hook can crash the bot.

Failure semantics are mode-aware:

- **Observer** failures lose only side effects; the next hook still runs
- **Collector** failures lose only that hook's contributed items
- **Transformer** failures lose only that hook's draft changes; the previous draft continues

### No quarantine, no cooldown

A hook that raises is logged and skipped for that one event. The next event invokes it again. If it keeps raising, you keep getting logs — fix it (combined with [plugin hot reload](plugins.md#live-development-hot-reload), the next save is live within ~1s) and the next invocation just works. There is no failure threshold, no muting, no cooldown to wait out.

### No automatic retries

The hook runtime does not retry failed hooks.
If a hook needs retry logic, implement it inside the hook where the author understands idempotency.

## Plugin state

Every hook has access to persistent storage via `ctx.state_root`, which maps to `mindroom_data/plugins/<plugin_name>/`.
The directory is created on first access.

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

Scoped sub-paths (per-room, per-user) are the plugin author's responsibility.

## Context reference

### Base fields (all hooks)

Every hook context includes these fields:

| Field | Type | Description |
| --- | --- | --- |
| `event_name` | `str` | The event that triggered this hook |
| `plugin_name` | `str` | Name of the plugin owning this hook |
| `settings` | `dict[str, Any]` | Plugin settings from `config.yaml` |
| `config` | `Config` | Current MindRoom config (read-only) |
| `runtime_paths` | `RuntimePaths` | Storage paths and environment values |
| `logger` | `BoundLogger` | Plugin-scoped structured logger |
| `correlation_id` | `str` | Unique ID per inbound event |
| `state_root` | `Path` | Plugin state directory (property) |

Every hook context also exposes the following helpers:

**`await ctx.send_message(room_id, text, *, thread_id=None, extra_content=None, trigger_dispatch=False)`**
Sends a hook-originated Matrix message and returns the event ID on success, or `None` when no sender is bound.
For message-derived contexts, MindRoom automatically preserves the original requester in `com.mindroom.original_sender` so downstream routing, permissions, and memory attribution continue to use the human sender instead of the router relay.
For `ScheduleFiredContext`, omitting `thread_id` inherits `ctx.thread_id`, while passing `thread_id=None` explicitly posts at room level.
Plain `hook` sends can still dispatch when they satisfy the usual routing rules, for example if the message explicitly mentions an agent or otherwise qualifies as a normal addressed message.
Hook-originated sends always carry an internal synthetic-chain depth.
The first hook-originated hop uses depth `1`, and each later synthetic hop increments it.
When `trigger_dispatch=True`, MindRoom sends the message as source kind `hook_dispatch`.
The first synthetic hook hop still re-enters the normal ingress pipeline, including `message:received`.
For `hook_dispatch`, that first synthetic hop also bypasses the usual "ignore other agent unless mentioned" ingress gate before continuing through normal permissions, routing, and should-respond checks.
If that first synthetic hop originated from `message:received`, MindRoom skips the origin plugin on the `message:received` re-entry.
Deeper synthetic hook hops still arrive as messages, but they do not re-enter `message:received` and they stop before further command handling or agent/model dispatch to avoid feedback loops.

**`await ctx.query_room_state(room_id, event_type, state_key=None)`**
Queries Matrix room state events.
When `state_key` is provided, returns the content `dict` for that single state event, or `None` on Matrix error response/not-found.
When `state_key` is `None`, returns a `{state_key: content}` dict of all state events matching `event_type`, or `None` on Matrix error response.
Returns `None` when no room state querier is available (e.g. no Matrix client bound).
When both the current bot and the router can query room state, MindRoom tries the current bot first and falls back to the router on Matrix error responses.
Transport exceptions from the underlying Matrix client propagate to the hook.

**`await ctx.put_room_state(room_id, event_type, state_key, content)`**
Writes a single Matrix room state event and returns `True` on success, `False` on Matrix error response.
Returns `False` when no room state putter is available.
When both the current bot and the router can write room state, MindRoom tries the current bot first and falls back to the router on Matrix error responses.
Transport exceptions from the underlying Matrix client propagate to the hook.

**`ctx.matrix_admin`**
Provides a narrow Matrix admin facade when MindRoom has a router-backed admin client available for the current hook context.
This facade is part of the supported hook contract and is intentionally not the raw Matrix client.
It is `None` when no admin-capable client is bound.
The available methods are `resolve_alias(alias)`, `create_room(name=..., alias_localpart=..., topic=..., power_user_ids=...)`, `invite_user(room_id, user_id)`, `get_room_members(room_id)`, and `add_room_to_space(space_room_id, room_id)`.

### Transport objects

```python
MessageEnvelope(
    source_event_id: str,
    room_id: str,
    target: MessageTarget,
    requester_id: str,
    sender_id: str,
    body: str,
    attachment_ids: tuple[str, ...],
    mentioned_agents: tuple[str, ...],
    agent_name: str,
    source_kind: str,  # "message", "edit", "voice", "image", "scheduled", "hook", "hook_dispatch"
    hook_source: str | None = None,
    message_received_depth: int = 0,  # internal synthetic-chain depth for hook-originated relays
)

# target.thread_id preserves the raw inbound thread ID.
# target.resolved_thread_id is the delivery thread after safe-root and room-mode resolution.
# target.session_id is the canonical persistence key for the conversation.

ResponseDraft(
    response_text: str,
    response_kind: str,  # "ai", "team", "router", "system"
    tool_trace: list[ToolTraceEntry] | None,
    extra_content: dict[str, Any] | None,
    envelope: MessageEnvelope,
    suppress: bool = False,
)

ResponseResult(
    response_text: str,
    response_event_id: str,
    delivery_kind: str,  # "sent" or "edited"
    response_kind: str,
    envelope: MessageEnvelope,
)

ToolBeforeCallContext(
    tool_name: str,
    arguments: dict[str, Any],
    agent_name: str,
    room_id: str | None,
    thread_id: str | None,
    requester_id: str | None,
    session_id: str | None,
    declined: bool = False,
    decline_reason: str = "",
)

ToolAfterCallContext(
    tool_name: str,
    arguments: dict[str, Any],
    agent_name: str,
    room_id: str | None,
    thread_id: str | None,
    requester_id: str | None,
    session_id: str | None,
    result: object | None,
    error: BaseException | None,
    blocked: bool,
    duration_ms: float,
)
```

For `schedule:fired`, `ScheduleFiredContext.thread_id` is the resolved delivery thread.
This may differ from `workflow.thread_id` when the workflow starts a new thread or resolves to room mode.

## Testing

Hook tests follow standard pytest patterns.
Build a registry from stub plugins and invoke the execution helpers directly.

### Testing an observer hook

```python
import pytest

from mindroom.hooks import EVENT_MESSAGE_RECEIVED, HookRegistry, MessageReceivedContext, hook
from mindroom.hooks.execution import emit


@hook(EVENT_MESSAGE_RECEIVED)
async def suppress_spam(ctx):
    if "spam" in ctx.envelope.body:
        ctx.suppress = True


@pytest.mark.asyncio
async def test_suppress_spam(hook_context_factory):
    registry = HookRegistry.from_plugins([stub_plugin("demo", [suppress_spam])])
    ctx = hook_context_factory(MessageReceivedContext, body="buy spam now")

    await emit(registry, EVENT_MESSAGE_RECEIVED, ctx)

    assert ctx.suppress is True
```

### Testing an enrichment hook

```python
import pytest

from mindroom.hooks import EVENT_MESSAGE_ENRICH, HookRegistry, hook
from mindroom.hooks.execution import emit_collect


@hook(EVENT_MESSAGE_ENRICH)
async def enrich_with_time(ctx):
    ctx.add_metadata("time", "2026-03-23T10:00:00Z")


@pytest.mark.asyncio
async def test_enrichment(hook_context_factory):
    registry = HookRegistry.from_plugins([stub_plugin("demo", [enrich_with_time])])
    ctx = hook_context_factory("MessageEnrichContext")

    items = await emit_collect(registry, EVENT_MESSAGE_ENRICH, ctx)

    assert len(items) == 1
    assert items[0].key == "time"
```

### Testing a transformer hook

```python
import pytest

from mindroom.hooks import EVENT_MESSAGE_BEFORE_RESPONSE, HookRegistry, hook
from mindroom.hooks.execution import emit_transform


@hook(EVENT_MESSAGE_BEFORE_RESPONSE)
async def append_footer(ctx):
    ctx.draft.response_text += "\n-- Footer"


@pytest.mark.asyncio
async def test_append_footer(hook_context_factory):
    registry = HookRegistry.from_plugins([stub_plugin("demo", [append_footer])])
    ctx = hook_context_factory("BeforeResponseContext", response_text="Hello")

    result = await emit_transform(registry, EVENT_MESSAGE_BEFORE_RESPONSE, ctx)

    assert result.response_text == "Hello\n-- Footer"
```

### Creating stub plugins for tests

```python
from mindroom.config.plugin import PluginEntryConfig


def stub_plugin(name, callbacks, *, plugin_order=0, settings=None, hooks=None):
    return type(
        "PluginStub",
        (),
        {
            "name": name,
            "discovered_hooks": tuple(callbacks),
            "entry_config": PluginEntryConfig(
                path=f"./plugins/{name}",
                settings=settings or {},
                hooks=hooks or {},
            ),
            "plugin_order": plugin_order,
        },
    )()
```

## Migration

Existing plugins work with zero changes.
A manifest with only `name`, `tools_module`, and `skills` behaves exactly as before.

To adopt hooks:

1. Add `@hook(...)` decorators to the existing `tools_module`. MindRoom auto-scans and discovers them.
2. Switch the plugin config entry from string to object form only when you need `settings` or per-hook overrides.
3. Add `hooks_module` to the manifest later if you want to separate hook code from tool code.

### What stays the same

- `plugins: list[str]` config works unchanged
- Tool names remain globally unique
- Per-agent tool filtering (`tools: [file, shell]`) is unchanged
- Skill allowlists are unchanged
- Hot reload rebuilds the hook registry from scratch and swaps atomically

### What is out of scope

- Hooks cannot replace core routing, authorization, or deduplication
- No hook context exposes the Matrix client directly
- No automatic retries in the hook runtime
- No cross-worker custom event IPC (primary process only)
