# Reactive Triggers Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: implement this plan task-by-task with the skill named in **Execution**. Steps use checkbox (`- [ ]`) syntax.

**Execution:** `baspowers:subagent-driven-development` — 6 tightly-scoped tasks across a new isolated package + thin wiring, where per-task review catches schema, security, and `tach` issues early before later tasks build on them.

**Goal:** Build the unified event-driven `triggers` system (`room_message` + cheap triage, `thread_tag`/`resolved`, and shared `external_event` sources including GitHub HMAC-SHA256) by evolving `external_trigger_manager` into `triggers` with ~95% isolated code in `src/mindroom/reactive_triggers/`.

**Architecture:** One primary-runtime SQLite store (`reactive_triggers.db`) holds trigger rules, room cursors, thread checkpoints, and a `UNIQUE(trigger_id, dedupe_key)` firing ledger. Three thin source adapters feed one pipeline: deterministic filter → optional tool-less `judgment` triage → atomic claim (with `once` disable & rate limits) → owner-scoped Matrix delivery.

**Tech Stack:** Python 3.12+, Pydantic v2, SQLite (`sqlite3` WAL), FastAPI, `matrix-nio`, existing `mindroom.judgment`, `mindroom.external_triggers`, `mindroom.thread_tags`, `mindroom.event_journal`, `tach`.

**Spec:** `docs/baspowers/specs/2026-11-22-reactive-triggers-design.md`

## Global Constraints

- ~95% isolated in `src/mindroom/reactive_triggers/`; zero edits to reply turn loop, ingress, coalescing, streaming, or compaction.
- All new internal imports declared in `tach.toml` (`uv run --with tach tach check --dependencies --interfaces` must pass).
- Existing `external_trigger_manager` configs, Python imports, and `POST /api/triggers/{trigger_id}` API stay 100% backward-compatible.
- Never leak secrets in logs, errors, tool responses, or SQLite records (`secret_env` stores only env var name).
- Event payloads are untrusted data; triage is strictly tool-less and fail-closed (`decision is not True` drops without consuming `once=True`).
- Every commit ends with `--trailer "Assisted-by: MindRoom <display name>:<model id> <https://github.com/mindroom-ai/mindroom>"`.
- Never run `env`, `printenv`, `set`, or `env | grep`. Never `git stash`. Always `unset VIRTUAL_ENV` before `uv run`.

## Review Focus

1. **Triage failure must not consume `once=True`:** A cheap triage call that returns `False`, `None`, times out, or raises must record `triaged_no` and leave a `once=True` rule armed for the next matching event (tested in Task 1 & Task 2).
2. **Loop prevention on self-watched rooms:** A `room_message` trigger that posts its wake message into the same room it watches must never trigger itself or another `room_message` trigger (`SOURCE_KIND_KEY == "reactive_trigger"` and agent-authored messages skipped by default; tested in Task 2 & Task 4).
3. **Offline thread resolution reconciliation without double-fire:** A `thread_tag` rule created while a thread is unresolved must fire once if resolved while MindRoom is offline and caught on `restore_room`, must NOT fire twice if a live event arrives during restore, and must NOT misfire on creation if the thread is already resolved (tested in Task 4).
4. **GitHub webhook authorization & spoofing:** `POST /api/event-sources/{source_id}` must reject invalid/missing `X-Hub-Signature-256` or missing `X-GitHub-Delivery`, reject repos outside `allowed_repos`, prevent unsubscribed users from receiving private-repo events, and match strictly on structured fields (`repo`, `number`, `merged`) rather than PR title (tested in Task 3).
5. **Backward compatibility of `external_trigger_manager`:** Existing `config.yaml` entries using `external_trigger_manager`, `ExternalTriggerManagerTools` imports, `create_trigger(..., public_key=...)`, and `POST /api/triggers/{trigger_id}` must continue to work identically alongside the new `triggers` name and reactive trigger functions (tested in Task 5 & Task 6).

---

### Task 1: Config, Models & SQLite Store (`reactive_triggers` core data layer)

