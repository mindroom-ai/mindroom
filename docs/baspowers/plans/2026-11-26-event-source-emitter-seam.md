# EventSource Emitter Seam & Slack Watch Unification Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: implement this plan task-by-task with the skill named in **Execution**. Steps use checkbox (`- [ ]`) syntax.

**Execution:** `baspowers:subagent-driven-development` — multi-subsystem architectural change touching `reactive_triggers`, `plugin_api`, `custom_tools/triggers`, `api/room_triggers`, and `slack`; fresh `gemini4_neon@xhigh` subagents keep each task focused and verified before whole-PR `opus` + `astra` review.

**Goal:** Add an internal + plugin-extensible `EventSource` emitter seam into `ReactiveTriggerService` and migrate `slack_watch` onto it while retiring the parallel Slack watch store and tool.

**Architecture:** `ReactiveTriggerService` owns a token-fenced `EventSourceRegistry` that built-in integrations (`Integration`) and plugins (`PluginLifecycle.register_event_source` / `plugin_http.event_source_emitter`) register into. Registered sources plug into `triggers` discovery, subscribe/update validation (with server-owned opaque `binding`), event fan-out, pre-triage/pre-dispatch access checks, history gating, and Room Triggers UI privacy, enabling `SlackEventSource` to replace `watches.py` + `watch_delivery.py` + `slack_watch` cleanly.

**Tech Stack:** Python 3.13, Pydantic v2, SQLite WAL (`ReactiveTriggerStore`), FastAPI, matrix-nio, pytest / pytest-asyncio, Tach (`tach.toml`), Ruff, Mypy.

**Spec:** `docs/baspowers/specs/2026-11-26-event-source-emitter-seam-design.md`

## Global Constraints

- One PR on branch `feat/event-source-emitter-seam` (commits per task, never a stack of PRs).
- Zero new parameters or per-platform actions on `triggers(action=...)`; keep `watch_pr` intact.
- `source_id` must match `^[A-Za-z0-9_-]{1,64}$`; plugin IDs must equal `plugin_name` or start with `f"{plugin_name}_"` / `f"{plugin_name}-"`; reject collisions with built-ins, YAML `event_sources`, and other plugins.
- Automatic DB/state migrations only (`schema_version` 2 -> 3 with `applied_migrations`, one-time transactional `watches.json` + unexpired `slack/replay` thread root import before archiving `watches.json` to `watches.json.migrated`).
- Fail-closed legacy config validation in `src/mindroom/config/legacy_fields.py` for `slack_watch` (`RETIRED_CONSOLIDATED_FUNCTIONS["slack"]` and `retired_tool_override("slack", ...)`).
- Keep `src/mindroom/config/*` free of `mindroom.matrix.*` and top-level `cryptography` imports (`tests/test_config_architecture_boundaries.py`, `tests/test_import_graph.py`).
- Bump `PLUGIN_API_VERSION = (1, 36)` and pin every new `plugin_api` / `plugin_http` export in `tests/test_plugin_api.py`.
- Keep prod & test code compact (~1:1 test:prod ratio; target ~220 lines for `event_sources.py`, ~320 lines for `slack/event_source.py`, deleting 521 lines in `watches.py` + `watch_delivery.py`).
- Every commit ends with `--trailer "Assisted-by: MindRoom DevAgent:<model_id> <https://github.com/mindroom-ai/mindroom>"`.

## Review Focus

1. **Plugin hot-reload race:** New lifespan registers `source_id` before old lifespan stops; old teardown must NOT remove the replacement registration, and the old `EventSourceEmitter` must fail closed (`EventSourceInactiveError`) if called afterward.
2. **Forged or stripped OAuth `binding`:** Calling `triggers(action="update", match=...)` must NEVER overwrite, strip, or allow forging `ExternalEventSourceSpec.binding` (`team_id`, `slack_user_id`), and `binding` must never appear in `TriggerRow` or `_reactive_record_payload`.
3. **Private-room & history leakage:** When a second person is invited to or joins a private Slack watch room, (a) delivery skips fail-closed before triage, (b) `GET /api/rooms/{room_id}/triggers` hides `private_room_only` triggers from non-owners, and (c) `triggers(action="history")` refuses private Slack history from a shared room and redacts `triage_reason` on non-owner admin rows.
4. **Watch-turn prompt-injection defense across approvals:** `HOOK_SOURCE_KEY="event_source:slack"` on `REACTIVE_TRIGGER_SOURCE_KIND` must block Slack writes AND mutating `triggers` actions (including after approval resume), while NOT injecting `[posted automatically by hook ...]` in `execution_preparation.py` and allowing a fresh human message in the same thread to post normally.
5. **Idempotent `watches.json` migration:** Importing `watches.json` + live `slack/replay` watch thread roots runs once atomically under `watches.json.lock` with `applied_migrations` marker `"slack_watches_v1"`, archives `watches.json` only after commit, never resurrects a deleted trigger, and skips `apps.event.authorizations.list` when zero Slack rules match.

