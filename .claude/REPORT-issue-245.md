# ISSUE-245 Implementation Report

## Summary

MindRoom now has a built-in todo auto-poke scanner that wakes an idle configured agent when assigned native todo work is actionable and no pending schedule already owns that room/thread scope.

The implementation uses an orchestrator-owned sleep-first worker and does not depend on plugin hooks or `schedule:fired`.

## What changed

- Added `src/mindroom/custom_tools/todo_state.py` as the leaf owner for todo paths, locked JSON access, atomic replacement, terminal statuses, and actionability rules.
- Updated `src/mindroom/custom_tools/todo.py` to consume the extracted primitives without changing tool behavior.
- Added `src/mindroom/custom_tools/todo_poke.py` with typed frozen snapshots, `TodoPokePolicy`, injected dependencies, deterministic scan logic, durable fingerprint/cooldown state, and the sleep-first worker.
- Added `get_pending_schedule_thread_ids_for_room` in `src/mindroom/scheduling.py`, including only pending existing-scope schedules and excluding `new_thread=True`.
- Wired worker start, config-reload reuse, environment disablement, and shutdown into the orchestrator runtime-support lifecycle.
- Added router delivery with an explicit assignee mention, `trigger_dispatch=True`, and the internal MindRoom requester identity when available.
- Hardened template persistence so literal or rendered whitespace-only todo titles are rejected before state is written.
- Documented native auto-poke behavior and environment controls in the project-management guide, and marked the external workloop plugin as legacy for this workflow.
- Regenerated the tracked MindRoom docs-skill references for those documentation changes.
- Updated `tach.toml` for the new orchestrator dependencies and documented both new modules in `CLAUDE.md`.

## Scanner behavior

- Todo files are scanned in sorted order, malformed files are warned about and skipped independently, and invalid individual items—including timezone-overflowing timestamps—no longer discard valid siblings.
- Dependents of skipped, duplicate, or missing dependency identities remain blocked instead of becoming prematurely actionable.
- Directory traversal, JSON access, advisory locking, revalidation reads, pruning, and persistence run in worker threads rather than blocking the asyncio event loop.
- Persisted `main` thread sentinels normalize to room-main `None`.
- Only open, dependency-unblocked items with a nonempty configured direct-agent assignee can produce a poke.
- The quiet gate uses the newest `updated_at` among that assignee's actionable items in the scope, while treating timestamps beyond one quiet window in the future as already quiet so clock skew cannot wedge a scope.
- An older item that becomes actionable after a dependency completes remains immediately eligible, which is the intended handoff behavior.
- Direct-agent activity and activity in every running configured team containing that agent suppress the poke, with a second idle check immediately before delivery.
- Poke state is consulted before schedule queries, so dedup-blocked scopes cause no Matrix schedule reads.
- Pending schedules are queried once per remaining room and suppress only their existing room/thread scope.
- Runtime unavailability skips the whole tick, while an executed schedule query that errors fails open with a warning.
- Each scope is re-read and its fingerprint recomputed after schedule I/O, so work completed, reassigned, or otherwise changed during the await is not poked from a stale snapshot.
- Durable scope keys hash the canonical `(assigned_agent, room_id, normalized_thread_id)` tuple.
- Fingerprints include every actionable item, including items beyond the five shown in the message, plus thread total and terminal counts.
- An unchanged fingerprint receives at most three one-hour anti-stall retries, while a changed fingerprint waits for the normal cooldown and resets that retry count.
- Poke messages contain exactly one intentional assignee mention, while todo titles are rendered as literal code text with mention tokens neutralized.
- Persisted assignees must match the same alphanumeric-and-underscore identifier shape as configured entities before they can reach idle checks or mention formatting.
- The per-scan cap counts send attempts, including failures, and failed sends persist a consecutive counter that permits the initial attempt plus three retries for one unchanged fingerprint.
- A successful delivery suppresses further scopes for the same agent during that scan, so one tick cannot enqueue multiple new turns for one idle agent.
- Successful deliveries enter worker memory before durable persistence, so a per-scope write failure neither repeats immediately nor prevents later scopes, and later ticks retry the write.
- The worker remembers failed scope keys in memory and orders them behind fresh scopes on later scans, preventing deterministic failures from starving healthy work.
- Obsolete poke records are pruned when their actionable assignee/room/thread scope no longer exists, but a transient todo-file I/O failure conservatively defers pruning for that tick.
- Non-object, malformed JSON, non-UTF-8, non-finite timestamp, boolean-timestamp, and invalid-counter poke state is warned about or treated as an absent record and repaired through locked atomic persistence.
- A transient `poke_state.json` I/O failure skips the tick instead of discarding durable dedup state, while content corruption retains the existing recovery path.
- Enabled scan intervals below one second are rejected in favor of the default, while `0` still disables the worker and subsecond quiet periods remain valid.

## Round-1 review disposition

The requester-ownership proposal was intentionally not implemented because this deployment uses the single-user trust model: router-triggered automation carries the configured internal MindRoom user, matching the existing trusted automation path.
Designing multi-user todo ownership and authorization is a separate product and security change outside ISSUE-245.

`PLAN.md` and this report remain intentionally committed as review artifacts for the branch and will be stripped during squash merge, so the artifact-removal finding required no code change.

## Round-2 review disposition