**Files:**
- Modify: `src/mindroom/config/external_trigger_policy.py`
- Modify: `src/mindroom/dispatch_source.py`
- Create: `src/mindroom/reactive_triggers/__init__.py`
- Create: `src/mindroom/reactive_triggers/models.py`
- Create: `src/mindroom/reactive_triggers/store.py`
- Modify: `tach.toml`
- Test: `tests/test_reactive_triggers_store.py`

**Interfaces:**
- Produces:
  - `ExternalEventSourceConfig` (`GitHubEventSourceConfig | Ed25519EventSourceConfig` discriminated union on `kind`) and policy fields (`reactive_enabled`, `max_reactive_triggers_per_owner`, `max_fires_per_hour`, `event_sources`) on `ExternalTriggerPolicyConfig`.
  - `REACTIVE_TRIGGER_SOURCE_KIND = "reactive_trigger"` in `src/mindroom/dispatch_source.py`.
  - Pydantic models in `mindroom.reactive_triggers.models`: `RoomMessageSourceSpec`, `ThreadTagSourceSpec`, `ExternalEventSourceSpec`, `TriageSpec`, `TriggerTargetSpec`, `ReactiveTriggerRecord`, `TriggerEvent`, `FiringRecord`, `FiringStatus`, `RoomCursor`, `ThreadCheckpoint`, `generate_trigger_id()`.
  - `ReactiveTriggerStore(runtime_paths: RuntimePaths)` in `mindroom.reactive_triggers.store` with SQLite WAL DB at `control_state_dir / "reactive_triggers.db"`:
    - `create_trigger(record, *, initial_cursor=None, initial_checkpoint=None, max_per_owner=...) -> ReactiveTriggerRecord`
    - `get_trigger(trigger_id) -> ReactiveTriggerRecord | None`
    - `list_triggers(*, owner_user_id=None, include_disabled=True, source_type=None, source_room_id=None, source_id=None) -> list[ReactiveTriggerRecord]`
    - `set_enabled(trigger_id, enabled: bool) -> ReactiveTriggerRecord | None`
    - `delete_trigger(trigger_id) -> bool`
    - `get_room_cursor(trigger_id) -> RoomCursor | None` / `advance_room_cursor(trigger_id, cursor: RoomCursor) -> None`
    - `get_thread_checkpoint(trigger_id) -> ThreadCheckpoint | None` / `update_thread_checkpoint_and_enqueue(trigger_id, *, tag_present: bool, state_event_id: str | None, firing_dedupe_key: str | None = None, event: TriggerEvent | None = None) -> bool`
    - `enqueue_firing(trigger_id, dedupe_key: str, event: TriggerEvent, *, advance_cursor: RoomCursor | None = None) -> bool`
    - `mark_triaged_no(firing_id: int, reason: str | None = None) -> None`
    - `claim_firing(firing_id: int, *, now: float | None = None, triage_reason: str | None = None) -> Literal["claimed", "disabled", "expired", "cooldown", "rate_limited", "missing"]`
    - `mark_delivered(firing_id: int, matrix_event_id: str) -> None`
    - `mark_delivery_retry(firing_id: int, error: str, *, now: float | None = None, max_attempts: int = 5) -> Literal["retrying", "failed"]`
    - `mark_skipped(firing_id: int, reason: str) -> None`
    - `list_due_firings(*, now: float | None = None, limit: int = 50) -> list[FiringRecord]`
    - `get_thread_key_binding(trigger_id: str, thread_key: str) -> str | None` / `save_thread_key_binding(trigger_id: str, thread_key: str, thread_root_id: str) -> None`
    - `prune_old_firings(*, retention_seconds: float = 30 * 86400, now: float | None = None) -> int`