---

### Task 1: Core `EventSource` Registry, Emitter Seam & Reactive Trigger Extensions

**Files:**
- Create: `src/mindroom/reactive_triggers/event_sources.py`
- Modify: `src/mindroom/reactive_triggers/models.py`
- Modify: `src/mindroom/reactive_triggers/matching.py`
- Modify: `src/mindroom/reactive_triggers/store.py`
- Modify: `src/mindroom/reactive_triggers/dispatch.py`
- Modify: `src/mindroom/reactive_triggers/service.py`
- Modify: `src/mindroom/reactive_triggers/__init__.py`
- Test: `tests/test_event_source_seam.py`

**Interfaces:**
- Produces in `src/mindroom/reactive_triggers/event_sources.py`:
  - `EventSourceInactiveError(RuntimeError)`
  - `EventSourceDescriptor(source_id, description, allowed_kinds=(), match_fields=(), example=None, default_once=True, default_new_thread=False, default_thread_key_from_event=False, default_reopen_on_fire=False, private_room_only=False)`
  - `ValidatedSubscription(kind, match, binding=...)`
  - `EventSourceContext(owner_user_id, config, runtime_paths, target_room_id=None, target_agent=None, target_room_state=None, calling_room_id=None, calling_room_state=None, client=None, credentials_manager=None)`
  - `EventSource` base class with `source_id: str`, `describe_for_user`, `validate_subscription`, `matches_rule`, `check_delivery_access`, `check_history_access`
  - `EventSourceEmitter.emit(*, kind, event_id, text=None, title=None, summary=None, actor=None, thread_key=None, data=None, timestamp=None, owner_user_ids=None, audience=None, dedupe_key=None) -> int`
  - `EventSourceRegistry` (`register`, `unregister`, `get`, `all`, `emitter_for_plugin`, `is_active_token`)
- Produces in `models.py` / `store.py` / `service.py`:
  - `ExternalEventSourceSpec.match: dict[str, str | bool | int | list[str]]` and `.binding: dict[str, str | bool | int]`
  - `TriggerTargetSpec.thread_key_from_event: bool = False`
  - `TriggerEvent.thread_key: str | None = None`
  - `ReactiveTriggerStore`: schema v3 with `applied_migrations`, `has_migration(name)`, `import_triggers_once(migration_name, triggers, thread_keys)`, `get_bound_thread_keys(trigger_ids, thread_key, *, ttl_seconds=..., now=None) -> frozenset[str]`, pruning expired `trigger_thread_keys` in `prune_old_firings`
  - `ReactiveTriggerService.event_sources: EventSourceRegistry` and `async def emit_event(...) -> int`

