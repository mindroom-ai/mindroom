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
| `message:before_response` | Transformer | `BeforeResponseContext` | After AI generation; before Matrix send (streaming: after stream completes, before final edit) | `draft.response_text`, `draft.suppress` |
| `message:after_response` | Observer | `AfterResponseContext` | After final Matrix send or edit | None (frozen) |
| `agent:started` | Observer | `AgentLifecycleContext` | After bot starts (Matrix login, presence, callbacks registered) | None (frozen) |
| `agent:stopped` | Observer | `AgentLifecycleContext` | During orderly shutdown | None (frozen) |
| `schedule:fired` | Observer | `ScheduleFiredContext` | Before scheduled task posts its synthetic message | `message_text`, `suppress` |
| `reaction:received` | Observer | `ReactionReceivedContext` | After built-in reaction handlers (stop, config, interactive) | None (frozen) |
| `config:reloaded` | Observer | `ConfigReloadedContext` | After orchestrator applies new config and restarts affected entities | None (frozen) |
| `tool:before_call` | Gate | `ToolBeforeCallContext` | Immediately before each tool call runs | `decline()` |
| `tool:after_call` | Observer | `ToolAfterCallContext` | After each tool call returns, raises, or is declined | None (observer result snapshot) |

### Default timeouts

| Event | Default timeout (ms) |
| --- | --- |
| `message:received` | 15000 |
| `message:enrich` | 2000 |
| `message:before_response` | 200 |
| `message:after_response` | 3000 |
| `reaction:received` | 500 |
| `schedule:fired` | 1000 |
| `agent:started` | 5000 |
| `agent:stopped` | 5000 |
| `config:reloaded` | 5000 |
| `tool:before_call` | 200 |
| `tool:after_call` | 300 |
| Custom events | 1000 |

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

The `message:enrich` event powers a full enrichment pipeline that injects live context into AI prompts without polluting session history.

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
4. **Strip from history**: After the response completes, MindRoom strips enrichment blocks from the persisted Agno session history so volatile data does not leak into future conversations.

### Cache policy

Each enrichment item has a `cache_policy`:

- `"volatile"` (default): The item may change on every message (e.g., weather, time).
- `"stable"`: The item changes rarely (e.g., user profile, timezone).

The enrichment digest (a SHA-256 hash of all items) is included in the AI response cache key, preventing stale-cache hits when volatile context changes.

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

## Custom events

Plugins can define and emit namespaced custom events.
Built-in namespaces (`message:*`, `agent:*`, `schedule:*`, `reaction:*`, `config:*`, `tool:*`) are reserved.

### Defining a custom event hook

```python
from mindroom.hooks import hook


@hook("todo:item_completed")
async def audit_completion(ctx):
    append_jsonl(ctx.state_root / "events.jsonl", {"item_id": ctx.payload["item_id"]})
```

### Emitting from hook code

```python
from mindroom.hooks.execution import emit


@hook("todo:item_added")
async def on_item_added(ctx):
    # Process the item, then emit a follow-up event
    await emit(ctx.hook_registry, "todo:processed", ctx)
```

### Emitting from tool code

Tools emit custom events through the runtime context:

```python
from mindroom.tool_system.runtime_context import emit_custom_event

# Inside a tool method:
await emit_custom_event("my-plugin", "todo:item_completed", {"item_id": "123"})
```

### Event name rules

- Pattern: `^[a-z0-9_.-]+(:[a-z0-9_.-]+)+$`
- Must contain at least one colon separator
- Reserved namespaces: `message`, `agent`, `schedule`, `reaction`, `config`, `tool`
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

### Circuit breaker

The runtime tracks consecutive failures per `(plugin_name, hook_name)`.
After **5 consecutive failures**, the hook enters a **5-minute cooldown** where it is skipped entirely.
The next successful invocation clears the failure count.

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

Every hook context also exposes `await ctx.send_message(room_id, text, *, thread_id=None, extra_content=None)`.
When a runtime sender is available, it sends a hook-originated Matrix message and returns the event ID when available.
When no sender is bound for the current runtime, it returns `None`.
For message-derived contexts, MindRoom automatically preserves the original requester in `com.mindroom.original_sender` so downstream routing, permissions, and memory attribution continue to use the human sender instead of the router relay.

### Transport objects

```python
MessageEnvelope(
    source_event_id: str,
    room_id: str,
    thread_id: str | None,
    resolved_thread_id: str | None,
    requester_id: str,
    sender_id: str,
    body: str,
    attachment_ids: tuple[str, ...],
    mentioned_agents: tuple[str, ...],
    agent_name: str,
    source_kind: str,  # "message", "edit", "voice", "image", "scheduled", "hook"
)

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