- [ ] **Step 1: Write failing tests in `tests/test_reactive_triggers_store.py`**
  Cover: config validation (`GitHubEventSourceConfig`, `Ed25519EventSourceConfig`, empty `allowed_repos` rejected, `reactive_enabled` defaults), trigger CRUD + owner quota enforcement, `room_cursors` advance, `thread_checkpoints` atomic update + enqueue, `UNIQUE(trigger_id, dedupe_key)` deduplication, `mark_triaged_no` keeping `once=True` armed, `claim_firing` atomically disabling `once=True` + enforcing `cooldown_seconds` + `max_fires_per_hour` + `expires_at`, delivery retry backoff & terminal failure after 5 attempts, thread-key binding, and pruning.
- [ ] **Step 2: Run pytest to verify failure**
  Run: `unset VIRTUAL_ENV && NIX_PATH=nixpkgs=flake:nixpkgs nix-shell --run 'uv run pytest tests/test_reactive_triggers_store.py -x -n 0 --no-cov'`
- [ ] **Step 3: Implement config additions, `dispatch_source.py` constant, `models.py`, `store.py`, and `tach.toml` entries**
- [ ] **Step 4: Run tests and `tach check`**
  Run: `unset VIRTUAL_ENV && NIX_PATH=nixpkgs=flake:nixpkgs nix-shell --run 'uv run pytest tests/test_reactive_triggers_store.py -x -n 0 --no-cov && uv run --with tach tach check --dependencies --interfaces'`
- [ ] **Step 5: Commit**

---

### Task 2: Deterministic Matching, Tool-Less Cheap Triage & Owner-Scoped Dispatch

**Files:**
- Create: `src/mindroom/reactive_triggers/matching.py`
- Create: `src/mindroom/reactive_triggers/triage.py`
- Create: `src/mindroom/reactive_triggers/dispatch.py`
- Modify: `tach.toml`
- Test: `tests/test_reactive_triggers_pipeline.py`

**Interfaces:**
- Consumes: Task 1 models & store; `mindroom.judgment.evaluator.create_judgment_evaluator`, `mindroom.judgment.state.JudgmentQuestion`, `JudgmentMessage`, `build_judgment_request`; `mindroom.external_triggers.executor.is_external_trigger_owner_joined_target_room`; `mindroom.hooks.sender.send_matrix_message`; `mindroom.matrix.mentions.format_entity_mention`; `mindroom.matrix.message_builder.build_message_content`, `markdown_to_html`; `mindroom.authorization.is_sender_allowed_for_agent_reply_in_room`.
- Produces:
  - `matches_room_message(spec: RoomMessageSourceSpec, event: TriggerEvent, *, config: Config, runtime_paths: RuntimePaths) -> bool`
  - `matches_external_event(spec: ExternalEventSourceSpec, event: TriggerEvent) -> bool`
  - `is_reactive_trigger_event_content(content: Mapping[str, object]) -> bool`
  - `evaluate_triage(rule: ReactiveTriggerRecord, event: TriggerEvent, *, config: Config, runtime_paths: RuntimePaths) -> TriageOutcome` (`act: bool`, `reason: str | None`, `failure: str | None`)
  - `dispatch_reactive_trigger(*, rule: ReactiveTriggerRecord, firing: FiringRecord, triage_reason: str | None, store: ReactiveTriggerStore, target_bot: AgentBot, config: Config, runtime_paths: RuntimePaths, memberships: AgentReplyMembershipIndex) -> DispatchResult` (`status: Literal["delivered", "retry", "skipped"]`, `matrix_event_id: str | None`, `reason: str | None`)

- [ ] **Step 1: Write failing tests in `tests/test_reactive_triggers_pipeline.py`**
  Cover:
  - `matches_room_message`: sender allowlist with alias equivalence, thread filter, case-insensitive regex, skipping reactive/external-trigger messages (`loop guard`).
  - `matches_external_event`: `kind` exact match, structured `match` dotted/top-level equality (`repo`, `number`, `merged`), ignoring non-matching fields and rejecting type confusion (`True != 1`).
  - `evaluate_triage`: `llm`, `typesafe`, `openai_decisions` backends; framing event payload in `<untrusted_event_data>`; fail-closed on `False`, `None`, timeout, missing credential, or raised exception.
  - `dispatch_reactive_trigger`: owner authorization recheck, owner-joined-room recheck, `target_thread_id="origin"` (reply in triggering thread or root on room-level message), fixed cross-thread target (`$B`), `new_thread=True` with & without `thread_key`, `ORIGINAL_SENDER_KEY` + `SOURCE_KIND_KEY` + `io.mindroom.reactive_trigger.id` + `io.mindroom.reactive_trigger.dedupe_key` metadata stamps, prompt + `<untrusted_event_data>` body separation.