- [ ] **Step 1: Write failing tests in `tests/test_event_source_seam.py`**
  Cover: registry validation & collision rejection, reload token fencing (Review Focus #1), revoked emitter failing closed, `list[str]` membership in `matches_external_event`, `ExternalEventSourceSpec.binding` round-trip, `thread_key_from_event` grouping + sliding 7-day refresh on continuation + expiry pruning, `HOOK_SOURCE_KEY="event_source:<id>"` stamped on `external_event` dispatches without triggering `hook_source_from_content`, and `applied_migrations` + `import_triggers_once` idempotency.
- [ ] **Step 2: Run targeted pytest to confirm failures**
  Run: `NIX_PATH=nixpkgs=flake:nixpkgs nix-shell --run 'uv run pytest tests/test_event_source_seam.py -x -n 0 --no-cov'`
- [ ] **Step 3: Implement `event_sources.py` and extend `models.py`, `matching.py`, `store.py`, `dispatch.py`, `service.py`, `__init__.py`**
- [ ] **Step 4: Run targeted tests (`test_event_source_seam.py` + existing `test_reactive_triggers_*.py`)**
  Run: `NIX_PATH=nixpkgs=flake:nixpkgs nix-shell --run 'uv run pytest tests/test_event_source_seam.py tests/test_reactive_triggers_store.py tests/test_reactive_triggers_pipeline.py tests/test_reactive_triggers_service.py tests/test_reactive_triggers_external_sources.py -x -n 0 --no-cov'`
  Expected: PASS.
- [ ] **Step 5: Commit Task 1**

---

### Task 2: Plugin Lifecycle, Plugin HTTP & Plugin API Surface

**Files:**
- Modify: `src/mindroom/plugin_lifecycle.py`
- Modify: `src/mindroom/plugin_http.py`
- Modify: `src/mindroom/plugin_api.py`
- Modify: `src/mindroom/tool_system/plugin_imports.py` (`PLUGIN_API_VERSION = (1, 36)`)
- Modify: `src/mindroom/api/app_state.py` (`EmbeddedRuntime.reactive_triggers`)
- Modify: `src/mindroom/orchestrator.py` (wire `reactive_triggers` into `PluginLifespans`, `EmbeddedRuntime`, `bind_integrations`)
- Modify: `src/mindroom/integrations.py` (pass optional `reactive_triggers` / support integration event source registration)
- Modify: `tests/test_plugin_api.py`
- Modify: `tests/test_event_source_seam.py`

**Interfaces:**
- Consumes: Task 1 `EventSource`, `EventSourceRegistry`, `EventSourceEmitter`, `EventSourceInactiveError`, `ReactiveTriggerService.event_sources`
- Produces:
  - `PluginLifecycle.register_event_source(source: EventSource) -> EventSourceEmitter` (auto-unregistered in `finally` on startup failure, reload, disable, shutdown)
  - `plugin_http.event_source_emitter(request: Request, source_id: str) -> EventSourceEmitter`
  - `mindroom.plugin_api` exports: `EventSource`, `EventSourceContext`, `EventSourceDescriptor`, `EventSourceEmitter`, `EventSourceInactiveError`, `ValidatedSubscription`
  - `EmbeddedRuntime.reactive_triggers: ReactiveTriggerService`

- [ ] **Step 1: Add failing tests for plugin lifespan registration, reload fencing, startup-error cleanup, and plugin HTTP route `event_source_emitter` ownership**
- [ ] **Step 2: Run tests to verify failure**
- [ ] **Step 3: Wire `PluginLifecycle`, `PluginLifespans`, `plugin_http`, `plugin_api`, `PLUGIN_API_VERSION`, `EmbeddedRuntime`, `orchestrator`, and `integrations`; update `tests/test_plugin_api.py`**
- [ ] **Step 4: Run `test_plugin_api.py` and `test_event_source_seam.py`**
  Run: `NIX_PATH=nixpkgs=flake:nixpkgs nix-shell --run 'uv run pytest tests/test_plugin_api.py tests/test_event_source_seam.py -x -n 0 --no-cov'`
  Expected: PASS.
- [ ] **Step 5: Commit Task 2**

---

### Task 3: `triggers` Tool & Room Triggers UI Integration

**Files:**
- Modify: `src/mindroom/custom_tools/triggers.py`
- Modify: `src/mindroom/api/room_triggers.py`
- Test: `tests/test_event_source_seam.py`, `tests/api/test_room_triggers.py`, `tests/test_triggers_tool.py`

**Interfaces:**
- Consumes: Task 1 `EventSourceRegistry`, `EventSourceContext`, `EventSourceDescriptor`, `ValidatedSubscription`, `ExternalEventSourceSpec.binding`, `TriggerTargetSpec.thread_key_from_event`; Task 2 `EmbeddedRuntime.reactive_triggers`
- Produces:
  - `TriggersTools`: registered `EventSource` discovery in `list` (+ live `paused_reason`), subscribe/update validation + OAuth connect payload handling + descriptor defaults + immutable `binding` preservation, calling-room-aware `history` gating + redacted `triage_reason`, dry-run `test` via `matches_rule`, mutating action refusal on `event_source:*` / `slack-watch/*` turns.
  - `room_triggers.py`: `TriggerRow.source_id` + owner-only `paused_reason`; hides `private_room_only` registered event-source triggers from non-owners.

- [ ] **Step 1: Write failing tests covering Review Focus #2, #3, #4 on `TriggersTools` and `api/room_triggers.py`**
  Include: subscribe/list/update/history/test against a registered `EventSource`, `binding` preservation on `update(match=...)` and omission from payloads, `paused_reason` in `list` and Room Triggers UI, non-owner hiding of `private_room_only` triggers, `triage_reason` redaction on admin history, and refusal of mutating `triggers` actions on `event_source:*` turns.
- [ ] **Step 2: Run targeted tests to verify failure**
- [ ] **Step 3: Implement `custom_tools/triggers.py` and `api/room_triggers.py` updates**
- [ ] **Step 4: Run `test_event_source_seam.py`, `test_triggers_tool.py`, `tests/api/test_room_triggers.py`**
  Run: `NIX_PATH=nixpkgs=flake:nixpkgs nix-shell --run 'uv run pytest tests/test_event_source_seam.py tests/test_triggers_tool.py tests/api/test_room_triggers.py -x -n 0 --no-cov'`
  Expected: PASS.
- [ ] **Step 5: Commit Task 3**

---

### Task 4: Migrate `slack_watch` onto `SlackEventSource`, Retire `slack_watch`, Auto-Migrate `watches.json`, Update Tach & Docs

**Files:**
- Create: `src/mindroom/slack/event_source.py`
- Delete: `src/mindroom/slack/watches.py`
- Delete: `src/mindroom/slack/watch_delivery.py`
- Modify: `src/mindroom/slack/delivery.py`
- Modify: `src/mindroom/slack/events.py`
- Modify: `src/mindroom/slack/integration.py`
- Modify: `src/mindroom/custom_tools/slack.py`
- Modify: `src/mindroom/tools/slack.py`
- Modify: `src/mindroom/tools_metadata.json`
- Modify: `src/mindroom/config/legacy_fields.py`
- Modify: `tach.toml`
- Modify: `docs/tools/slack.md`, `docs/architecture/code-map.md`, `docs/plugins.md`
- Create: `tests/slack/test_event_source.py` (replacing `tests/slack/test_watches.py`)
- Delete: `tests/slack/test_watches.py`
- Modify: `tests/slack/test_delivery.py`, `tests/slack/test_toolkit.py`, `tests/slack/test_events.py`, `tests/slack/test_manifest_docs.py`

**Interfaces:**
- Consumes: Task 1-3 `EventSource`, `EventSourceRegistry`, `ReactiveTriggerService`, `TriggersTools`, `room_triggers`
- Produces:
  - `SlackEventSource` (`source_id="slack"`, kinds `message`/`mention`/`direct_message`, `match={"channel_id": ...}`, OAuth `(team_id, slack_user_id)` binding, private-room enforcement, `apps.event.authorizations.list` visibility isolation)
  - `emit_slack_watch_event(attempt, reactive_triggers) -> DeliveryResult`
  - `migrate_legacy_slack_watches(runtime_paths, store, config) -> int`
  - `started_by_slack_trigger(context) -> bool`
  - Retired `slack_watch` fail-closed validation in `config/legacy_fields.py`

- [ ] **Step 1: Write failing tests in `tests/slack/test_event_source.py` and update Slack delivery/toolkit/config tests**
  Cover: all Slack watch kinds (`message`, `mention`, `direct_message`), multi-channel `channel_id` filter (channel-only, never limiting DMs), bound thread continuation, `apps.event.authorizations.list` prefiltering & visibility isolation & transient retry, private room enforcement & OAuth account-change pause, watch-turn Slack write refusal (including approval resume & fresh human message allowance — Review Focus #4), `watches.json` + unexpired `slack/replay` thread root automatic migration & idempotency (Review Focus #5), and retired `slack_watch` fail-closed config validation (`tool_approval.rules` and `include_tools`/`exclude_tools`).
- [ ] **Step 2: Implement `src/mindroom/slack/event_source.py`, wire `delivery.py`/`events.py`/`integration.py`/`custom_tools/slack.py`/`tools/slack.py`/`tools_metadata.json`/`config/legacy_fields.py`, delete `watches.py` & `watch_delivery.py`**
- [ ] **Step 3: Update `tach.toml` and docs (`docs/tools/slack.md`, `docs/architecture/code-map.md`, `docs/plugins.md`)**
- [ ] **Step 4: Run verification gates**
  - `NIX_PATH=nixpkgs=flake:nixpkgs nix-shell --run 'uv run pytest tests/slack/ tests/test_event_source_seam.py tests/test_triggers_tool.py tests/api/test_room_triggers.py tests/test_plugin_api.py tests/test_import_graph.py tests/test_config_architecture_boundaries.py tests/test_tach_visibility.py tests/test_tool_approval_grants.py -x -n 0 --no-cov'`
  - `NIX_PATH=nixpkgs=flake:nixpkgs nix-shell --run 'uv run --with tach tach check --dependencies --interfaces'`
  - `NIX_PATH=nixpkgs=flake:nixpkgs nix-shell --run 'uv run ruff check src/ tests/'`
  - `NIX_PATH=nixpkgs=flake:nixpkgs nix-shell --run 'uv run mypy src/mindroom/reactive_triggers/ src/mindroom/slack/ src/mindroom/custom_tools/triggers.py src/mindroom/custom_tools/slack.py src/mindroom/api/room_triggers.py src/mindroom/plugin_api.py src/mindroom/plugin_lifecycle.py src/mindroom/plugin_http.py src/mindroom/integrations.py'`
  - Tool schema token comparison for `triggers` before vs after.
- [ ] **Step 5: Commit Task 4**
