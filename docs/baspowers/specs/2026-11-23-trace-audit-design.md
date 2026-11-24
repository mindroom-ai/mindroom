# Passive Trace Auditor, Friction-Triggered Skill Learning & Real-Data Backtest

## Problem & Intent

MindRoom's existing `skill_learning` triggers blindly every `review_interval` (default 10) model replies using an in-memory counter. On 30 days of real production data (a personal-assistant agent, 233 active sessions, 732 runs, 8,299 tool calls):
- `skill_learning` ran **115 reviews** across only **33 sessions** (31.3M input tokens, 103 skill history edits), with **37 reviews (32%)** firing on zero-friction sessions.
- Meanwhile, **62 sessions** had concrete friction (53 with repeated same-tool errors), and `skill_learning` **missed 44 of 62 (71%)** and **39 of 53 repeat-error sessions (74%)** because short threads never reached 10 replies, failed runs do not count, and restarts wipe in-memory counts.
- Only 36 tool calls set `tool_call_error=True`; 208+ additional failures returned `tool_call_error=False` with non-zero shell exit codes, JSON `status: "error"`, or anchored `Error:`/`Traceback` messages.

**Goal:** Build a passive trace auditor that detects real friction at zero LLM cost, confirms teachable failures via a bounded tool-free judge, clusters recurring failures across distinct sessions within the same agent/room scope, and promotes confirmed clusters to MindRoom's existing `skill_learning` writer — plus an offline read-only chronological backtest CLI to measure coverage, cost, and held-out skill applicability on real session snapshots.

**Non-goals:**
- No weight training / RL inside MindRoom.
- No live tool-mocking replay of side-effecting turns.
- No cross-room or cross-user evidence pooling into shared skills.
- No breaking changes to existing `skill_learning: {enabled: true}` configs (default trigger remains `"interval"`).

## Architecture: One Pure Core, Three Callers

All detection, reading, auditing, clustering, and promotion logic lives in one new package, **`src/mindroom/trace_audit/`**, consumed identically by:
1. **Scheduled automation** — built-in `@automation("trace_audit", requires_file_memory=False)` (`src/mindroom/automations/trace_audit.py`), reusing `AutomationRunner` cron, timezone, private-instance enumeration, room resolution, membership checks, and per-instance concurrency lock.
2. **Existing skill writer** — `SkillReviewRunner.review_audit_cluster(...)` + bounded evidence-only review in `src/mindroom/skill_learning/reviewer.py`, preserving `SkillTools`, `library_turn` locking, learner-only ownership, read-before-write, `.history/` backups, and `kind="skill_learning"` usage attribution.
3. **Offline backtest CLI** — `evals/trace_audit/` running the exact same core against a read-only SQLite backup copy and scratch skills copy, never touching production data or telemetry.

### Passive Automation Execution (`Run` Step)
Today `AutomationRunner._fire` expects `Ask | None` and wakes an unrestricted `@agent` chat turn. For `trace_audit`, waking chat is both unsafe (exposes shell/web tools and unrestricted chat `skill_manage` to trace content) and wasteful (reloads full agent prompts).

Extend `src/mindroom/automations/steps.py` with a built-in-only `Run` step:
```python
@dataclass(frozen=True)
class Run:
    """Async passive work run on the main loop under the automation's instance lock."""
    execute: Callable[[], Awaitable[Done | None]]
```
`AutomationRunner._fire` rejects `Run` from plugin automations. For built-in automations returning `Run`, `_fire` awaits `step.execute()` under the existing instance guard (releasing it in `finally`) and posts `done.notice` via `_notify` only if `done` is returned with non-empty `notice`.

## Stage 1: Deterministic Friction Detector (`trace_audit/signals.py`, 0 LLM Cost)

Pure module with zero SQLite, config, or LLM imports.

