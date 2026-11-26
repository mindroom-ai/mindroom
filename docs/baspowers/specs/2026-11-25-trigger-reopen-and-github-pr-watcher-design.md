# Native Trigger Auto-Reopen (`reopen_on_fire`) + Native GitHub PR/CI Watcher

> **Implementation Note (unified architecture):** The shipped implementation unifies owned and external PR watching onto **one ETag REST poller (`src/mindroom/reactive_triggers/github_pr.py`) with signed GitHub webhooks acting as an instant nudge** (zero idle polling when webhook-backed, fail-closed on revocation), rather than maintaining a second webhook PR matching engine.

Date: 2026-11-25. Status: designed and vetted with Neon (`gemini4_neon@xhigh`) and Astra (`astra`); ready for implementation planning.

## Intent

Eliminate hand-rolled Python polling scripts, Ed25519 keys, and systemd timers for two recurring MindRoom workflows:

1. **Auto-reopening resolved threads when a trigger fires:** Users resolve waiting threads so they stay out of the inbox, then want any trigger firing into that thread (reactive or direct webhook) to automatically clear the `resolved` tag before/as the agent wakes, without relying on prompt instructions.
2. **Watching GitHub PRs/CI/merges/closes with one native call (`watch_pr`):**
   - For owned repos covered by a configured shared `github` webhook event source (e.g. `mindroom-ai/*`): bind to the webhook source for instant, zero-polling delivery.
   - For external repos (e.g. `matrix-org/*`, `ionic-team/capacitor`, `ggml-org/llama.cpp`): poll GitHub REST API on a shared internal scheduler inside `ReactiveTriggerService` using `ETag` / `If-None-Match` conditional requests, covering timeline events, inline review comments, and CI checks, auto-disabling on merge/close, and surfacing cleanly in the Room Triggers menu.
3. **Fixing the `triggers(action="create")` thread-default inconsistency:** `create` previously defaulted `target_thread_id` to `None` (room timeline) even when called inside a thread, unlike `watch_thread` and `subscribe_event`. Add `target_thread_id="room"` as an explicit escape hatch across creation actions.

## Explicit Non-Goals (v1)

- No `pull-request-plugin` changes or new plugin API exposure for reactive triggers.
- No GraphQL polling (lacks ETag support).
- No hybrid dual-transport rules (one rule uses either webhook OR polling, never both).
- No GitHub Actions job-log fetching (deliver check name, conclusion, and URL; woken agent inspects logs on demand).
- No repo-wide / issue-only / discussion / wildcard watches in `watch_pr` (single `(repo, number)` PR scope only).
- No GitHub Enterprise / custom API base URLs (hardcode `https://api.github.com`).
- No MindRoom Chat frontend blockers (`RoomTriggerRow.tsx` already falls back safely for new trigger kinds and ignores extra fields; an optional Chat UI label PR can follow independently).
- No webhook review-burst coalescing state machine (poller naturally batches all new items per cycle into one firing; webhook path uses standard delivery-ID deduplication).

---

## Architecture

### Part 1: Native `reopen_on_fire`, `create` Thread-Default Fix, and `target_thread_id="room"`

#### 1.1 Data Models
- Add `reopen_on_fire: bool = False` to:
  - `TriggerTargetSpec` (`src/mindroom/reactive_triggers/models.py`)
  - `ExternalTriggerTarget` (`src/mindroom/external_triggers/store.py`)
- Both models use `extra="forbid"`, so a defaulted field is 100% backwards-compatible with existing `reactive_triggers.db` rows and `triggers.json` records.
- Add `reopen_on_fire: bool` to `TriggerRow` (`src/mindroom/api/room_triggers.py`).