- [ ] **Step 2: Run pytest to verify failure**
- [ ] **Step 3: Implement `matching.py`, `triage.py`, `dispatch.py`, and `tach.toml` entries**
- [ ] **Step 4: Run tests and `tach check`**
- [ ] **Step 5: Commit**

---

### Task 3: Shared External Event Sources (GitHub HMAC-SHA256 + Ed25519 Fan-Out API)

**Files:**
- Create: `src/mindroom/reactive_triggers/external_sources.py`
- Modify: `src/mindroom/api/external_triggers.py` (add `/api/event-sources/{source_id}` router + runtime hook for reactive triggers without touching existing `/api/triggers/{trigger_id}` behavior)
- Modify: `src/mindroom/api/main.py` (mount event-sources router, pass reactive-triggers callback in `bind_external_trigger_runtime` / `unbind_external_trigger_runtime`)
- Modify: `tach.toml`
- Test: `tests/test_reactive_triggers_external_sources.py`

**Interfaces:**
- Consumes: Task 1 models & store, Task 2 `matches_external_event`, `mindroom.external_triggers.auth.verify_trigger_request`, `mindroom.delivery_replay.DeliveryReplayStore`, `mindroom.credentials.environment.get_secret_from_env`.
- Produces:
  - `is_source_subscriber_allowed(source: ExternalEventSourceConfig, owner_user_id: str, config: Config, runtime_paths: RuntimePaths) -> bool`
  - `validate_subscription_against_source(source: ExternalEventSourceConfig, *, kind: str, match: Mapping[str, str | int | bool]) -> None` (raises `ValueError` on disallowed repo/event/kind)
  - `verify_and_normalize_github_webhook(*, source_id: str, source: GitHubEventSourceConfig, headers: Mapping[str, str], body: bytes, runtime_paths: RuntimePaths) -> TriggerEvent`
  - `verify_and_normalize_ed25519_event(*, source_id: str, source: Ed25519EventSourceConfig, path: str, headers: Mapping[str, str], body: bytes, replay_store: DeliveryReplayStore) -> TriggerEvent`
  - `fan_out_external_event(*, source_id: str, source: ExternalEventSourceConfig, event: TriggerEvent, store: ReactiveTriggerStore, config: Config, runtime_paths: RuntimePaths) -> int` (number of newly enqueued firings)
  - FastAPI endpoint `POST /api/event-sources/{source_id}` returning `202 Accepted` `{accepted: true, source_id, event_id, matched_rules}`.

- [ ] **Step 1: Write failing tests in `tests/test_reactive_triggers_external_sources.py`**
  Cover:
  - GitHub HMAC-SHA256 valid signature, invalid signature (`401`), missing `X-GitHub-Delivery` (`400`), missing secret env (`503`), oversized body (`413`).
  - GitHub normalization: `pull_request` closed+merged → `pull_request.merged` (`data={repo, number, merged: True, branch, base_branch, author, sha, url}`), `pull_request` closed+unmerged → `pull_request.closed` (`merged: False`), `release.published`, `issues.opened`, `check_suite.completed`, `push`.
  - Repo allowlist enforcement (`403` / ignored for disallowed repo), subscriber allowlist enforcement (non-subscriber rule skipped), PR title spoofing does NOT match structured `match`.
  - Ed25519 shared source verification + nonce replay rejection (`409`).
  - Fan-out to 2 rules watching different PRs (only matching PR rule enqueued; duplicate `X-GitHub-Delivery` deduplicated; existing `/api/triggers/{trigger_id}` unaffected).