### Tool-Outcome Classifier
Classify each `ToolExecution` once, in priority order:
1. `tool_call_error` (`is_error=True`, excluding `confirmed is False`) -> `kind="tool_error"`.
2. `confirmed is False` -> `kind="approval_denied"` (tracked separately; not treated as a tool bug).
3. Shell timeout: first line matches `^Stopped after \d+(?:\.\d+)?s\.` -> `kind="tool_timeout"`.
4. Shell non-zero exit: first line matches `^Exit code (\d+)` with `N != 0` -> `kind="nonzero_exit"`, subject to **benign exit-1 suppression**:
   - Parse the command string with `shlex.split` (never executing). If `N == 1`, `Stderr:` is absent/empty, and the simple command (no unguarded compound failure) is a known no-match/predicate utility (`grep`, `rg`, `ag`, `ack`, `diff`, `cmp`, `test`, `[`, `[[`, `pgrep`, `which`, `command`, or `git diff` / `git grep`), classify as `benign_exit_1` (not a failure).
5. Top-level JSON error (bounded `<= 65_536` chars, result starts with `{`): `status == "error"`, `mindroom_tool_output.status == "error"`, `view_status == "error"`, `ok is False`, or non-empty string `errcode` -> `kind="json_error"`.
6. Anchored text error: ONLY for non-reader tools (excluding `read_file`, `grep`, `find_files`, `ls`, `read_url`, `scrape_webpage`, `search_knowledge_base`, `search_memories`, `get_skill_instructions`, `get_skill_reference`, `get_skill_script`, `matrix_message`, `matrix_room`, `view_file`, `get_attachment`), first line matches `^(?:Error|ERROR|Traceback \(most recent call last\)|Exception|FATAL):` -> `kind="text_error"`.

Each classified failure produces a stable signature `(tool_name, operation, kind, diagnostic_family)` where `operation` is an allowlisted subcommand/action (e.g. `git:push`, `pytest`, `uv`, `curl`, `edit_file`, or `""`) and `diagnostic_family` normalizes paths, UUIDs, numbers, and hashes into coarse buckets (e.g. `not_found`, `permission`, `timeout`, `syntax`, `rate_limit`, `exit_<code>`, `other`).

### Session Incident Rules
Evaluate only settled sessions (`now - session.last_updated_at >= min_idle_seconds`, default 600s) over each new terminal top-level run plus up to 2 preceding eligible runs in the same session within 24h:
- **Material failure (eligible for Stage 2 immediately):**
  - `run_error`: `status == ERROR` or non-empty `run.error`.
  - `failing_tools`: `run.failing_tools` non-empty with failure count > 0.
  - `repeat_tool_error`: `>= 2` classified failures on the same `(tool_name, operation)`.
  - `multi_tool_error`: `>= 2` classified tool failures in the window.
  - `tool_loop`: `>= 3` consecutive calls with identical `(tool_name, normalized_args_digest)` and identical failure/error outcome (excluding pagination/offset changes).
- **Weak signals (eligible for Stage 2 only when `>= 2` distinct weak channels fire in the window):**
  - `single_tool_error`: 1 classified tool failure.
  - `settled_cancel`: `status == CANCELLED` with `>= 1` recorded tool call (or duration `> 15s`) and no unresolved interrupted-call checkpoint placeholder.
  - `user_correction`: next observed human turn within 10 minutes and `<= 400` chars (after stripping Matrix metadata tags) starting with an explicit correction cue (EN/NL: `no,`, `wrong`, `incorrect`, `you didn't`, `you forgot`, `stop `, `don't `, `remember to`, `nee,`, `verkeerd`, `niet zo`), excluding code blocks and quotes.
  - `tool_outlier`: `>= max(15, 3 * median_tools)` tool calls AND `>= 1` classified failure (when `>= 20` baseline runs exist; otherwise `>= 20` calls AND `>= 1` failure).
- **Clean-run sample:** Deterministic salted hash of `run_id` selects `sample_rate` (default `0.02`, capped at `2` per tick) of zero-signal completed runs (`>= 1` tool call or `>= 200`-char reply) to detect silent failures (e.g., unsupported completion claims).

## Read-Only Source Layer (`trace_audit/source.py`)

