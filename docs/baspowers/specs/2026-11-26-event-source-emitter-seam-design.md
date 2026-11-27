# EventSource Emitter Seam & Slack Watch Unification Design

**Issue:** `mindroom-ai/mindroom#4410`
**Date:** 2026-11-26
**Status:** Design approved via cross-model consultation (Gemini 4 Neon `xhigh` + GPT-6.2 Astra)

## Problem & Intent

Event-driven watching is currently split across two engines:
- `src/mindroom/reactive_triggers/` provides the unified durable rule engine (SQLite WAL store, dedupe, quotas, rate caps, tool-less triage, `reopen_on_fire`, firing history, dry-run `test`, and Room Triggers UI `/api/rooms/{room_id}/triggers`) covering `room_message`, `thread_tag`, `github_pr`, and YAML-configured `external_event` webhooks (`github`/`hmac`/`ed25519`).
- `src/mindroom/slack/watches.py` + `src/mindroom/slack/watch_delivery.py` + `slack_watch` built a parallel watch store (`watches.json`), delivery loop, and tool outside `reactive_triggers`.

We want:
1. A minimal, lifetime-safe **internal + plugin-extensible `EventSource` emitter seam** into `ReactiveTriggerService`, so built-in integrations (Slack) and plugins own their own OAuth, webhook/Socket Mode/PubSub/polling, and normalization, and emit normalized events into `reactive_triggers`.
2. Agents subscribing via the existing generic `triggers(action="subscribe_event", source_id=..., kind=..., match=..., ...)` with **zero per-platform actions or parameters** added to `triggers`.
3. `slack_watch` retired and migrated onto this shared seam — giving Slack watches triage, `reopen_on_fire`, history, dry-run `test`, and Room Triggers UI while preserving every existing Slack privacy/security invariant and automatically migrating existing `watches.json` files.
4. `watch_pr` kept intact.

## Architecture Overview

```
Built-in Integration (Slack)           Plugin (lifecycle_module / routes_module)
   Integration.bind / unbind              lifecycle.register_event_source(src)
              \                                    /
               v                                  v
         ReactiveTriggerService.event_sources (EventSourceRegistry)
               |                                  |
               | emit(...)                        | describe / validate /
               v                                  | check_delivery_access /
       ReactiveTriggerStore  <--------------------+ check_history_access
        (reactive_triggers.db)                    |
               |                                  |
               v                                  v
   ReactiveTriggerService drain  --->  TriggersTools (`triggers`) & Room Triggers UI
   (triage -> claim -> dispatch)       (`/api/rooms/{room_id}/triggers`)
```

## Component Design

### 1. `EventSource` Emitter Seam (`src/mindroom/reactive_triggers/event_sources.py`)

#### Data types & Protocol
- **`EventSourceDescriptor`** (frozen dataclass):
  - `source_id: str`
  - `description: str`
  - `allowed_kinds: tuple[str, ...] = ()`
  - `match_fields: tuple[str, ...] = ()`
  - `example: str | None = None`
  - `default_once: bool = True`
  - `default_new_thread: bool = False`
  - `default_thread_key_from_event: bool = False`
  - `default_reopen_on_fire: bool = False`
  - `private_room_only: bool = False`
- **`ValidatedSubscription`** (frozen dataclass):
  - `kind: str`
  - `match: dict[str, str | bool | int | list[str]]`
  - `binding: dict[str, str | bool | int] = field(default_factory=dict)`
- **`EventSourceContext`** (frozen dataclass):
  - `owner_user_id: str`
  - `config: Config`
  - `runtime_paths: RuntimePaths`
  - `target_room_id: str | None = None`
  - `target_agent: str | None = None`
  - `target_room_state: RoomStateSnapshot | None = None`
  - `calling_room_id: str | None = None`
  - `calling_room_state: RoomStateSnapshot | None = None`
  - `client: nio.AsyncClient | None = None`
  - `credentials_manager: CredentialsManager | None = None`
