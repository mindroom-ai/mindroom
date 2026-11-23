# Reactive Triggers: Unified Event-Driven Rules

Date: 2026-11-22. Status: approved by Bas, vetted by Fable (Claude Fable 5.2) and Astra (GPT-6.2 Astra), ready to implement.

## Intent

Let users and agents create durable event-driven triggers in chat across Matrix and external events—without running a polling background script per watch or creating overlapping trigger toolkits.

Three motivating scenarios named by Bas:
1. **Room watch with cheap triage:** "Watch room X; when a message arrives, quickly triage it with a cheap model and, if needed, act on it."
2. **Cross-thread lifecycle trigger:** "Whenever thread A resolves, get started on thread B and do such and such."
3. **External event trigger:** "When PR #4198 merges as an external event, do this."

Non-goals for v1:
- Replacing `scheduler` (time/cron remains in `scheduler`).
- Folding `callback_manager` into `triggers` (`mint_callback` stays a separate one-shot helper sharing the external trigger store).
- Launching `#4198` `agent-graphs` workflows (added as a second action target once `#4198` merges).
- Reactions, room-member joins, or MindRoom Chat UI panels (follow-up v2).
- Changing `agents.<name>.listen` semantics (passive room observation remains untouched).

## Architectural Decision: Tool Consolidation

Upgrade `external_trigger_manager` into the unified **`triggers`** toolkit instead of adding a competing `reactive_rules` toolkit:
- **`scheduler`** = time-based ("at 9am", "every Monday").
- **`triggers`** = event-based (all event triggers in one inventory: room messages, thread tags/resolution, shared external event sources, and direct Ed25519 webhooks).
- **`external_trigger_manager`** becomes a canonical alias for `triggers` in `TOOL_ALIASES` (`src/mindroom/config/tool_aliases.py`), and both names are listed in `_DEFER_PROHIBITED_CONTROL_TOOLS` (`src/mindroom/config/models.py`). Existing `config.yaml` files and `create_trigger(...)` calls keep working unchanged.
- `triggers` remains opt-in (not in `defaults.tools`) and respects `external_trigger_policy.enabled`.

## Unified Pipeline

Every reactive trigger flows through one primary-runtime pipeline:

```text
Source Adapter ──▶ Deterministic Filter ──▶ Optional Tool-Less Triage ──▶ Atomic Claim ──▶ Owner-Scoped Dispatch
(room / tag /      (exact fields/regex,     (judgment: llm / typesafe /   (SQLite UNIQUE   (Matrix mention with
 external)          no LLM cost)             openai_decisions)             + once disable)  owner & loop stamp)
```

Core invariant: **the primary runtime decides *whether* to act; the woken agent decides *how*.** A non-match creates neither a Matrix message nor an agent turn, and never consumes a `once=True` trigger.

## Data Model & SQLite Store (`src/mindroom/reactive_triggers/`)

Primary-only SQLite database at `<control_state>/reactive_triggers.db` (WAL mode, `busy_timeout=5000`, `schema_version` table).

### Tables
1. **`reactive_triggers`**:
   - `trigger_id TEXT PRIMARY KEY` (user-provided ASCII `[A-Za-z0-9_-]{1,64}` or auto-generated `tr_<12 hex>`)
   - `owner_user_id TEXT NOT NULL` (canonical requester Matrix ID; human only)
   - `source_type TEXT NOT NULL` (`room_message | thread_tag | external_event`)
   - `source_room_id TEXT` (resolved Matrix `!room` ID for `room_message` / `thread_tag`)
   - `source_id TEXT` (named external source key for `external_event`)
   - `enabled INTEGER NOT NULL DEFAULT 1`
   - `once INTEGER NOT NULL DEFAULT 0`
   - `expires_at REAL` (optional Unix timestamp)
   - `max_fires_per_hour INTEGER NOT NULL`
   - `cooldown_seconds INTEGER NOT NULL DEFAULT 0`
   - `fire_count INTEGER NOT NULL DEFAULT 0`
   - `last_fired_at REAL`
   - `spec_json TEXT NOT NULL` (validated `ReactiveTriggerRecord` JSON: source spec, filter, optional triage spec, target spec, description)
   - `created_at REAL NOT NULL`, `updated_at REAL NOT NULL`
2. **`room_cursors`**:
   - `trigger_id TEXT PRIMARY KEY REFERENCES reactive_triggers(trigger_id) ON DELETE CASCADE`
   - `created_ts INTEGER NOT NULL`
   - `logical_event_id TEXT NOT NULL`
   - `updated_at REAL NOT NULL`