- Opens the agent's session SQLite database strictly read-only (`file:<path>?mode=ro`, `PRAGMA query_only = ON`, `timeout=5.0`) using `session_storage.schema.TableNames`. Never calls `create_session_storage` (which creates/migrates).
- Reads terminal top-level `kind='agent'` runs (`status IN ('COMPLETED', 'ERROR', 'CANCELLED')`, `parent_run_id IS NULL`) ordered by `seq` within `session_id`, joining `_log_tools` and `_log_messages` in `ord` order, plus `_log_removed` run IDs and `_usage` snapshots.
- **Exclusions (no self-audit or cross-scope leakage):**
  - Skip `:dynamic_workflow:` sessions.
  - Skip runs with `HOOK_SOURCE_RUN_METADATA_KEY` starting with `"automation/"`.
  - Skip sessions in `automation_threads` or contact sessions (`contact_session_check`) when provided by the caller.
  - Filter sessions to the target scope (`room_id` when present in session/run metadata or session ID `<room_id>:<thread_id>`) so shared workspaces never pool private rooms into a shared skill.

## Stage 2: Passive Auditor, Clustering & Promotion (`trace_audit/auditor.py`, `trace_audit/pipeline.py`)

### Bounded Evidence Card
Never send full transcripts or raw tool outputs to the auditor. Build a bounded `EvidenceCard` (total `<= 4_000` chars) passed through `redact_sensitive_text`:
- Header: incident reasons, run status, loaded skill names (`get_skill_instructions` args), tool count.
- Typed failure sequence: up to 6 entries of `(ord, tool_name, operation, kind, diagnostic_family, truncated_error <= 240 chars)` plus recovery indicator if a later call to the same `(tool_name, operation)` succeeded.
- Up to 4 text snippets (`<= 280` chars each): triggering user ask, final assistant reply excerpt, and (if present) user correction turn.

### Tool-Free Structured Audit Call
One tool-free JSON completion per selected incident using `entry.model` -> `cheap_model` -> `skill_learning.model` -> agent model (or `--audit-model` in backtest). Records usage under `kind="trace_audit"` when a usage sink is provided.

Returns `AuditFinding`:
- `verdict`: `"actionable" | "transient_env" | "benign" | "unverified_claim" | "no_issue"`
- `teachable`: `bool` (True only if a reusable procedural rule would prevent the mistake on future tasks)
- `category`: `"tool_misuse" | "verification_gap" | "retry_loop" | "user_preference" | "skill_bug" | "other"`
- `remedy`: small enum (`"verify_outcome" | "validate_input" | "change_retry_strategy" | "follow_user_constraint" | "fix_skill" | "use_correct_tool" | "none"`)
- `root_cause`: `<= 240` chars
- `proposed_rule`: `<= 280` chars (generalized procedural check; no task-specific literals)
- `has_verified_fix`: `bool`
- `target_skill`: optional existing skill name

### Durable State, Clustering & Anti-Churn Promotion
Persist rolling 30-day metadata in `state_dir / "trace_audit.json"` via `write_json_file_durable` (content-free: cursors `session_cursors: {sid: last_seq}`, `findings` metadata list capped at 300 entries, `cooldowns: {cluster_key: epoch_ts}`, `metrics` counters — zero raw transcripts or tool arguments).

- **Cluster key:** `(scope_key, tool_name, operation, kind, remedy)` for tool failures, or `(scope_key, category, remedy, target_skill)` for non-tool/unverified-claim failures.
- **Promotion gate:** Promote a cluster to `skill_learning` ONLY when:
  1. `verdict in {"actionable", "unverified_claim"}` and `teachable is True`,
  2. `distinct_sessions >= min_cluster_sessions` (default `2`) within the 30-day window (never two failures in one loop),
  3. Cluster key not promoted within `cooldown_days` (default `14`),
  4. Under `max_nominations_per_run` (default `1`) and `max_audits_per_run` (default `12`, reserving 2 for clean samples).
- Record the cooldown/attempt marker *before* invoking the learner so a crash never causes blind retry loops.

## `skill_learning` Integration & Config

- **`config/skill_learning.py`**: Add `trigger: Literal["interval", "friction"] = "interval"`.
  - `"interval"` (default): exact existing behavior (`review_interval: 10`, in-memory counter, `_fork()`/`_replay()`).
  - `"friction"`: `response_runner.py` skips per-reply capture and counting completely (zero live hot-path work); `trace_audit` automation hands promoted clusters to `SkillReviewRunner.review_audit_cluster(...)`.
  - Validate in `config/main.py` that `skill_learning.trigger == "friction"` requires a `trace_audit` entry in that agent's effective automations.