#### 1.2 Shared Reopen Helper (`src/mindroom/thread_tags.py`)
Add `reopen_thread_if_resolved(client, room_id, thread_root_id, *, requester_user_id) -> bool`:
1. Fast-path single state GET via `get_thread_tag_content(client, room_id, thread_root_id, RESOLVED_THREAD_TAG)`. If `None`, return `False` immediately (zero extra requests).
2. Parse via `parse_thread_tag_record(RESOLVED_THREAD_TAG, content)`. If `None`, return `False`.
3. Call `remove_thread_tag(client, room_id, thread_root_id, RESOLVED_THREAD_TAG, requester_user_id=requester_user_id, expected_record=record)`.
   - Passing `requester_user_id` enforces the trigger owner's Matrix room power level (preventing confused-deputy reopens).
   - Passing `expected_record` prevents clobbering a concurrent re-resolve.
   - Removing `resolved` writes the normal empty tombstone; any `thread_tag` rule watching `resolved:removed` fires normally (genuine state transition; no self-loop possible).

#### 1.3 Dispatch Timing & Failure Handling
- Call `reopen_thread_if_resolved` **after** authorization, claim, and final thread root resolution, **immediately before** `send_matrix_message`:
  - `src/mindroom/reactive_triggers/dispatch.py`: when `rule.target.reopen_on_fire and resolved_thread.thread_event_id is not None and not resolved_thread.per_fire_root` (covers fixed `thread_id`, `origin`, and existing `thread_key` continuations; skips room timeline and brand-new roots).
  - `src/mindroom/external_triggers/executor.py`: when `snapshot.target.reopen_on_fire and thread_event_id is not None` (covers fixed `thread_id` and `continue_thread_event_id`; skips room timeline and brand-new roots).
- **Best-effort on failure:** catch `ThreadTagsError` and transport/unexpected exceptions, log a structured warning, and proceed with `send_matrix_message`. Never fail or retry a trigger wake solely because reopening failed.

#### 1.4 `triggers(action="create")` Default Thread & `target_thread_id="room"` Escape Hatch
- In `src/mindroom/custom_tools/triggers.py`:
  - Normalize `target_thread_id == "room"` to `None` **before** defaulting across all trigger creation paths (`create`, `watch_room`, `watch_thread`, `subscribe_event`, `watch_pr`). Reject `target_thread_id="room"` combined with `new_thread=True`. Never persist `"room"` in storage.
  - Fix `_direct_target` (used by `action="create"`):
    - Resolve `resolved_room_id = resolve_room_id(raw_room_id, context.runtime_paths)` and `current_room_id = resolve_room_id(context.room_id, context.runtime_paths)` up front (fixing non-admin cross-room check when `context.room_id` is a room key/alias).
    - When `target_thread_id is None` and `not new_thread` and `resolved_room_id == current_room_id`, default `thread_id` to `context.resolved_thread_id or context.thread_id` (matching `watch_thread` and `subscribe_event`).
  - Expose `reopen_on_fire: bool | None = None` on `triggers(...)` for `watch_room`, `watch_thread`, `subscribe_event`, `create`, and `update`.

---

### Part 2: Shared GitHub Client / Owner Credentials & Expanded Webhook Normalizers

#### 2.1 Shared Owner-Scoped GitHub Auth & Safe HTTP (`src/mindroom/github_client.py`)
Extract from `src/mindroom/api/github_requests.py` into a reusable module (imported by both `api/github_requests.py` and `reactive_triggers/github_pr.py`):
- `resolve_owner_github_token(owner_user_id: str, config: Config, runtime_paths: RuntimePaths) -> tuple[str | None, Literal["token", "app", "anonymous"]]`
  - Uses `owner_user_id` to resolve the owner's saved PAT (`github_user_token`) first, else their GitHub App OAuth connection (`github_oauth_provider()`), else `(None, "anonymous")`.
  - If the saved PAT exists but is unreadable (`CredentialsUnreadableError`), raise rather than silently falling back to another credential or anonymous.
  - Never uses process environment `GITHUB_TOKEN`, never shares credentials across users, never stores tokens in SQLite checkpoints.