- **`EventSource`** (base class / protocol with sensible defaults so simple connectors need only `source_id`, `description`, `allowed_kinds`):
  - `source_id: str`
  - `async def describe_for_user(self, ctx: EventSourceContext) -> EventSourceDescriptor | None` — returns `None` when the source is hidden or unavailable for `ctx.owner_user_id`.
  - `async def validate_subscription(self, ctx: EventSourceContext, *, kind: str, match: Mapping[str, str | bool | int | list[str]], existing_binding: Mapping[str, str | bool | int] | None = None) -> ValidatedSubscription` — validates `kind`, `match`, prerequisites, and target room; produces normalized `match` and immutable server-owned `binding` (preserving `existing_binding` on `update`).
  - `def matches_rule(self, rule: ReactiveTriggerRecord, event: TriggerEvent, *, audience: object = None, has_thread_binding: bool = False) -> bool` — defaults to `matches_external_event(rule.source, event)`. Personal sources inspect ephemeral `audience` (never persisted in `TriggerEvent.data`) and `rule.source.binding`; an empty personal audience never broadcasts.
  - `async def check_delivery_access(self, ctx: EventSourceContext, *, rule: ReactiveTriggerRecord, event: TriggerEvent | None = None) -> DispatchResult | None` — live access gate invoked before triage, before dispatch, and to compute `paused_reason` in `list` / Room Triggers UI.
  - `async def check_history_access(self, ctx: EventSourceContext, *, rule: ReactiveTriggerRecord, event: TriggerEvent | None = None) -> bool` — checks whether the reader may view firing payloads in the current calling room.

#### `EventSourceRegistry` & `EventSourceEmitter`
- Owned by `ReactiveTriggerService` (primary runtime only).
- `source_id` must match `^[A-Za-z0-9_-]{1,64}$`.
- Plugin-registered IDs must equal `plugin_name` or start with `f"{plugin_name}_"` or `f"{plugin_name}-"`.
- Collisions with built-ins, YAML `config.external_trigger_policy.event_sources`, or other plugins raise `ValueError`.
- **Reload-safe token fencing:** Because `PluginLifespans._apply_published` starts the new lifespan before stopping the old one, registrations carry an opaque `token`. A plugin's new lifespan may atomically replace its own `source_id` entry with a new token; when the old lifespan exits, `unregister(source_id, old_token)` is a no-op if a newer token is active, and any call to `old_emitter.emit(...)` fails closed with `PluginInactiveError` / `EventSourceInactiveError`.
- **`EventSourceEmitter.emit(...)`** (`async def emit(...) -> int`):
  - Accepts `kind`, `event_id`, optional `text`, `title`, `summary`, `actor`, `thread_key`, `data`, `timestamp`, `owner_user_ids: Collection[str] | None = None`, `audience: object = None`, `dedupe_key: str | None = None`.
  - Stamps `source_type="external_event"` and `source_id=self.source_id` internally so a producer cannot forge another source's identity.
  - Delegates to `ReactiveTriggerService.emit_event(...)`: checks reactive policy enabled, queries enabled unexpired `external_event` rules for `source_id`, filters by `owner_user_ids` (via `equivalent_requester_ids`; empty collection matches 0 rules), batch-queries unexpired `trigger_thread_keys` when `event.thread_key` is set, calls `source.matches_rule(...)`, enqueues deduplicated firings with `expected_rule_uid=rule.rule_uid`, and calls `notify_work(poll_github_prs=False)`.

#### Wiring & Plugin Surface
- **Built-in integrations:** `Integration` (`src/mindroom/integrations.py`) registers Slack's `SlackEventSource` via `ReactiveTriggerService` (wired from `EmbeddedRuntime.reactive_triggers` and `orchestrator.py`), unregistering on `unbind`.
- **Plugins (`plugin_api.py`, `PLUGIN_API_VERSION = (1, 36)`):**
  - `PluginLifecycle.register_event_source(source: EventSource) -> EventSourceEmitter` (tracked on the lifecycle and auto-unregistered in `finally` on startup failure, reload, disable, or shutdown).
  - `plugin_http.event_source_emitter(request: Request, source_id: str) -> EventSourceEmitter` for plugin HTTP routes (`routes_module`), enforcing active plugin + ownership of `source_id` via `_route_plugin_name(request)`.
  - Export `EventSource`, `EventSourceContext`, `EventSourceDescriptor`, `EventSourceEmitter`, `EventSourceInactiveError`, `ValidatedSubscription` from `mindroom.plugin_api` and `event_source_emitter` from `mindroom.plugin_http`; pin in `tests/test_plugin_api.py`.
- Keep `/api/event-sources/{source_id}` strictly for YAML-configured webhook sources (`github`/`hmac`/`ed25519`), preserving the GitHub PR webhook nudge.

