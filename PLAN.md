## FINAL PLAN

### 1. Structure

- **`src/mindroom/custom_tools/todo_state.py`** (new leaf module): extract from `todo.py` the storage + actionability primitives — state root/thread paths (`todo.py:208-214`), locked JSON read/update (`todo.py:185-205`), `_TERMINAL_STATUSES`, `is_actionable`/`is_blocked` (`todo.py:228-241`). `todo.py` imports these with zero behavior change. Rationale: `todo.py` module-imports Agno/Jinja/Pydantic; the scanner must not drag those into orchestrator wiring.
- **`src/mindroom/custom_tools/todo_poke.py`** (new): frozen slotted `TodoPokePolicy` (`interval_seconds=120`, `cooldown_seconds=300`, `quiet_seconds=300`, `max_pokes_per_scan=3`), a `TodoPokeDeps` dataclass of injected callables (state root, schedule query, idle check, sender, clock), pure scan logic, and `TodoPokeWorker` (asyncio loop).
- **`src/mindroom/scheduling.py`**: new typed helper `get_pending_schedule_thread_ids_for_room(...)` reusing `_parse_scheduled_task_record` (`scheduling.py:307-331`); include only `pending` schedules and **exclude `new_thread=True`** records (they store `thread_id=None` per `scheduling.py:1354` but wake a NEW thread, so they must not suppress room-main pokes).

### 2. Drive and wiring

- Orchestrator-owned worker mirroring `MemoryAutoFlushWorker` (`orchestrator.py:282-315`, sync from `_sync_runtime_support_services` at `orchestrator.py:582`, stop on shutdown at the same call sites as the memory worker). NOT `schedule:fired` (cannot wake a thread with no schedule — that is the bug) and NOT hook registry (plugin-only, `hooks/registry.py:47-110`).
- **Sleep-first loop**: wait one interval before the first scan (plugin parity). Do NOT scan immediately at startup — the first `_sync_runtime_support_services` runs before the router bot exists (`orchestrator.py:1122` vs `1045-1060`). Loop uses a stop event with timeout for prompt shutdown; a failed scan logs and continues.
- **Env override**: `MINDROOM_TODO_POKE_INTERVAL_SECONDS` via `runtime_paths.env_value` (precedent `constants.py:848-856`); `0` disables the worker. No YAML config surface.

### 3. Scan algorithm (per tick)

1. Enumerate `storage_root/todo/threads/*/todos.json` in sorted order; skip malformed files with a structured warning and continue.
2. Parse into frozen snapshots; normalize persisted `"main"` thread sentinel to `None`; isolate invalid or duplicate items while keeping their dependents blocked.
3. Group actionable items (native rule: status `open` + all deps terminal) by nonempty, safe-identifier `assigned_agent`; ignore unassigned, malformed, or no-longer-configured agents.
4. **Quiet gate**: skip scope unless `now - max(updated_at of that agent's actionable items) >= quiet_seconds` (data already in todos.json; restores "don't barge in right after activity" without activity ledgers).
   Intentionally use the actionable item's timestamp, so an older item that becomes unblocked by a newly completed dependency is immediately eligible for handoff.
5. **Idle check**: agent's direct bot exists, is running, `in_flight_response_count == 0` (`response_runner.py:448-458`, exposed at `bot.py:712-720`) AND every running configured team bot containing the member (`config/agent.py:382-426`) also has zero in-flight. Recheck idle immediately before send.
6. **Schedule suppression**: query pending schedules for the room once per room via the injected querier; if the todo scope has a pending schedule (after `new_thread` exclusion) → skip. **Failure posture**: sender/querier not yet available (startup) → skip the whole tick; query executed but errored → log warning and treat as no pending schedules (fail-open — a persistent read failure must not silently disable anti-stall forever; worst case is one redundant poke bounded by dedup).
7. **Dedup + cooldown** in `storage_root/todo/poke_state.json` (same lock/atomic-replace discipline): key = canonical tuple of `(assigned_agent, room_id, normalized_thread_id)` (nested mapping or hashed tuple — no string concatenation of raw IDs); store `last_poked_at` + `last_fingerprint`. Fingerprint = canonical serialization of ALL actionable items for that agent in scope (id/title/priority/depends_on/assigned_agent/updated_at) + thread total/terminal counts. Unchanged fingerprint → never re-poke; changed fingerprint → only after cooldown.
8. **Send**: native todo poke message ("Todo work is ready" style, list up to 5 items with priorities) from the router into the stored room/thread with exactly one explicit assignee mention, literal non-mention todo titles, `trigger_dispatch=True`, and `extra_content={ORIGINAL_SENDER_KEY: mindroom_user_id(config, runtime_paths)}` when non-None (parity with `hooks/context.py:83-113`, `811-830`). Router sender returns `None` on failure (`bot.py:2045-2076`): persist fingerprint/timestamp ONLY on a non-None event ID so failed sends stay retryable.
9. Stop after `max_pokes_per_scan` delivery attempts; deterministic iteration order, with worker-memory failed scope keys ordered behind fresh scopes on later scans so repeated failures cannot starve healthy work.

### 4. Scope item 2 (`assigned_agent` defaults)

Already on main since `7be4d90af` (#1337): `plan` and `apply_template` set `_default_assignee` (`todo.py:637-643,670,707,935,957`) with tests. **Do NOT add a `plan`-level `assigned_agent` parameter** (per-item override already exists in `add_todo` and templates; a plan-level param is new ambiguous API surface). Deliverable: regression tests locking the default + template-explicit-override precedence, and a PR-body note that main already fixed this half.

### 5. Cleanup + docs

- `~/.mindroom-chat/plugins/workloop/` is runtime state, not repo state: note as deploy-time cleanup in PLAN.md/report; do not touch it from the repo.
- Router-triggered todo pokes assume the current single-user deployment trust model, where the configured internal MindRoom user is the authorized automation requester; multi-user requester ownership is outside this issue.
- Update the CLAUDE.md module table for `todo_state.py` + `todo_poke.py`.

### 6. Tests

Unit: malformed file and invalid-item skip-and-continue; duplicate IDs and timezone-naive timestamps; dependencies on skipped items remain blocked; unassigned, unsafe, or unconfigured assignee skip; quiet gate and immediate newly-unblocked handoff; direct-busy and team-busy skip; pre-send idle and state recheck races; schedule suppression incl. `new_thread` exclusion and both failure postures; fingerprint dedup (hidden 6th item change re-arms; unchanged never re-pokes or queries schedules); cooldown ordering; three-send-attempt cap with failed-scope fairness and no failed-send persistence; poke-state recovery and pruning; once-per-room schedule query; orchestrator lifecycle (start/reload/restart/stop); env override incl. `0` disables and invalid values; scheduling helper parsing. Regression tests for scope item 2 as above.

Gates: `uv run pytest`, `uv run pre-commit run --all-files`, `uv run tach check --dependencies --interfaces`.

### 7. Live test (later phase, design for it now)

With `MINDROOM_TODO_POKE_INTERVAL_SECONDS=10` (+ quiet override via same env pattern if needed — make quiet_seconds env-overridable too: `MINDROOM_TODO_POKE_QUIET_SECONDS`):
(a) idle agent + unblocked assigned todo + no pending schedule → poke fires, agent visibly replies;
(b) same thread WITH a pending schedule → NO poke for 2+ intervals;
(c) cancel the schedule → next eligible scan pokes (proves suppression, not a dead scanner).