- [ ] **Step 2: Run pytest to verify failure**
- [ ] **Step 3: Implement `external_sources.py`, API endpoint, `main.py` router mount, and `tach.toml` entries**
- [ ] **Step 4: Run tests + existing `tests/test_external_triggers*.py` + `tach check`**
- [ ] **Step 5: Commit**

---

### Task 4: ReactiveTriggerService Runtime (Room Poller, Thread-Tag Observer/Reconciler, Async Delivery Loop & Wiring)

**Files:**
- Create: `src/mindroom/reactive_triggers/service.py`
- Modify: `src/mindroom/orchestrator.py` (~12 lines)
- Modify: `src/mindroom/bot.py` (~6 lines)
- Modify: `src/mindroom/runtime_protocols.py` (~3 lines)
- Modify: `src/mindroom/orchestration/external_trigger_runtime.py` (~8 lines)
- Modify: `tach.toml`
- Test: `tests/test_reactive_triggers_service.py`

**Interfaces:**
- Consumes: Tasks 1–3, `mindroom.background_tasks.SupervisedLoop`, `mindroom.thread_tags` (`THREAD_TAGS_EVENT_TYPE`, `parse_thread_tag_state_key`, `parse_thread_tag_record`, `get_thread_tags`), `mindroom.matrix.conversation_reads.projected_visible_messages`, `mindroom.matrix.transport_progress.is_unfinished_reply`, `mindroom.matrix.agent_authored`.
- Produces:
  - `ReactiveTriggerService` implementing `RuntimeService` (`name = "reactive_triggers"`, `sync(config)`, `stop()`):
    - `observe_state_event(room_id: str, source: Mapping[str, object]) -> None`
    - `restore_room(bot: AgentBot, room_id: str) -> None` (with `_live_during_restore` guard)
    - `notify_work() -> None` (wakes delivery loop immediately on live tag transition or external webhook ingest)
    - `poll_room_messages_once() -> None` & `drain_due_firings_once() -> None` (clean deterministic hooks for unit/integration tests without sleeping)

- [ ] **Step 1: Write failing tests in `tests/test_reactive_triggers_service.py`**
  Cover:
  - Room message end-to-end: poll reads journal after cursor, skips bot/reactive-trigger messages, runs triage (`False` drops without disabling `once=True`, `True` dispatches to `"origin"` thread and advances cursor across restart).
  - Thread tag end-to-end: live `resolved` state event fires cross-thread wake into `$B` and disables `once=True`; editing an already-resolved tag does not refire; reopening re-arms a repeating rule.
  - Startup reconciliation (`restore_room`): thread resolved while offline is caught and fires once; live event arriving during `restore_room` is not overwritten by stale snapshot (`_live_during_restore`); already-resolved checkpoint at creation does not misfire on startup.
  - External event drain: enqueued GitHub PR merge firing wakes target thread once and retries transiently failed Matrix send without double-firing after delivery.
  - Zero-rule fast path: loop does zero journal queries when no `room_message` rules are enabled.
- [ ] **Step 2: Run pytest to verify failure**
- [ ] **Step 3: Implement `service.py` and thin wiring in `orchestrator.py`, `bot.py`, `runtime_protocols.py`, `external_trigger_runtime.py`, and `tach.toml`**
- [ ] **Step 4: Run tests and `tach check`**
- [ ] **Step 5: Commit**

---

### Task 5: Unified `triggers` Toolkit (Evolving `external_trigger_manager`)