### 2. Minimal Extensions to `reactive_triggers`

- **`models.py`**:
  - `ExternalEventSourceSpec.match`: widen value type to `str | bool | int | list[str]` (non-empty `list[str]` items). Scalar equality stays strict; `list[str]` matches when `isinstance(actual, str) and actual in expected`.
  - `ExternalEventSourceSpec.binding`: `dict[str, str | bool | int] = Field(default_factory=dict)` — immutable server-owned binding stored in `spec_json` (no DB column migration). Never accepted from tool input, preserved unconditionally on `update`, omitted from public dumps (`_reactive_record_payload`, `TriggerRow`).
  - `TriggerTargetSpec.thread_key_from_event: bool = False` — requires `new_thread=True`, mutually exclusive with static `thread_key`.
  - `TriggerEvent.thread_key: str | None = None` — opaque external conversation thread key, distinct from Matrix `thread_id`.
- **`store.py`**:
  - Add `applied_migrations (name TEXT PRIMARY KEY, applied_at REAL NOT NULL)` table (schema version `2 -> 3`, automatic on startup).
  - Add `get_bound_thread_keys(trigger_ids, thread_key, *, ttl_seconds=7 * 86400, now=None) -> frozenset[str]` for batch lookup during emission.
  - Prune expired `trigger_thread_keys` rows (`updated_at < now - 7 * 86400`) inside `prune_old_firings(...)`.
- **`dispatch.py`**:
  - `_resolve_new_thread_target`: when `rule.target.thread_key_from_event`, resolve `effective_key = firing.event.thread_key`. Refresh `save_thread_key_binding(...)` on **every** successful delivery (both root creation and continuation) so the 7-day TTL slides from the latest message.
  - `_build_dispatch_content`: stamp `HOOK_SOURCE_KEY: f"event_source:{rule.source.source_id}"` on `ExternalEventSourceSpec` dispatches while keeping `SOURCE_KIND_KEY: REACTIVE_TRIGGER_SOURCE_KIND`. (`dispatch_source.hook_source_from_content` stays gated on hook source kinds, so `execution_preparation` does not inject hook labels, while `ingress_validation` and `approval/continuation_runner` propagate `hook_source` reliably.)
  - `check_reactive_source_access`: check `event_sources.get(source.source_id)` first (passing `target_room_state` from `known_room_states` with zero extra Matrix calls), falling back to YAML `check_external_event_source_access`.

### 3. Slack Watch Migration (`src/mindroom/slack/event_source.py`)

- **Retire `slack_watch`:**
  - Remove `slack_watch` from `SlackTools`, `src/mindroom/tools/slack.py`, and `src/mindroom/tools_metadata.json`.
  - Delete `src/mindroom/slack/watches.py` and `src/mindroom/slack/watch_delivery.py`.
  - Add `"slack": {"slack_watch": "triggers"}` to `RETIRED_CONSOLIDATED_FUNCTIONS` in `src/mindroom/config/legacy_fields.py` (fail-closed on `tool_approval.rules`) and reject `include_tools`/`exclude_tools` referencing `slack_watch` in `retired_tool_override("slack", ...)`.
