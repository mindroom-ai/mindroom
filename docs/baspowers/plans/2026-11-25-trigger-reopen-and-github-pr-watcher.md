# Native Trigger Auto-Reopen (`reopen_on_fire`) + Native GitHub PR/CI Watcher Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: implement this plan task-by-task with the skill named in **Execution**. Steps use checkbox (`- [ ]`) syntax.

**Execution:** `baspowers:subagent-driven-development` — three cleanly bounded tasks with explicit interfaces, executed by `gemini4_neon@xhigh` subagents and reviewed end-to-end by `opus` and `astra`.

**Goal:** Add native `reopen_on_fire` auto-reopen and thread-default fixes to MindRoom triggers, then add a unified `github_pr` reactive trigger source + `watch_pr` tool supporting both instant GitHub webhook delivery and ETag-polled external PR/CI watching.

**Architecture:** `reopen_on_fire` lives on both `TriggerTargetSpec` and `ExternalTriggerTarget` and calls a shared fast-path helper in `thread_tags.py` immediately before Matrix send. `watch_pr` is a second method on `TriggersTools` backed by a new `github_pr` reactive source in `src/mindroom/reactive_triggers/`, bound at creation to either an authorized `GitHubEventSourceConfig` webhook or a non-blocking ETag REST poller inside `ReactiveTriggerService`, sharing owner-scoped GitHub credentials extracted from `api/github_requests.py`.

**Tech Stack:** Python 3.13, Pydantic v2, SQLite WAL (`reactive_triggers.db` v2), `httpx`, `pytest`, `tach`, `ruff`, `mypy`.

**Spec:** `docs/baspowers/specs/2026-11-25-trigger-reopen-and-github-pr-watcher-design.md`

## Global Constraints

- One branch and one PR (`feat/trigger-reopen-and-github-pr-watch`), organized as 3 phased commits plus docs commit.
- Backwards-compatible Pydantic models (`extra="forbid"`): every new field has a safe default so existing `reactive_triggers.db` and `triggers.json` records load cleanly.
- SQLite schema migration `1 -> 2` in `ReactiveTriggerStore._initialize_database()` preserves existing rows while still rejecting unknown/newer versions.
- Strict module boundaries (`uv run --with tach tach check --dependencies --interfaces`), slim config imports (`tests/test_config_architecture_boundaries.py`, `tests/test_import_graph.py`), and regenerated `src/mindroom/tools_metadata.json` + `frontend/src/test/fixtures/config-schema.json`.
- Hardcoded `https://api.github.com` only, no process `GITHUB_TOKEN`, no cross-user credential sharing, no `pull-request-plugin` changes, and no `*_AP_*` names anywhere.
- Every commit includes `--trailer "Assisted-by: MindRoom DevAgent:<model_id> <https://github.com/mindroom-ai/mindroom>"`.

## Review Focus

1. **Resolved thread not actually resolved / lacking power level on `reopen_on_fire`:** Fast-path GET returns `None` (0 PUTs) when open; permission/transport error logs a warning and still delivers the wake message instead of retrying into failure.
2. **Existing `create` tests & room-timeline targeting from inside a thread:** `triggers(action="create")` inside a thread now defaults `target.thread_id` to the current thread (update existing tests that expected `None`); `target_thread_id="room"` explicitly targets the room timeline and is rejected with `new_thread=True`.
3. **Webhook revocation & fork-PR CI attribution:** Revoking a user or repo from `event_sources.github` blocks webhook-backed `github_pr` delivery and never silently switches to polling; fork-PR `check_run`/`check_suite` webhooks with empty `pull_requests` match only via `(same authorized repo, checkpoint.head_sha)` and never leak across repos.
4. **Terminal merge/close surviving auto-disable & delivery retry:** Never disable a `github_pr` rule at enqueue when a terminal merge/close firing is enqueued; disable with `disabled_reason="pr_closed"` atomically inside `claim_firing` and allow `"pr_closed"` alongside `"once_consumed"` during claimed delivery retries.
5. **Poller efficiency, pagination, and rate-limit safety:** A `304` on `/pulls/{number}` still polls subscribed timeline/review-comments/check-runs; paginated timeline/comments batch into at most one summarized firing per cycle; `429` / `403` rate-limit responses pause the credential/anonymous bucket until `Retry-After` / `X-RateLimit-Reset`; restart does not replay seeded history; re-enabling a paused watch re-seeds silently.

---

### Task 1: Core Triggers — `reopen_on_fire`, `create` Thread-Default Fix, and `target_thread_id="room"`