- **`config/automations.py`**: Add `TraceAuditAutomation(_ScheduledAutomation)`:
  - `name: Literal["trace_audit"] = "trace_audit"`
  - `requires_file_memory: ClassVar[bool] = False`
  - `cron: str = "0 * * * *"`
  - `min_idle_seconds: int = 600`
  - `sample_rate: float = 0.02`
  - `max_audits_per_run: int = 12`
  - `min_cluster_sessions: int = 2`
  - `cooldown_days: int = 14`
  - `max_nominations_per_run: int = 1`
  - `notify: bool = True`
- **`skill_learning/reviewer.py`**: Add `review_audit_cluster(...)`:
  - Uses an **evidence-only bounded request** (never `_fork()`, never `_replay()` of full conversation history).
  - Caps at **4 tool calls** and **16,000 cumulative input tokens**.
  - Provides only the generalized cluster dossier (coded failure pattern, remedy, distinct session count, generalized rules) + skill catalog.
  - Uses a conservative prompt: patch an existing learner-owned skill first, create a new learner skill only if none fits, and respond `"Nothing to save."` if guidance already exists or is too narrow.
  - Reuses `SkillTools` (learner-only, read-before-write, `library_turn` lock, `archive_unused_skills`, `.history/` backups, `record_run_usage(kind="skill_learning")`).
- **`usage/storage.py`**: Add `"trace_audit"` to `IndependentUsageKind`.
- **Report-only mode:** When `trace_audit` automation is enabled on an agent whose `skill_learning.trigger != "friction"` (or `skill_learning.enabled == False`), `trace_audit` runs discovery, clustering, and posts a concise `Done` summary notice (if `notify=True` and actionable clusters exist) without modifying any skills.

## Offline Real-Data Backtest (`evals/trace_audit/`)

CLI patterned on `evals/memory_recall/`:
```sh
uv run python -m evals.trace_audit.run \
  --db SNAPSHOT.db \
  --skills-dir SKILLS_DIR \
  --config CONFIG.yaml \
  --window-days 30 \
  --train-days 20 \
  --audit-model MODEL_A \
  --judge-model MODEL_B \
  --out OUTPUT_DIR \
  [--dry-run]
```

1. **Strict safety:** Opens `--db` strictly read-only (`mode=ro`, `PRAGMA query_only = ON`), copies `--skills-dir` into `--out/scratch_skills/`, never writes to production DBs/workspaces/telemetry, writes private artifacts with `0600` permissions, and writes an aggregate-only `report.md` safe to share.
2. **Two baselines:**
   - **Baseline A (observed):** historical `kind="skill_learning"` rows in `_usage`, priced with `budgets.pricing.cost_usd`.
   - **Baseline B (simulated `interval: 10`):** chronological replay of `count_model_replies` on eligible completed runs.
3. **Chronological holdout (no session leakage):**
   - Partition sessions by `first_run.created_at`: Train (`days 1..train_days`) vs Holdout (`days train_days+1..window_days`).
   - Run exact production `trace_audit` pipeline chronologically on Train -> clusters + scratch skill patches (via real bounded `review_audit_cluster`) -> freeze patches at cutoff.
   - Independently audit Holdout sessions (unseen sessions separated from continuing sessions) -> measure recurrence of trained clusters.
   - **Blinded independent judge (`--judge-model`, different family from `--audit-model`)** scores each held-out teachable failure against randomly ordered `(proposed_patch, original_or_noop, placebo_unrelated_skill)` on a `direct / partial / none / harmful` rubric, reporting direct-hit rate vs placebo control, token/USD cost comparison via `budgets.pricing.cost_usd`, and patch churn vs historical baseline.
4. **`--dry-run` mode (0 LLM cost):** Runs Stage 1 + Baseline A + Baseline B deterministically over the full window for instant signal/cost diagnostics before spending LLM tokens.