- Safe fixed-host GitHub API request helper:
  - Hardcoded base `https://api.github.com` only.
  - `httpx.AsyncClient(follow_redirects=False)` with strict same-host redirect verification (`https`, `api.github.com`, no custom port, max 3 hops).
  - Bounded response body read (`_MAX_POLL_RESPONSE_BYTES = 2 * 1024 * 1024`).
  - `Accept: application/vnd.github+json`, `X-GitHub-Api-Version: 2022-11-28`.

#### 2.2 Expanded GitHub Webhook Normalizers (`src/mindroom/reactive_triggers/external_sources.py`)
- `_normalize_pull_request`: add explicit `"head_sha": head.get("sha")` (keeping `"sha"` backwards-compatible).
- `_normalize_issue_comment`: normalize `issue_comment` payloads (`repo`, `number`, `action`, `author`, `comment_id`, `is_pull_request`, `url`, `title`, `summary`).
- `_normalize_pull_request_review_comment`: normalize `pull_request_review_comment` payloads (`repo`, `number`, `action`, `author`, `comment_id`, `review_id`, `path`, `url`, `title`, `summary`).
- `_normalize_check_run`: normalize `check_run` payloads (`repo`, `id`, `action`, `name`, `status`, `conclusion`, `sha`, `number` for the first associated PR when present, `url`, `title`) and attach `"pr_numbers": list[int]` on `data` (preserving all associated PR numbers while keeping `number` as an `int` for `subscribe_event` `match` filters).
- `_normalize_check_suite`: also include `number` (first associated PR when present) and `"pr_numbers": list[int]` extracted from `check_suite.pull_requests[].number`.
- Update `GitHubEventSourceConfig.allowed_events` default in `src/mindroom/config/external_trigger_policy.py` to include `issue_comment`, `pull_request_review_comment`, and `check_run`.

---

### Part 3: Native GitHub PR/CI Watcher (`github_pr` Source + `watch_pr` Tool)

#### 3.1 Policy Config (`src/mindroom/config/external_trigger_policy.py`)
Add to `ExternalTriggerPolicyConfig`:
- `github_pr_polling_enabled: bool = True` — kill switch for outbound GitHub REST polling (webhook-backed `github_pr` rules continue working when `False`).
- `min_github_pr_poll_interval_seconds: int = Field(default=60, ge=30, le=3600)` — lower clamp for poll interval.
- `max_github_pr_watches_per_owner: int = Field(default=20, ge=1, le=200)` — sub-quota within `max_reactive_triggers_per_owner`.

#### 3.2 Data Models (`src/mindroom/reactive_triggers/models.py`)
- `SourceType = Literal["room_message", "thread_tag", "external_event", "github_pr"]`
- `DisabledReason = Literal["manual", "once_consumed", "pr_closed"]`
- `GitHubPREventType = Literal["activity", "ci", "merge", "close"]`
- `GitHubPRSourceSpec`:
  - `source_type: Literal["github_pr"] = "github_pr"`
  - `repo: str` — validated against `^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$`, rejecting `.` and `..` segments.
  - `number: int = Field(ge=1)`
  - `events: list[GitHubPREventType] = Field(default_factory=lambda: ["activity", "ci", "merge", "close"])` — validated non-empty and deduplicated preserving order. `merge` = closed with `merged=True`; `close` = closed without merge (`merged=False`).
  - `webhook_source_id: str | None = None` — bound at creation (`None` = poll-backed, non-`None` = webhook-backed). Never mutates between webhook and poll after creation (preventing revocation bypass).
  - `ignore_actors: list[str] = Field(default_factory=list)` — case-insensitive GitHub logins ignored for `"activity"` only.
  - `ignore_bots: bool = True` — ignores actors with `login.endswith("[bot]")` or `user.type == "Bot"` for `"activity"` **only** (never `"ci"`, `"merge"`, `"close"`).
  - `auto_disable_on_close: bool = True` — disables the trigger with `disabled_reason="pr_closed"` once the PR merges or closes.
  - `poll_interval_seconds: int = Field(default=300, ge=30, le=86400)` — ignored when `webhook_source_id` is set.