3. **`thread_checkpoints`**:
   - `trigger_id TEXT PRIMARY KEY REFERENCES reactive_triggers(trigger_id) ON DELETE CASCADE`
   - `tag_present INTEGER NOT NULL` (0 or 1)
   - `state_event_id TEXT`
   - `updated_at REAL NOT NULL`
4. **`trigger_firings`**:
   - `id INTEGER PRIMARY KEY AUTOINCREMENT`
   - `trigger_id TEXT NOT NULL REFERENCES reactive_triggers(trigger_id) ON DELETE CASCADE`
   - `dedupe_key TEXT NOT NULL` (source-scoped stable key: Matrix event ID for room messages, `f"{state_event_id}"` or transition key for thread tags, `X-GitHub-Delivery` / `event_id` for external events)
   - `status TEXT NOT NULL` (`pending | triaged_no | claimed | delivered | failed | skipped`)
   - `event_json TEXT NOT NULL` (normalized `TriggerEvent` snapshot for async triage/delivery + retry)
   - `triage_reason TEXT`
   - `matrix_event_id TEXT`
   - `attempt_count INTEGER NOT NULL DEFAULT 0`
   - `next_retry_at REAL`
   - `error_message TEXT`
   - `created_at REAL NOT NULL`, `updated_at REAL NOT NULL`
   - `UNIQUE(trigger_id, dedupe_key)`

### Transactional Semantics
- **Idempotency:** Inserting a candidate row uses `INSERT OR IGNORE` on `UNIQUE(trigger_id, dedupe_key)`. Restart retries never double-queue or double-fire.
- **Triage no/error:** Updates the row to `status='triaged_no'` (or `'skipped'` on unrecoverable auth loss) without touching `enabled`, `fire_count`, or `last_fired_at`. A `once=True` rule stays armed.
- **Atomic claim:** In one `BEGIN IMMEDIATE` transaction: verify `enabled=1`, not expired, cooldown elapsed, and hourly `claimed`/`delivered` count `< max_fires_per_hour`; set row `status='claimed'`, increment `fire_count`, update `last_fired_at`, and if `once=1`, set `enabled=0`.
- **Delivery retry:** A `claimed` row whose Matrix send fails transiently stays `claimed` with exponential backoff (`next_retry_at`, capped at 5 attempts before `failed`), retrying with the same `dedupe_key` metadata.
- **Pruning:** Finished firing rows older than 30 days are pruned periodically.

## Event Sources

### 1. `room_message`
- Watches new visible messages in one resolved `source_room_id`.
- At creation, initializes `room_cursors` to the room's current journal head (or `(now_ms, "")` if unread) so history prior to rule creation is never replayed.
- Polled by `ReactiveTriggerService`'s `SupervisedLoop` every 5 seconds only when at least one enabled `room_message` rule exists.
- Reads from a joined running bot's `EventJournalStore` via `projected_visible_messages` after repairing sync gaps, stopping before unfinished streaming replies (`is_unfinished_reply`) and messages awaiting refetch (matching `listening/service.py`).
- Loop protection: skips messages authored by MindRoom bots/agents (`include_bots=False` by default) and always skips messages carrying `SOURCE_KIND_KEY == "reactive_trigger"` or `"external_trigger"` (and `io.mindroom.reactive_trigger.id`).
- Deterministic filters: optional `senders` (matched via `equivalent_requester_ids`), optional `thread_id` (only messages inside that thread), optional `pattern` (case-insensitive regex, max 256 chars, validated at creation).
- Access check: before triage/fire, verifies the rule owner is still joined to `source_room_id` and allowed to hear the sender via the serving bot's reply policy.
- Advances `room_cursors` atomically with queueing/skipping each message so restarts resume cleanly.

### 2. `thread_tag`
- Watches a thread's `com.mindroom.thread.tags` state event (`tag` default `"resolved"`, `transition: Literal["added", "removed"] = "added"`).
- `thread_id` is required for `watch_thread` (resolved via `explicit_thread_root`; defaults to the current thread if omitted in a threaded conversation).
- At creation, reads current tag presence from room state and stores it in `thread_checkpoints` without firing. If the tag is already in the target state (`added` when already present, or `removed` when already absent), `watch_thread` still succeeds (or reports `already_in_state=True`) and notes it will fire only on a future transition.
- **Live path:** `bot.py:_on_room_state_event` forwards `THREAD_TAGS_EVENT_TYPE` events to `orchestrator.reactive_triggers.observe_state_event(room_id, event.source)`.
- **Startup reconciliation:** `bot.py:_restore_joined_room` calls `orchestrator.reactive_triggers.restore_room(self, room_id)` with the same `_live_during_restore` guard as `ThreadSnoozeRuntimeCoordinator`, catching offline transitions that remain in the new state on startup without overwriting live events or double-firing.
- Transition logic: `absent → present` fires `"added"` rules and updates `tag_present=1`; `present → absent` fires `"removed"` rules (and re-arms repeating `"added"` rules) and updates `tag_present=0`. Editing an already-present tag (`present → present`) does not refire.

