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
- Updated `tach.toml` for the new orchestrator dependencies and documented both new modules in `CLAUDE.md`.

## Scanner behavior

- Todo files are scanned in sorted order, and malformed files are warned about and skipped independently.
- Persisted `main` thread sentinels normalize to room-main `None`.
- Only open, dependency-unblocked items with a nonempty configured direct-agent assignee can produce a poke.
- The quiet gate uses the newest `updated_at` among that assignee's actionable items in the scope.
- Direct-agent activity and activity in every running configured team containing that agent suppress the poke, with a second idle check immediately before delivery.
- Pending schedules are queried once per room and suppress only their existing room/thread scope.
- Runtime unavailability skips the whole tick, while an executed schedule query that errors fails open with a warning.
- Durable scope keys hash the canonical `(assigned_agent, room_id, normalized_thread_id)` tuple.
- Fingerprints include every actionable item, including items beyond the five shown in the message, plus thread total and terminal counts.
- An unchanged fingerprint never repeats, while a changed fingerprint waits for the cooldown.
- Failed sends do not consume the successful-delivery cap and do not persist poke state.

## Assigned-agent defaults

The `plan` and `apply_template` default-assignee behavior was already present on main from `7be4d90af`.

This change intentionally does not add a plan-level `assigned_agent` parameter.

Regression coverage now locks plan defaults, template defaults, and explicit template-assignee precedence.

## Validation

- `env -u MINDROOM_OWNER_USER_ID -u MINDROOM_DOCKER_WORKER_IMAGE uv run pytest`: 10,696 passed and 120 skipped.
- `uv run pre-commit run --all-files`: passed.
- `uv run tach check --dependencies --interfaces`: passed.
- Focused todo-poke, orchestrator, todo-tool, and scheduling suites: passed.
- Changed-file `ty` validation: passed without rule overrides.

The first unsanitized pytest run exposed four unrelated failures because the shell exported owner and Docker image values that overrode isolated test fixtures.

Removing those two ambient values made the four tests and the complete suite pass.

On this Linux worktree, the all-files `ty` hook also needed local non-repository empty import stubs for the existing Darwin-only `AppKit`, `ApplicationServices`, and `Quartz` modules.

Without those local stubs, only untouched desktop modules fail import resolution, while all changed files type-check normally.

## Deployment and live-test notes

`~/.mindroom-chat/plugins/workloop/` is runtime state and should be removed as a deploy-time cleanup after the native worker is rolled out.

No repository code touches or removes that directory.

The later live test can use `MINDROOM_TODO_POKE_INTERVAL_SECONDS=10` and `MINDROOM_TODO_POKE_QUIET_SECONDS=0` to verify poke delivery, pending-schedule suppression for two intervals, and delivery after schedule cancellation.