Requester ownership remains intentionally scoped to the documented single-user trust model, so the repeated multi-tenant authority proposal was not implemented.
Router and assignee membership checks were not added because todo state is created through the native tool in rooms where those configured entities operate, and the bounded misconfiguration case does not justify membership-query machinery.
Schedule reads deliberately remain fail-open because fail-closed behavior can silently disable anti-stall indefinitely, while dedup bounds the redundant-poke risk.
Ad-hoc team activity is a known idle-check limitation because only configured direct and team bots expose the required in-flight counts; a new execution-wide activity registry is outside this issue.
The committed plan and report remain intentional review artifacts that safe squash merge removes.

## Round-3 review disposition

Locked state recovery now handles non-UTF-8 bytes, each scan delivers at most one successful poke per assigned agent, and unchanged fingerprints become eligible for up to three one-hour anti-stall retries.
The scanner's stricter dependency rule is now documented at its ownership boundary, and enabled subsecond scan intervals fall back to the default.

Corrupt-state warnings remain intentionally unsuppressed because persistent state corruption should stay visible until a successful atomic repair, and adding warning rate-limit state is unnecessary for this bounded recovery path.
File-lock timeouts remain intentionally out of scope because todo state uses the repository's shared advisory-lock discipline, and introducing a scanner-only timeout contract would add inconsistent lock machinery without evidence of a deadlock.
The repeated round-three findings about requester identity, room membership, fail-open schedule reads, ad-hoc team activity, and review artifacts remain covered by the round-one and round-two dispositions above.

## Round-4 review disposition

Timestamp overflow is normalized into per-item malformed-state isolation, dedup records reject non-finite and boolean timestamps, and unchanged retries now stop after three attempts until the fingerprint changes.
Successful deliveries are remembered before persistence and repaired on later ticks, while transient source I/O prevents pruning durable records that might still be active.
The schedule helper now documents its `RuntimeError` read-failure contract, todo priority ordering has one leaf-module source of truth, the duplicated safe-name regexes cross-reference each other, and literal backtick fence behavior has parameterized coverage.

The standing requester-identity, room-membership, fail-open schedule-read, ad-hoc team-registry, and review-artifact proposals remain dropped for the reasons documented above.
Shutdown cancellation machinery also remains dropped because this worker intentionally matches the established `MemoryAutoFlushWorker` stop-event lifecycle.

## Round-5 review disposition

Future actionable timestamps no longer wedge the quiet gate, transient dedup-state read failures skip delivery for that tick, and consecutive failed sends now have the same bounded-retry posture as successful unchanged-fingerprint retries.
Both persisted retry counters reject boolean, negative, non-integer, and out-of-range values, and focused tests now cover unavailable idle, sender, and schedule-query orchestrator adapters.

Reviewer A's requester-identity, fail-open schedule-read, and room-membership findings remain dropped under the standing round-one and round-two decisions.
Reviewer A's idle-lifecycle proposal also remains dropped because the in-flight-count check plus immediate pre-send recheck is the decided bounded-harm design, and a lifecycle-wide activity predicate would add the orchestrator surface previously rejected as over-engineering.
Reviewer B's scan-state plumbing suggestion is a style preference without a correctness benefit at this boundary.
The plan and report remain intentional branch artifacts for safe squash-merge removal.

## Assigned-agent defaults

The `plan` and `apply_template` default-assignee behavior was already present on main from `7be4d90af`.

This change intentionally does not add a plan-level `assigned_agent` parameter.

Regression coverage now locks plan defaults, template defaults, and explicit template-assignee precedence.

## Validation

- `env -u MINDROOM_OWNER_USER_ID -u MINDROOM_DOCKER_WORKER_IMAGE -u MINDROOM_CONFIG_PATH -u MINDROOM_STORAGE_PATH uv run pytest -n auto --no-cov`: 10,749 passed and 120 skipped.
- `uv run pre-commit run --all-files`: passed.
- `uv run tach check --dependencies --interfaces`: passed.
- Focused round-2 todo-poke, orchestrator, and scheduling suites: 92 passed.
- Focused round-3 todo-poke scanner suite: 38 passed.
- Focused round-3 todo state, scanner, and orchestrator suites: 67 passed.
- Focused round-4 todo-poke and scheduling suites: 103 passed.
- Focused round-4 todo state, scanner, orchestrator, and scheduling suites: 132 passed.
- Focused round-5 todo-poke scanner and orchestrator suites: 66 passed.
- Changed-file `ty` validation: passed without rule overrides.

The first all-files hook pass regenerated three tracked docs-skill reference outputs, and the required second pass was clean.

One round-2 full-suite run hit an unrelated stochastic callback test because its random token contained the literal substring `jq`; the isolated test and the complete rerun both passed.

The first unsanitized pytest run exposed four unrelated failures because the shell exported owner and Docker image values that overrode isolated test fixtures.

Removing those two ambient values made the four tests and the complete suite pass.

On this Linux worktree, the all-files `ty` hook also needed local non-repository empty import stubs for the existing Darwin-only `AppKit`, `ApplicationServices`, and `Quartz` modules.

Without those local stubs, only untouched desktop modules fail import resolution, while all changed files type-check normally.

## Deployment and live-test notes

`~/.mindroom-chat/plugins/workloop/` is runtime state and should be removed as a deploy-time cleanup after the native worker is rolled out.

No repository code touches or removes that directory.

The later live test can use `MINDROOM_TODO_POKE_INTERVAL_SECONDS=10` and `MINDROOM_TODO_POKE_QUIET_SECONDS=0` to verify poke delivery, pending-schedule suppression for two intervals, and delivery after schedule cancellation.