- `GitHubPRCheckpoint`:
  - `head_sha: str | None = None`
  - `pr_state: Literal["open", "closed", "merged"] = "open"`
  - `seeded: bool = False`
  - `next_poll_at: float = 0.0`
  - `consecutive_errors: int = 0`
  - `state: dict[str, Any] = Field(default_factory=dict)` — ETags, timeline page + bounded seen IDs, review comment `since`/max ID, CI check snapshot (`checks_by_id`, `notified_failed_checks`, `last_ci_overall`).
  - `updated_at: float`

#### 3.3 SQLite Store (`src/mindroom/reactive_triggers/store.py`)
- Bump `_SCHEMA_VERSION = 2`. In `_initialize_database()`, cleanly migrate version `1 -> 2` (create `github_pr_checkpoints` table + index and update `schema_version`), while still rejecting unknown or newer versions.
- New table `github_pr_checkpoints`:
  ```sql
  CREATE TABLE IF NOT EXISTS github_pr_checkpoints (
      trigger_id TEXT PRIMARY KEY REFERENCES reactive_triggers(trigger_id) ON DELETE CASCADE,
      head_sha TEXT,
      pr_state TEXT NOT NULL DEFAULT 'open',
      seeded INTEGER NOT NULL DEFAULT 0,
      next_poll_at REAL NOT NULL DEFAULT 0.0,
      consecutive_errors INTEGER NOT NULL DEFAULT 0,
      state_json TEXT NOT NULL DEFAULT '{}',
      updated_at REAL NOT NULL
  )
  ```
  Plus `CREATE INDEX IF NOT EXISTS idx_github_pr_checkpoints_due ON github_pr_checkpoints(next_poll_at)`.
- Populate `reactive_triggers.source_id = source.repo.lower()` for `GitHubPRSourceSpec` (reuses `idx_reactive_triggers_source_id` for O(1) webhook fan-out lookup).
- Enforce `max_github_pr_per_owner` inside `create_trigger` alongside `max_per_owner`.
- Add checkpoint methods: `get_github_pr_checkpoint`, `upsert_github_pr_checkpoint`, `update_github_pr_checkpoint_and_enqueue` (atomic checkpoint update + optional firing enqueue + optional immediate disable with `disabled_reason='pr_closed'` when terminal and no firing is enqueued), `list_due_github_pr_triggers(now=..., limit=...)`.
- **Terminal auto-disable semantics (matching `once_consumed`):**
  - Never disable a rule at enqueue time if a terminal firing was enqueued (`_check_pending_access` skips disabled rules).
  - Instead, terminal firings carry `event.data["terminal"] = True`. Inside `claim_firing()`, if `rule.source.source_type == "github_pr"` and `rule.source.auto_disable_on_close` and `firing.event.data.get("terminal") is True`, atomically set `enabled = 0, disabled_reason = 'pr_closed'` (unless already `once_consumed`).
  - In `service.py:_process_firing()`, allow `rule.disabled_reason in ("once_consumed", "pr_closed")` to finish retrying a claimed firing.
  - On re-enable (`refresh_reactive_state_on_reenable` in `management.py`), reset `seeded = False, next_poll_at = now, consecutive_errors = 0` on `github_pr_checkpoints` so a paused PR watch re-seeds silently on its next poll without replaying stale activity.

