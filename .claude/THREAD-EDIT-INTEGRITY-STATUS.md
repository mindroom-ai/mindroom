# Thread edit integrity follow-up status

Updated: 2026-07-24 08:52 America/Los_Angeles.

## Goal

Fix wrong-sender Matrix replacements in full thread-history resolution.

Fix SQLite and PostgreSQL equal-timestamp replacement tie-breaking to use lexicographically greatest event ID.

Open a normal PR, validate CI and AI review, and never merge it.

## Repository state

Branch: `fix/thread-edit-integrity`.

Verified base: `HEAD` and freshly fetched `origin/main` were both `66dd4f4a68bcfd1a5e43b2cac20a1b464f306ab1`.

Pre-existing untracked `.claude/TASK-1784907055-a461.md` belongs to the task harness and must remain uncommitted.

Dependencies were refreshed with `uv sync --all-extras`.

## Verified bugs on unchanged main

An owning-seam probe resolved Alice's `$original` through Mallory's newer `$forged` edit and produced body `FORGED`.

An SQLite probe stored `$z-edit` and then `$a-edit` at timestamp `2000`, and `get_latest_edit()` returned `$a-edit`.

SQLite and PostgreSQL loaders both order equal-timestamp edits by `write_seq DESC`.

The full-scan helper already selects by `(server_timestamp, event_id)`.

## Shared design note

Read `fuzz_reactions_edits_design.md` after it appeared at 08:32.

It confirms sender equality and `(origin_server_ts, event_id)` as the required Matrix invariants.

It rejects timeline-order tie-breaking.

Re-read this note before edits, commits, and final handoff because it is living shared state.

## Implemented changes

Full resolution now retains the latest replacement candidate per target and sender.

Projection selects only the candidate whose sender matches the known target event sender.

Room scans retain each sender's candidate so a newer invalid replacement cannot discard an older valid same-sender candidate.

Valid edit-only recovery remains available when the original event is absent and its sender is unknown.

SQLite and PostgreSQL latest-edit queries now order equal timestamps by edit event ID descending.

Full-resolution regressions cover wrong-sender root, threaded message, reply, and edit-of-edit shapes.

A backend-parametrized regression stores `$z-edit` before `$a-edit` at equal timestamps and expects `$z-edit`.

## Validation ledger

- `uv sync --all-extras`: passed.
- Direct wrong-sender full-resolution probe on unchanged main: reproduced.
- Direct SQLite tie-break probe on unchanged main: reproduced.
- New wrong-sender regression before implementation: failed with forged visible content.
- New equal-timestamp SQLite regression before implementation: failed with `$a-edit`.
- Sender-integrity targeted tests after implementation: 4 passed.
- Equal-timestamp backend regression after implementation: 2 passed, including SQLite and PostgreSQL.
- Sender-scoped plus equal-timestamp backend regressions: 4 passed across SQLite and PostgreSQL.
- Affected suite across `test_thread_history.py`, `test_event_cache.py`, `test_event_cache_backends.py`, `test_event_cache_contract.py`, `test_matrix_cache_interaction_contract.py`, and `test_thread_resolution_reuse.py`: passed.
- Ruff on touched source and test files: passed.
- Pyright spot check: unavailable because the executable is not installed by the project environment.
- Targeted tests: passed.
- Relevant backend tests: passed.
- Full pytest: `11499 passed, 54 skipped, 78 warnings` in 181.80 seconds.
- Tach: passed after the full-suite run, with no boundary-file change required.
- All-file pre-commit: passed on the second run.
- The first all-file pre-commit run exposed pre-existing Prettier drift in seven unrelated frontend files.
- The hook-created frontend formatting was deliberately excluded from this narrow branch after the passing second run.

## Git and PR ledger

Sender-integrity commit `543f08094` was pushed to `origin/fix/thread-edit-integrity`.

Cache tie-break commit `00bd54304` was pushed to `origin/fix/thread-edit-integrity`.

Validation handoff commit `7bdc33950` was pushed to `origin/fix/thread-edit-integrity`.

Remote branch exists.

PR: `https://github.com/mindroom-ai/mindroom/pull/1641`.

PR is open, normal, and not a draft.

PR head at creation: `349cee5cc17bb7d7a309afe3ea10d343fc73ed1c`.

GitHub reports the branch mergeable with checks queued or running.

Greptile and CodeRabbit reviews are pending.

Sourcery hit its weekly rate limit, Gemini review is sunset, and Qodo initially reported paused or busy states.

Before every commit, verify `git var GIT_AUTHOR_IDENT` resolves to `Bas Nijholt <bas@nijho.lt>`.

Never amend, force-push, create a draft PR, or merge.
