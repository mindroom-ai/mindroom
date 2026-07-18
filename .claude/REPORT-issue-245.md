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

- Todo files are scanned in sorted order, malformed files are warned about and skipped independently, and invalid individual items no longer discard valid siblings.
- Directory traversal, JSON access, advisory locking, revalidation reads, pruning, and persistence run in worker threads rather than blocking the asyncio event loop.
- Persisted `main` thread sentinels normalize to room-main `None`.
- Only open, dependency-unblocked items with a nonempty configured direct-agent assignee can produce a poke.
- The quiet gate uses the newest `updated_at` among that assignee's actionable items in the scope.
- An older item that becomes actionable after a dependency completes remains immediately eligible, which is the intended handoff behavior.
- Direct-agent activity and activity in every running configured team containing that agent suppress the poke, with a second idle check immediately before delivery.
- Poke state is loaded first, so unchanged and cooldown-blocked scopes cause no schedule queries.
- Pending schedules are queried once per remaining room and suppress only their existing room/thread scope.
- Runtime unavailability skips the whole tick, while an executed schedule query that errors fails open with a warning.
- Each scope is re-read and its fingerprint recomputed after schedule I/O, so work completed, reassigned, or otherwise changed during the await is not poked from a stale snapshot.
- Durable scope keys hash the canonical `(assigned_agent, room_id, normalized_thread_id)` tuple.
- Fingerprints include every actionable item, including items beyond the five shown in the message, plus thread total and terminal counts.
- An unchanged fingerprint never repeats, while a changed fingerprint waits for the cooldown.
- Poke messages contain exactly one intentional assignee mention, while todo titles are rendered as literal code text with mention tokens neutralized.
- The per-scan cap counts send attempts, including failures, while failed sends remain retryable because they do not persist poke state.
- Obsolete poke records are pruned when their actionable assignee/room/thread scope no longer exists.
- Non-object poke-state roots and non-object `scopes` values are warned about, treated as empty, and repaired through locked atomic persistence.

## Round-1 review disposition

The requester-ownership proposal was intentionally not implemented because this deployment uses the single-user trust model: router-triggered automation carries the configured internal MindRoom user, matching the existing trusted automation path.
Designing multi-user todo ownership and authorization is a separate product and security change outside ISSUE-245.

`PLAN.md` and this report remain intentionally committed as review artifacts for the branch and will be stripped during squash merge, so the artifact-removal finding required no code change.

## Assigned-agent defaults

The `plan` and `apply_template` default-assignee behavior was already present on main from `7be4d90af`.

This change intentionally does not add a plan-level `assigned_agent` parameter.

Regression coverage now locks plan defaults, template defaults, and explicit template-assignee precedence.

## Validation

- `env -u MINDROOM_OWNER_USER_ID -u MINDROOM_DOCKER_WORKER_IMAGE uv run pytest -n auto --no-cov`: 10,718 passed and 120 skipped.
- `uv run pre-commit run --all-files`: passed.
- `uv run tach check --dependencies --interfaces`: passed.
- Focused todo-poke, orchestrator, todo-tool, and scheduling suites: 112 passed.
- Changed-file `ty` validation: passed without rule overrides.

The first all-files hook pass regenerated three tracked docs-skill reference outputs, and the required second pass was clean.

The first unsanitized pytest run exposed four unrelated failures because the shell exported owner and Docker image values that overrode isolated test fixtures.

Removing those two ambient values made the four tests and the complete suite pass.

On this Linux worktree, the all-files `ty` hook also needed local non-repository empty import stubs for the existing Darwin-only `AppKit`, `ApplicationServices`, and `Quartz` modules.

Without those local stubs, only untouched desktop modules fail import resolution, while all changed files type-check normally.

## Deployment and live-test notes

`~/.mindroom-chat/plugins/workloop/` is runtime state and should be removed as a deploy-time cleanup after the native worker is rolled out.

No repository code touches or removes that directory.

The later live test can use `MINDROOM_TODO_POKE_INTERVAL_SECONDS=10` and `MINDROOM_TODO_POKE_QUIET_SECONDS=0` to verify poke delivery, pending-schedule suppression for two intervals, and delivery after schedule cancellation.