### 3. `external_event` (Shared Fan-Out Sources)
- Leaves existing direct `/api/triggers/{trigger_id}` Ed25519 endpoints untouched.
- Admin configures named sources under `external_trigger_policy.event_sources: dict[str, ExternalEventSourceConfig]`:
  - **`github` source:**
    - `kind: "github"`
    - `secret_env: str` (env var holding webhook secret; validated via `X-Hub-Signature-256` with `hmac.compare_digest`)
    - `allowed_repos: list[str]` (non-empty `owner/repo` allowlist; fail-closed)
    - `allowed_events: list[str]` (default `["pull_request", "pull_request_review", "check_suite", "release", "issues", "push"]`)
    - `subscribers: list[str]` (Matrix user IDs allowed to subscribe rules; trigger admins also allowed)
    - `max_body_bytes: int = 262144`
  - **`ed25519` source:**
    - `kind: "ed25519"`
    - `public_key: str`, `key_id: str = "default"`
    - `allowed_kinds: list[str]` (optional kind allowlist)
    - `subscribers: list[str]`
    - `replay_window_seconds: int = 300`, `max_body_bytes: int = 65536`
- Mounted at `POST /api/event-sources/{source_id}` (returns `202 Accepted` with `{accepted, source_id, event_id, matched_rules}` after durable SQLite enqueue).
- GitHub normalization:
  - Uses `X-GitHub-Delivery` as `event_id` (required header; rejects missing with `400`).
  - `pull_request`: if `action == "closed"` and `pull_request.merged is True`, `kind = "pull_request.merged"`; otherwise `kind = f"pull_request.{action}"`. Structured `data`: `{repo, number, action, merged, branch, base_branch, author, sha, url}`.
  - `pull_request_review`, `issues`, `release`, `check_suite`: `kind = f"{gh_event}.{action}"` with structured `{repo, number/tag/id, action, author, state/conclusion, url}`.
  - `push`: `kind = "push"` with structured `{repo, ref, branch, after, pusher, url}`.
  - Attacker-controlled free text (PR title/body) is placed in untrusted `summary`, never in structured `match` keys unless explicitly accessed, and `match` supports exact equality (`str | int | bool`) on structured `data` keys.
- **Split authorization:** Webhook signature authenticates the producer only. Subscriber creation and event fan-out both enforce `owner in subscribers` (or admin) and intersect rule filters with `allowed_repos` / `allowed_events` / `allowed_kinds`.

## Cheap Triage (`src/mindroom/reactive_triggers/triage.py`)

Optional `TriageSpec`:
- `instructions: str` (max 1,000 chars: plain-English criteria for when to act)
- `provider: Literal["llm", "typesafe", "openai_decisions"] = "llm"`
- `model: str | None = None` (required when `provider="llm"`; validated against `config.models`)
- `threshold: float = 0.8` (used by `typesafe` / `openai_decisions`; rejected for `llm` or defaulted cleanly)
- `timeout_seconds: float | None = None`

Execution:
- Builds a `JudgmentQuestion` + `build_judgment_request(...)` framing event text inside explicit untrusted-data boundaries.
- Calls `create_judgment_evaluator(...)` with `owner=f"reactive_trigger:{rule.trigger_id}"`, `question_id=f"reactive_trigger:{rule.trigger_id}"`.
- Strictly **tool-less** (no tool calls, no target mutation).
- **Fail-closed:** only `result.decision is True` triggers an action; `False`, `None`, timeout, missing key, or provider error drops silently without consuming `once=True`.

## Action & Delivery (`src/mindroom/reactive_triggers/dispatch.py`)