#### 3.4 Transport Selection & Webhook Fan-Out (`src/mindroom/reactive_triggers/github_pr.py` + `external_sources.py`)
- **At creation (`find_covering_github_webhook_source`):**
  - Search `config.external_trigger_policy.event_sources` (sorted by `source_id` for determinism) for a `GitHubEventSourceConfig` where:
    1. `is_source_subscriber_allowed(cfg, owner_user_id, config, runtime_paths)` is `True`;
    2. `repo.lower()` is in `{r.lower() for r in cfg.allowed_repos}`;
    3. `allowed_events` covers the core required events for `events`:
       - Always requires `"pull_request"` (for lifecycle + `head_sha` tracking).
       - If `"activity"` in `events`: requires `"issue_comment"` or `"pull_request_review"` (notes a warning if `"pull_request_review_comment"` is missing).
       - If `"ci"` in `events`: requires `"check_run"` or `"check_suite"`.
  - If matched → bind `webhook_source_id = source_id` (zero polling). Seed initial `head_sha` via 1 best-effort PR GET (if owner has credentials or repo is public) so fork PR CI webhooks match immediately.
  - If not matched → require `policy.github_pr_polling_enabled` (fail with clear error if polling is disabled), bind `webhook_source_id = None`, and eagerly seed the PR + subscribed endpoints at creation (fails fast on 404/401/403 or already-closed/merged PR).