**Files:**
- Modify: `src/mindroom/thread_tags.py`
- Modify: `src/mindroom/reactive_triggers/models.py`
- Modify: `src/mindroom/external_triggers/store.py`
- Modify: `src/mindroom/reactive_triggers/dispatch.py`
- Modify: `src/mindroom/external_triggers/executor.py`
- Modify: `src/mindroom/custom_tools/triggers.py`
- Modify: `src/mindroom/api/room_triggers.py`
- Modify: `tach.toml`
- Test: `tests/test_external_trigger_executor.py`, `tests/test_external_trigger_manager_tool.py`, `tests/test_reactive_triggers_pipeline.py`, `tests/test_triggers_tool.py`, `tests/api/test_room_triggers.py`

**Interfaces:**
- Produces:
  - `mindroom.thread_tags.reopen_thread_if_resolved(client: nio.AsyncClient, room_id: str, thread_root_id: str, *, requester_user_id: str | None = None) -> bool`
  - `TriggerTargetSpec.reopen_on_fire: bool = False`
  - `ExternalTriggerTarget.reopen_on_fire: bool = False`
  - `TriggerRow.reopen_on_fire: bool`
  - `TriggersTools.triggers(..., reopen_on_fire: bool | None = None)` supporting `reopen_on_fire` on `watch_room`, `watch_thread`, `subscribe_event`, `create`, `update`, defaulting `create` `target_thread_id` to current thread in current room, and accepting `target_thread_id="room"` across all creation actions.

- [ ] **Step 1: Write failing tests for `reopen_thread_if_resolved`, reactive/direct reopen dispatch, `create` thread default, and `target_thread_id="room"`**
  - `tests/test_external_trigger_executor.py`: test `reopen_on_fire=True` removes `resolved` tag before `send_matrix_message` (for fixed `thread_id` and `continue_thread_event_id`), is a no-op when not resolved or targeting room timeline, and logs a warning while still delivering when `remove_thread_tag` raises `ThreadTagsPermissionError`.
  - `tests/test_reactive_triggers_pipeline.py`: test `dispatch_reactive_trigger` with `reopen_on_fire=True` reopens before send (fixed thread, `origin`, existing `thread_key` continuation), skips reopen for `per_fire_root=True` or room timeline, and still delivers on reopen error.
  - `tests/test_external_trigger_manager_tool.py` & `tests/test_triggers_tool.py`: update existing `create` tests that run inside `$thread` to assert `"thread_id": "$thread"`, add tests for `target_thread_id="room"` (producing `thread_id=None`), rejecting `target_thread_id="room"` with `new_thread=True`, `reopen_on_fire=True` on `create` / `watch_room` / `watch_thread` / `subscribe_event` / `update`, and non-admin cross-room check when `context.room_id` is a room key.
  - `tests/api/test_room_triggers.py`: assert `reopen_on_fire` is projected on `TriggerRow`.

- [ ] **Step 2: Run targeted tests to confirm failures**
  - Run: `NIX_PATH=nixpkgs=flake:nixpkgs nix-shell --run 'uv run pytest tests/test_external_trigger_executor.py tests/test_external_trigger_manager_tool.py tests/test_reactive_triggers_pipeline.py tests/test_triggers_tool.py tests/api/test_room_triggers.py -x -n 0 --no-cov'`

- [ ] **Step 3: Implement Task 1 production changes**
  - Add `reopen_thread_if_resolved` to `src/mindroom/thread_tags.py` (single `get_thread_tag_content` check, `parse_thread_tag_record`, conditional `remove_thread_tag(..., requester_user_id=requester_user_id, expected_record=record)`).
  - Add `reopen_on_fire: bool = False` to `TriggerTargetSpec` and `ExternalTriggerTarget`, and `reopen_on_fire: bool` to `TriggerRow`.
  - Call `reopen_thread_if_resolved` best-effort (warn + continue on `ThreadTagsError` or exception) immediately before `send_matrix_message` in `src/mindroom/reactive_triggers/dispatch.py` and `src/mindroom/external_triggers/executor.py`.
  - Update `src/mindroom/custom_tools/triggers.py`: fix `_direct_target` room/thread resolution, normalize `target_thread_id="room"` -> `None` across creation paths (rejecting `new_thread=True`), wire `reopen_on_fire` through `_ACTION_ALLOWED_PARAMS`, `_UPDATABLE_FIELDS`, `_direct_target`, `_resolve_reactive_target`, and `_build_updated_reactive_record`.
  - Update `tach.toml` to allow `mindroom.reactive_triggers.dispatch` and `mindroom.external_triggers.executor` to depend on `mindroom.thread_tags`.

- [ ] **Step 4: Run tests, tach, ruff, and mypy**
  - Run targeted pytest suite + `uv run --with tach tach check --dependencies --interfaces` + `uv run ruff check` on changed files + `uv run mypy` on changed files.