v1 supports one action: **`wake_agent`**.
- Target fields:
  - `agent: str` (defaults to current agent; non-admins may only target agents they are authorized to address in `target_room_id`)
  - `room_id: str` (defaults to current room; owner must be joined and authorized for `agent` in `room_id`)
  - `thread_id: str | None` (fixed thread root ID, `"origin"` for `room_message` to reply in the triggering message's thread/root, or `None` for room timeline)
  - `new_thread: bool = False` (mutually exclusive with fixed `thread_id` and `"origin"`)
  - `thread_key: str | None = None` (optional grouping key when `new_thread=True`, stored in SQLite)
  - `prompt: str` (owner's instruction to the woken agent, max 4,000 chars)
- Security checks at **both creation and fire time**:
  - `external_trigger_policy.enabled` and `reactive_enabled` are true.
  - Owner is human, allowed to talk to `target.agent` in `target.room_id` (`is_sender_allowed_for_agent_reply_in_room`), and currently joined to `target.room_id` (`is_external_trigger_owner_joined_target_room`).
  - Target bot is running and joined to `target.room_id`.
- Posted message format:
  - Mentions `target.agent` via `format_entity_mention`.
  - Contains the owner's `prompt` first, followed by a clearly delimited `<untrusted_event_data>` JSON/text block containing the event facts (never executing instructions from the event payload).
  - Stamps `ORIGINAL_SENDER_KEY = rule.owner_user_id` (enabling private/personal agents to resolve the owner's instance), `SOURCE_KIND_KEY = "reactive_trigger"`, `io.mindroom.reactive_trigger.id = rule.trigger_id`, `io.mindroom.reactive_trigger.dedupe_key = dedupe_key`.

## Chat Tool Interface (`triggers` toolkit)

Toolkit name: `triggers` (alias: `external_trigger_manager`).
Functions:
1. `watch_room(prompt, room_id=None, thread_id=None, senders=None, pattern=None, triage_instructions=None, triage_model=None, triage_provider="llm", triage_threshold=None, target_agent=None, target_room_id=None, target_thread_id=None, new_thread=False, once=False, max_fires_per_hour=10, cooldown_seconds=0, expires_in_hours=None, trigger_id=None, description="")` — when `new_thread=False` and `target_thread_id is None`, defaults to `"origin"` if targeting the watched room (replying in the triggering message's thread/root), or room timeline if targeting another room; explicit `target_thread_id` + `new_thread=True` is rejected.
2. `watch_thread(prompt, tag="resolved", transition="added", room_id=None, thread_id=None, target_agent=None, target_room_id=None, target_thread_id=None, new_thread=False, once=True, max_fires_per_hour=10, cooldown_seconds=0, expires_in_hours=None, trigger_id=None, description="")`
3. `subscribe_event(source_id, kind, prompt, match=None, triage_instructions=None, triage_model=None, triage_provider="llm", triage_threshold=None, target_agent=None, target_room_id=None, target_thread_id=None, new_thread=False, once=True, max_fires_per_hour=10, cooldown_seconds=0, expires_in_hours=None, trigger_id=None, description="")`
4. `create_trigger(...)` — existing direct Ed25519 webhook trigger (unchanged signature).
5. `list_triggers(include_disabled=True)` — returns unified list of direct external triggers + reactive triggers (scoped to requester, or all for admins).
6. `disable_trigger(trigger_id, enabled=False)` — toggles either direct or reactive trigger.
7. `delete_trigger(trigger_id)` — deletes either direct or reactive trigger.
8. `rotate_trigger_key(trigger_id, public_key, key_id="default")` — rotates key on a direct Ed25519 trigger.

## Global Constraints
- ~95% isolated in `src/mindroom/reactive_triggers/`; zero edits to reply turn loop, ingress, coalescing, streaming, or compaction.
- All new internal imports declared in `tach.toml` (`tach check --dependencies --interfaces` must pass).
- Existing `external_trigger_manager` configs, Python imports, and `/api/triggers/{trigger_id}` API stay 100% backward-compatible.
- Never leak secrets in logs, errors, tool responses, or SQLite records (`secret_env` stores only env var name).
- Every public/internal event text treated as untrusted data; triage is strictly tool-less and fail-closed.
- Per-owner quotas enforced across creation (`max_reactive_triggers_per_owner`, default 50).

## Testing Strategy
- Unit tests for SQLite store (schema init, cursor persistence, thread checkpoints, `UNIQUE(trigger_id, dedupe_key)`, atomic `once` disable, `triaged_no` preserving `once`, hourly rate limits, cooldown, expiry, pruning).
- Unit tests for deterministic matching + GitHub HMAC-SHA256 verification + GitHub payload normalization (PR merged vs closed-unmerged, wrong signature, missing delivery header, repo/subscriber grant enforcement).
- Unit tests for tool-less cheap triage (`llm`, `typesafe`, `openai_decisions`, fail-closed on False/None/timeout/error).
- Integration tests for all 3 sources end-to-end:
  1. `room_message` watch → cheap triage drops noise, fires on hit, skips bot/rule-sent messages (loop guard), advances cursor across restart.
  2. `thread_tag` watch → live `resolved` transition wakes target thread B once; offline resolution caught on `restore_room` without double-fire; already-resolved thread does not misfire on creation.
  3. `external_event` GitHub PR merge → `POST /api/event-sources/github` fans out to matching PR rule only, deduplicates repeated delivery, consumes `once=True`.
- Tool tests for unified `triggers` toolkit + `external_trigger_manager` alias backward compatibility + `tach` dependency check.
