# Thread edit integrity follow-up status

Updated: 2026-07-24 08:38 America/Los_Angeles.

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
- Ruff on touched source and test files: passed.
- Pyright spot check: unavailable because the executable is not installed by the project environment.
- Targeted tests: pending.
- Relevant backend tests: pending.
- Full pytest: pending.
- Tach: pending boundary check.
- All-file pre-commit: pending.

## Git and PR ledger

First atomic sender-integrity commit is being prepared.

No remote branch or PR yet.

Before every commit, verify `git var GIT_AUTHOR_IDENT` resolves to `Bas Nijholt <bas@nijho.lt>`.

Never amend, force-push, create a draft PR, or merge.