- **`SlackEventSource` (`source_id = "slack"`):**
  - **Kinds:** `"message"` (mentions + DMs), `"mention"` (mentions + bound thread replies), `"direct_message"` (`im`/`mpim`). Never a raw channel firehose; self-authored Slack messages are always ignored.
  - **Channel filter:** optional `match={"channel_id": "C..."}` or `match={"channel_id": ["C...", "C..."]}`, normalized via `parse_conversation_target`, rejected for `kind == "direct_message"`, applied to channel messages only (never DMs).
  - **OAuth binding:** binds `{"team_id": team_id, "slack_user_id": slack_user_id}` from verified owner Slack OAuth claims at subscribe time (raising `OAuthConnectionRequired` if unconnected or unverified). Updates preserve binding; account changes pause delivery with `"Slack account changed; recreate this subscription"`.
  - **Private-room invariant:** enforced at subscribe time, before triage, before dispatch, and on history reads: owner (or alias) joined, no other person or non-MindRoom bot/bridge joined or invited, `m.room.history_visibility in {"invited", "joined"}`, fail-closed when room state cannot be read. Also verifies `has_personal_slack(config, rule.target.agent)` and Slack app `app_token`.
  - **Emission (`emit_slack_watch_event`):**
    - Prefilters enabled unexpired `source_id="slack"` rules before calling `apps.event.authorizations.list`.
    - Keeps `api/slack_events.py`'s 2.5s ACK budget and `retry_slack_event` (project answering agents and paginated `apps.event.authorizations.list` still need it); returns `"retry"` on transient Slack API failures.
    - Uses `event_id = f"{event.team_id}:{event.channel_id}:{event.ts}"` for rule-level dedupe of retries and `app_mention`+`message` pairs.
    - Sets `thread_key = event.channel_id` (DMs) or `f"{event.channel_id}:{event.thread_ts or event.ts}"` (channel threads).
    - Author and channel display-name lookups fail soft to raw IDs.
  - **Watch-turn write & mutation refusal:**
    - `custom_tools/slack.py` refuses `slack_post`/`edit`/`delete`/`react`/`upload` when `hook_source == "event_source:slack"` or starts with `"slack-watch/"`.
    - `custom_tools/triggers.py` refuses mutating actions (`watch_room`, `watch_thread`, `watch_pr`, `subscribe_event`, `create`, `update`, `set_enabled`, `delete`, `rotate_key`) when `hook_source` starts with `"event_source:"` or `"slack-watch/"`.
- **Automatic one-time `watches.json` migration:**
  - Runs on `ReactiveTriggerService` startup before serving events, guarded by `applied_migrations` marker `"slack_watches_v1"` and `watches.json.lock`.
  - Imports every watch from `<control_state>/slack/watches.json` (deterministic `trigger_id = f"slack_watch_{watch.watch_id}"`, collision-safe, bypassing per-owner quota) plus any unexpired `slack:watch:{watch_id}` thread roots from `<control_state>/slack/replay` in one SQLite transaction.
  - Archives `watches.json` to `watches.json.migrated` only after the SQLite transaction commits.

### 4. `triggers` Tool & Room Triggers UI Updates

- **`custom_tools/triggers.py`** (no new parameters; schema stays lean):
  - `list`: includes registered `EventSource` descriptors in `event_sources`, and attaches `paused_reason: str | None` on rules backed by a registered `EventSource`.
  - `subscribe_event`: supports registered `EventSource`s, surfaces `OAuthConnectionRequired` connect payloads cleanly, and applies descriptor defaults (`default_once`, `default_new_thread`, `default_thread_key_from_event`, `default_reopen_on_fire`) when omitted.
  - `update`: re-validates `match` through registered `EventSource.validate_subscription` while unconditionally preserving `binding`.
  - `history`: enforces `EventSource.check_history_access` with calling room state (refusing private Slack history from shared rooms) and redacts `triage_reason` when `include_event=False`.
  - `test`: evaluates `EventSource.matches_rule` (synthesizing owner audience for dry-run) + optional triage without mutating state.
- **`api/room_triggers.py`**:
  - Hides `private_room_only` registered event-source triggers from non-owners in `GET /api/rooms/{room_id}/triggers`.
  - Adds `source_id: str | None = None` and owner-only `paused_reason: str | None = None` to `TriggerRow`.

## Verification Strategy

1. Unit & integration tests for the `EventSource` registry, reload token fencing, plugin lifespan/HTTP route emission, `thread_key_from_event` grouping + sliding 7-day TTL, and `HOOK_SOURCE_KEY` mutation/write refusal (`tests/test_event_source_seam.py`).
2. Migrated Slack watch tests covering mention/DM/MPIM/channel filtering, thread continuation, `apps.event.authorizations.list` visibility isolation, private room enforcement, OAuth account change pause, watch-turn write refusal, `watches.json` + replay root migration, and `slack_watch` retired config fail-closed validation (`tests/slack/test_event_source.py`, `tests/slack/test_delivery.py`, `tests/slack/test_toolkit.py`).
3. Room Triggers API tests for non-owner hiding and `paused_reason` (`tests/test_room_triggers_api.py`).
4. `tests/test_plugin_api.py`, `tests/test_import_graph.py`, `tests/test_config_architecture_boundaries.py`, `uv run --with tach tach check --dependencies --interfaces`, `ruff check`, `mypy`.
5. Tool schema token-size check on `triggers` before vs after to confirm zero token bloat for weak models.