- **At webhook fan-out (`fan_out_external_event`):**
  - Query enabled `github_pr` rules with `source_id = event_repo.lower()`.
  - Filter to rules with `rule.source.webhook_source_id == source_id` and unexpired and `is_source_subscriber_allowed(...)`.
  - Match PR:
    - Direct match: `event.data.get("number") == rule.source.number` or `rule.source.number in pr_numbers`.
    - Fork-PR CI fallback: for `check_run` / `check_suite` with empty `pr_numbers`, match `checkpoint.head_sha == event.data.get("sha")` (strictly scoped to the same authorized repo).
  - Refresh `checkpoint.head_sha` on `pull_request.opened` / `synchronize` / `reopened`.
  - Classify into `merge` / `close` / `activity` / `ci`:
    - `pull_request.merged` → `"merge"` (terminal).
    - `pull_request.closed` (with `merged=False`) → `"close"` (terminal).
    - `issue_comment.created` (with `is_pull_request=True`), `pull_request_review.submitted` (skipping empty-body `state == "commented"` wrapper reviews only when `pull_request_review_comment` is in the source's `allowed_events`), `pull_request_review_comment.created`, `pull_request.labeled` / `reopened` / `ready_for_review` → `"activity"` (filtered by `ignore_actors` and `ignore_bots`).
    - `check_run.completed` (when conclusion is failure/timed_out/action_required/cancelled) or `check_suite.completed` (failure or success summary) → `"ci"`.
  - If terminal (`merged` or `closed`):
    - If that terminal event is in `rule.source.events`, enqueue firing with `data["terminal"] = True` and update `checkpoint.pr_state`.
    - Else if `rule.source.auto_disable_on_close`, update `checkpoint.pr_state` and disable immediately with `disabled_reason="pr_closed"`.
- **Source access recheck (`check_reactive_source_access` in `dispatch.py`):**
  - Webhook-backed `github_pr`: recheck `webhook_source_id` still exists, `is_source_subscriber_allowed`, and `repo` still in `allowed_repos`. Revocation skips delivery and never falls back to polling.
  - Poll-backed `github_pr`: recheck `config.external_trigger_policy.github_pr_polling_enabled`.

#### 3.5 ETag REST Poller (`src/mindroom/reactive_triggers/github_pr.py` + `service.py`)
- **Non-blocking scheduler in `ReactiveTriggerService`:**
  - Triggered from `_run()` via a guarded background task (`_github_pr_poll_task`) every 15s (or when notified), never blocking `drain_due_firings_once()`. Cancelled cleanly in `stop()`.
  - Bounded concurrency (`asyncio.Semaphore(4)`), per-poll jitter (`±10%`).
- **Endpoints polled per rule (only what `events` requires):**
  1. `GET /repos/{repo}/pulls/{number}` (`If-None-Match: pr_etag`) — always fetched to observe `state`, `merged`, `merged_at`, `head.sha`, `title`, `html_url`. A `304` on `/pulls/{number}` does **not** skip activity/CI collections.
  2. If `"activity"` in `events`:
     - `GET /repos/{repo}/issues/{number}/timeline?per_page=100&page={timeline_page}` (`If-None-Match: timeline_etag`) — resume at saved tail page; if page returns 100 items, probe next page up to 3 pages per tick. Filter out noisy events (`subscribed`, `unsubscribed`, `mentioned`, `deployed`, `head_ref_deleted`, `head_ref_force_pushed`, `committed`, `base_ref_changed`, `automatic_base_change_succeeded`), `ignore_actors`, and (if `ignore_bots`) bot actors.
     - `GET /repos/{repo}/pulls/{number}/comments?per_page=100&since={since_iso}` (`If-None-Match: review_comments_etag`) — track `max_review_comment_id` and `since = last_poll - 60s` overlap; group inline comments cleanly by review ID.
  3. If `"ci"` in `events` and PR is still open and `head_sha` is known:
     - `GET /repos/{repo}/commits/{head_sha}/check-runs?per_page=100` (`If-None-Match: checks_etag`, up to 3 pages).
     - Reset CI tracking whenever `head_sha` changes.
     - Rollup logic: fire fail-fast on newly failed check runs (`conclusion in {"failure", "timed_out", "action_required", "cancelled"}` not yet notified for this `head_sha`), and fire once when all checks finish passing (`total > 0`, `pending == 0`, `failed == 0`, `last_ci_overall != "passing"`).
- **One summarized firing per poll cycle:**
  - Combine all newly observed deltas (activity items, CI transitions, merge/close) into one `TriggerEvent` (`source_type="github_pr"`, `kind="github_pr.update"` or `"github_pr.merged"` / `"github_pr.closed"`, concise `summary`, structured `data={"repo", "number", "head_sha", "state", "merged", "terminal", "items": [...]}`).
  - Dedupe key: deterministic SHA-256 over `(trigger_id, sorted(delta_keys))`.
  - Commit checkpoint update + firing enqueue atomically in `store.update_github_pr_checkpoint_and_enqueue(...)`.
- **Rate-limit & error backoff:**
  - In-memory pause map keyed by `(owner_user_id, auth_mode)` for authenticated tokens and `"anonymous"` globally for unauthenticated requests.
  - On `429` or `403` with `X-RateLimit-Remaining == "0"` or `Retry-After`, pause the bucket until `Retry-After` / `X-RateLimit-Reset` (clamped sensibly).
  - Stretch poll interval 2× when `X-RateLimit-Remaining` falls below safety reserve (`< 100` authed, `< 10` anonymous).
  - On repeated transient errors (`5xx`, network timeout) or `401`/`404`, apply exponential backoff per rule (`min(3600, poll_interval * 2**min(errors, 5))`).

#### 3.6 Tool Surface (`src/mindroom/custom_tools/triggers.py` + `src/mindroom/tools/triggers.py`)
- Add `watch_pr` as a second method on `TriggersTools`, updating `function_names=("triggers", "watch_pr")` in `src/mindroom/tools/triggers.py` and regenerating `src/mindroom/tools_metadata.json` and `frontend/src/test/fixtures/config-schema.json`.
- Signature:
  ```python
  async def watch_pr(
      self,
      repo: str,
      number: int,
      events: list[Literal["activity", "ci", "merge", "close"]] | None = None,
      prompt: str | None = None,
      description: str | None = None,
      ignore_actors: list[str] | None = None,
      ignore_bots: bool = True,
      auto_disable_on_close: bool = True,
      reopen_on_fire: bool = True,
      poll_interval_seconds: int | None = None,
      target_agent: str | None = None,
      target_room_id: str | None = None,
      target_thread_id: str | None = None,
      new_thread: bool = False,
      once: bool = False,
      expires_in_hours: float | None = 720.0,
      trigger_id: str | None = None,
  ) -> str:
  ```
- Defaults:
  - `events` → `["activity", "ci", "merge", "close"]`
  - `prompt` → `f"GitHub PR {repo}#{number} had new activity. Inspect the event data below and take any needed action."`
  - `description` → `f"Watch PR {repo}#{number}"`
  - `poll_interval_seconds` → `300`, clamped to `[policy.min_github_pr_poll_interval_seconds, 86400]`
  - `reopen_on_fire` → `True`
  - `expires_in_hours` → `720.0` (30 days by default; pass `None` for no expiration, or any positive value up to `8760.0`).
- Return payload includes `trigger`, `delivery_mode` (`"webhook"` or `"poll"`), `webhook_source_id`, `auth_mode` (when polling), `pr_state`, `head_sha`, and any `coverage_notes`.
- Existing `triggers(...)` actions (`list`, `update`, `set_enabled`, `delete`, `history`, `test`) support `github_pr` records cleanly.

---

## Testing Strategy

1. **Unit & integration tests for Part 1 (`reopen_on_fire` + `create` default + `target_thread_id="room"`):**
   - `reopen_thread_if_resolved`: no-op when not resolved (1 GET, 0 PUT); removes tag with `requester_user_id` + `expected_record` when resolved; returns cleanly on concurrent modification.
   - Reactive trigger dispatch & direct external trigger execution: reopens before `send_matrix_message` for fixed thread, `origin`, and `thread_key` continuation; skips reopen for room timeline and new thread roots; logs warning and still delivers on `ThreadTagsPermissionError` / failure.
   - `triggers(action="create")` inside a thread defaults `target.thread_id` to current thread; `target_thread_id="room"` forces `thread_id=None`; `target_thread_id="room"` + `new_thread=True` errors; `triggers(action="update")` toggles `reopen_on_fire`.
2. **Unit tests for Part 2 (shared GitHub auth + webhook normalizers):**
   - Existing `tests/api/test_github_requests.py` passes unchanged.
   - New normalizer tests for `issue_comment` (PR vs non-PR), `pull_request_review_comment`, `check_run`, `check_suite` PR numbers, and `pull_request.head_sha`.
3. **Unit & pipeline tests for Part 3 (`github_pr` source, poller, webhook fan-out, `watch_pr` tool, room triggers API):**
   - Schema v1 → v2 SQLite migration on an existing v1 DB preserves triggers/firings and creates `github_pr_checkpoints`.
   - `watch_pr`: chooses webhook when covered by authorized `GitHubEventSourceConfig`, chooses poll for external repos, rejects already-closed PRs, enforces `github_pr_polling_enabled` and `max_github_pr_watches_per_owner`.
   - Webhook fan-out: matches PR comments/reviews/merges/closes; matches fork-PR `check_run`/`check_suite` via `head_sha` without cross-repo leakage; revocation blocks delivery and never falls back to polling.
   - ETag poller: 304 on PR still polls activity/CI; paginated timeline & review comments batch into 1 firing per cycle; restart does not replay; CI fail-fast + single all-green wake-up + reset on `head_sha` change; rate-limit pause honoring `Retry-After` / `X-RateLimit-Reset`; terminal merge/close firing survives `pr_closed` auto-disable even across transient delivery retry; re-enabling re-seeds silently.
   - `api/room_triggers.py` surfaces `github_pr` triggers with `disabled_reason="pr_closed"` and `reopen_on_fire`.
4. **Architecture & schema checks:** `tach check --dependencies --interfaces`, `tests/test_tools_metadata.py`, `tests/test_config_schema.py`, `tests/test_config_architecture_boundaries.py`, `tests/test_import_graph.py`, `ruff`, `mypy`.