- [ ] **Step 5: Commit Task 1**
  - Commit with message `feat(triggers): add reopen_on_fire and default direct trigger thread to current thread` and `Assisted-by` trailer.

---

### Task 2: Shared Owner-Scoped GitHub Client/Auth + Expanded GitHub Webhook Normalizers

**Files:**
- Create: `src/mindroom/github_client.py`
- Modify: `src/mindroom/api/github_requests.py`
- Modify: `src/mindroom/config/external_trigger_policy.py`
- Modify: `src/mindroom/reactive_triggers/external_sources.py`
- Modify: `tach.toml`
- Test: `tests/api/test_github_requests.py`, `tests/test_reactive_triggers_external_sources.py`, `tests/test_external_trigger_policy_config.py`

**Interfaces:**
- Produces:
  - `mindroom.github_client`:
    - `GITHUB_API_BASE = "https://api.github.com"`
    - `GITHUB_API_VERSION = "2022-11-28"`
    - `GITHUB_USER_TOKEN_SERVICE = "github_user_token"`
    - `GitHubAuthMode = Literal["token", "app", "anonymous"]`
    - `resolve_owner_github_token(owner_user_id: str, config: Config, runtime_paths: RuntimePaths) -> tuple[str | None, GitHubAuthMode]`
    - `github_redirect_next(response: httpx.Response, method: str, content: bytes | None) -> tuple[str, httpx.URL, bytes | None] | None`
    - `read_bounded_body(response: httpx.Response, limit: int) -> bytes | None`
    - `is_github_rate_limited(response: httpx.Response) -> bool`
    - `github_get_json(path: str, *, token: str | None, etag: str | None = None, timeout_seconds: float = 20.0, max_bytes: int = 2 * 1024 * 1024, client: httpx.AsyncClient | None = None) -> GitHubGetResult` (dataclass with `status_code`, `etag`, `data`, `headers`, `rate_limited`, `retry_after_seconds`)
  - `mindroom.reactive_triggers.external_sources`:
    - Normalizers for `issue_comment`, `pull_request_review_comment`, `check_run`, plus `head_sha` on `pull_request` and `number` + `pr_numbers: list[int]` on `check_suite` & `check_run`.
  - `GitHubEventSourceConfig.allowed_events` default includes `issue_comment`, `pull_request_review_comment`, `check_run`.

- [ ] **Step 1: Write failing tests for expanded GitHub webhook normalizers and `github_client`**
  - `tests/test_reactive_triggers_external_sources.py`: test normalization of `issue_comment` (PR vs non-PR), `pull_request_review_comment`, `check_run` (with `pull_requests` and empty `pull_requests`), `check_suite` (`number` + `pr_numbers`), and `pull_request` (`head_sha` preserved even when `merge_commit_sha` is set).
  - `tests/test_external_trigger_policy_config.py`: verify `GitHubEventSourceConfig` default `allowed_events` includes the new events.
  - `tests/test_github_client.py` (compact unit tests): test `resolve_owner_github_token` priority (PAT -> OAuth -> anonymous, raises on unreadable PAT) and `github_get_json` (304 ETag, same-host redirect only, body limit, rate-limit header parsing).

- [ ] **Step 2: Run targeted tests to verify failures**

- [ ] **Step 3: Implement `src/mindroom/github_client.py`, wire `src/mindroom/api/github_requests.py` to reuse it, expand `external_sources.py` & `external_trigger_policy.py`, update `tach.toml`**

- [ ] **Step 4: Run tests, tach, ruff, and mypy**
  - Include `tests/api/test_github_requests.py`, `tests/test_config_architecture_boundaries.py`, `tests/test_import_graph.py`.

- [ ] **Step 5: Commit Task 2**
  - Commit with message `feat(triggers): add shared GitHub client helpers and expand PR webhook normalizers` and `Assisted-by` trailer.

---

### Task 3: Native `github_pr` Reactive Source, SQLite v2 Checkpoint Store, ETag Poller, Webhook Fan-Out, and `watch_pr` Tool

**Files:**
- Modify: `src/mindroom/config/external_trigger_policy.py`
- Modify: `src/mindroom/reactive_triggers/models.py`
- Modify: `src/mindroom/reactive_triggers/store.py`
- Modify: `src/mindroom/reactive_triggers/management.py`
- Create: `src/mindroom/reactive_triggers/github_pr.py`
- Modify: `src/mindroom/reactive_triggers/external_sources.py`
- Modify: `src/mindroom/reactive_triggers/dispatch.py`
- Modify: `src/mindroom/reactive_triggers/service.py`
- Modify: `src/mindroom/custom_tools/triggers.py`
- Modify: `src/mindroom/tools/triggers.py`
- Modify: `src/mindroom/api/room_triggers.py`
- Modify: `tach.toml`
- Regenerate: `src/mindroom/tools_metadata.json`, `frontend/src/test/fixtures/config-schema.json`
- Test: create `tests/test_github_pr_watcher.py`; extend `tests/test_reactive_triggers_store.py`, `tests/test_reactive_triggers_service.py`, `tests/test_triggers_tool.py`, `tests/api/test_room_triggers.py`