**Files:**
- Create: `src/mindroom/custom_tools/triggers.py`
- Create: `src/mindroom/tools/triggers.py`
- Modify: `src/mindroom/custom_tools/external_trigger_manager.py` (keep backward-compatible re-export / subclass)
- Modify: `src/mindroom/tools/external_trigger_manager.py` (keep registration or alias cleanly — verify with existing tests!)
- Modify: `src/mindroom/tools/__init__.py`
- Modify: `src/mindroom/config/tool_aliases.py` (`"external_trigger_manager": "triggers"`)
- Modify: `src/mindroom/config/models.py` (add `"triggers"` to `_DEFER_PROHIBITED_CONTROL_TOOLS`)
- Modify: `src/mindroom/tools_metadata.json` (regenerate via MindRoom's metadata script / check tests)
- Modify: `tach.toml`
- Test: `tests/test_triggers_tool.py` (and verify `tests/test_external_trigger_manager_tool.py`, `tests/test_mcp_gateway_tools.py`, `tests/test_sandbox_proxy.py`, `tests/test_tools_metadata.py` or equivalent)

**Interfaces:**
- Consumes: Tasks 1–4, `ExternalTriggerStore`, `require_external_trigger_owner_context`, `is_external_trigger_administrator`, `explicit_thread_root`, `resolve_room_id`, `get_thread_tags`.
- Produces:
  - `TriggersTools(Toolkit)` registered as `"triggers"` with `watch_room`, `watch_thread`, `subscribe_event`, `create_trigger`, `list_triggers`, `disable_trigger`, `delete_trigger`, `rotate_trigger_key`.
  - Canonical alias `"external_trigger_manager" -> "triggers"` in `TOOL_ALIASES`.

- [ ] **Step 1: Write failing tests in `tests/test_triggers_tool.py`**
  Cover:
  - `watch_room`: initializes cursor at journal head, validates triage model / regex / room & target access, rejects `target_thread_id` + `new_thread=True`, defaults `target_thread_id="origin"` when targeting same room.
  - `watch_thread`: initializes `thread_checkpoints` from current room state, sets `already_in_state=True` note if already resolved without firing, supports cross-thread `target_thread_id="$B"` and `once=True`.
  - `subscribe_event`: validates source exists, enforces `subscribers` allowlist + `allowed_repos`/`allowed_events`/`allowed_kinds`, stores structured `match`, supports `once=True`.
  - Unified inventory: `list_triggers` shows both direct Ed25519 triggers and reactive triggers; `disable_trigger` and `delete_trigger` work seamlessly on both; `trigger_id` collision across stores is rejected; non-admin cannot view/modify another owner's triggers or target unauthorized rooms/agents.
  - Backward compatibility: `config.yaml` with `tools: [external_trigger_manager]` canonicalizes to `triggers`; `ExternalTriggerManagerTools` import and `create_trigger(..., public_key=...)` still pass.
- [ ] **Step 2: Run pytest to verify failure**
- [ ] **Step 3: Implement `custom_tools/triggers.py`, `tools/triggers.py`, alias/metadata updates, and `tach.toml` entries**
- [ ] **Step 4: Run `tests/test_triggers_tool.py`, `tests/test_external_trigger_manager_tool.py`, `tests/test_mcp_gateway_tools.py`, `tests/test_sandbox_proxy.py`, and `tach check`**
- [ ] **Step 5: Commit**

---

### Task 6: Documentation, Parity Fixtures & Full Verification

**Files:**
- Modify: `docs/external-triggers.md` (document unified `triggers` toolkit, `watch_room`, `watch_thread`, `subscribe_event`, `external_trigger_policy.event_sources` including GitHub HMAC-SHA256 & Ed25519 fan-out, and `external_trigger_manager` alias)
- Modify: `docs/architecture/code-map.md` (add `reactive_triggers/` rows)
- Modify: any generated metadata/schema fixtures (`tools_metadata.json`, `tests/fixtures/agno_parity/tools/schemas.json` if checked by tests)

- [ ] **Step 1: Check all tests touching tool schemas/metadata/docs**
  Run: `unset VIRTUAL_ENV && NIX_PATH=nixpkgs=flake:nixpkgs nix-shell --run 'uv run pytest tests/test_reactive_triggers_*.py tests/test_triggers_tool.py tests/test_external_trigger*.py tests/test_mcp_gateway_tools.py -x -n 0 --no-cov'`
- [ ] **Step 2: Update `docs/external-triggers.md` and `docs/architecture/code-map.md`**
- [ ] **Step 3: Run `ruff check`, `ruff format --check` on changed files, `ty check` / typecheck, and `tach check --dependencies --interfaces`**
- [ ] **Step 4: Commit**