**Interfaces:**
- Consumes:
  - Task 1: `TriggerTargetSpec.reopen_on_fire`, `target_thread_id="room"` handling.
  - Task 2: `mindroom.github_client`, expanded GitHub webhook normalizers.
- Produces:
  - `ExternalTriggerPolicyConfig`: `github_pr_polling_enabled: bool = True`, `min_github_pr_poll_interval_seconds: int = 60`, `max_github_pr_watches_per_owner: int = 20`.
  - `GitHubPRSourceSpec`, `GitHubPRCheckpoint`, `SourceType` including `"github_pr"`, `DisabledReason` including `"pr_closed"`.
  - `ReactiveTriggerStore` schema v2 (`github_pr_checkpoints` table + `1 -> 2` migration) and checkpoint/due-poll/terminal-claim methods.
  - `mindroom.reactive_triggers.github_pr`:
    - `find_covering_github_webhook_source(owner_user_id, repo, events, config, runtime_paths) -> tuple[str | None, list[str]]`
    - `match_github_webhook_to_pr_rule(rule, checkpoint, event, *, allowed_events) -> WebhookPRMatch`
    - `seed_github_pr_watch(repo, number, events, *, owner_user_id, config, runtime_paths, poll_interval_seconds, now) -> SeededPRState`
    - `GitHubPRPoller` class used by `ReactiveTriggerService` (non-blocking background task, bounded concurrency, per-bucket rate-limit pause, ETag conditional GETs, paginated timeline/comments, CI rollup, atomic checkpoint+enqueue).
  - `TriggersTools.watch_pr(...)` registered in `src/mindroom/tools/triggers.py` with `function_names=("triggers", "watch_pr")`.

- [ ] **Step 1: Write failing tests covering all Review Focus scenarios & core flows**
  - `tests/test_github_pr_watcher.py`:
    - `watch_pr` webhook selection for covered `mindroom-ai/*` repo vs poll selection for external `matrix-org/*` repo; rejects already-closed/merged PR; respects `github_pr_polling_enabled=False` and `max_github_pr_watches_per_owner`.
    - Webhook fan-out: PR comments, reviews, inline comments, merge/close; fork-PR `check_run`/`check_suite` matching via `head_sha` without cross-repo leakage; revocation blocks delivery and never falls back to polling.
    - ETag poller: 304 on PR still polls timeline/comments/checks; paginated timeline + review comments batch into one firing per cycle; restart does not replay; `ignore_bots=True` skips bots on activity but never on CI/merge/close; `ignore_actors` skips specified logins on activity; CI fail-fast + single all-green wake + reset on `head_sha` change; rate-limit 429/403 pauses bucket until `Retry-After`/`X-RateLimit-Reset`; terminal merge/close firing survives `pr_closed` auto-disable even across transient delivery retry; disabling & re-enabling re-seeds silently.
  - `tests/test_reactive_triggers_store.py`: v1 -> v2 SQLite migration preserves existing triggers/firings and creates `github_pr_checkpoints`; terminal claim sets `disabled_reason="pr_closed"`.
  - `tests/api/test_room_triggers.py`: `github_pr` rule surfaces with `kind="github_pr"`, `disabled_reason="pr_closed"`, `reopen_on_fire=True`.

- [ ] **Step 2: Run targeted tests to verify failures**

- [ ] **Step 3: Implement Task 3 production changes + regenerate `tools_metadata.json` and `config-schema.json`**
  - Keep `github_pr.py` focused and under 500 lines; reuse `github_client.py` and existing `ReactiveTriggerStore` / `ReactiveTriggerService` primitives.
  - Regenerate `src/mindroom/tools_metadata.json` via `python -c "from pathlib import Path; from mindroom.tool_system.metadata import export_tools_metadata; Path('src/mindroom/tools_metadata.json').write_text(export_tools_metadata())"` (or exact command in `tests/test_tools_metadata.py`) and `frontend/src/test/fixtures/config-schema.json` via `.github/scripts/generate_config_schema.py`.

- [ ] **Step 4: Run full trigger + schema + architecture test suite, tach, ruff, mypy**
  - Run all trigger, GitHub, metadata, schema, boundary, and import-graph tests.

- [ ] **Step 5: Commit Task 3**
  - Commit with message `feat(triggers): add native github_pr watcher with webhook fan-out and ETag polling` and `Assisted-by` trailer.
