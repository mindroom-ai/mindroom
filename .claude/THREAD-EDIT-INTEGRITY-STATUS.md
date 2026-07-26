# PR #1641 thread-edit integrity handoff

## Current exact state

- PR: `https://github.com/mindroom-ai/mindroom/pull/1641`
- Branch: `fix/thread-edit-integrity`
- Published implementation head: `e3c38f43d`
- Base and merge-base: `858282afc77adb480fa06cd9e4057d511ff861d5`
- Git author required before every commit: `Bas Nijholt <bas@nijho.lt>`
- Never amend, force-push, merge, or use temporary directories for durable work.

## Exact-head evidence now invalidated

Exact `4e925fbae` passed full pytest (`12304`, 54 skipped), the seven PostgreSQL stress tests, all-file pre-commit, Tach, and `git diff --check`.
Those results remain useful diagnostic evidence but cannot certify a later head.
Real Tuwunel has not run on `4e925fbae`.

## Active review round

Fresh exact-`4e925fbae` review reported four blockers.
All were independently reproduced before production edits:

1. Full-history ordering retains stale opaque or provisional representations after a canonical same-ID upgrade.
2. Generic visible resolution observes top-level identities before bundled identities, allowing one edit ID to target two originals.
3. Room-history scan has the same bundled-identity gap before per-thread grouping.
4. Cache batch admission derives indexes and sidecar ownership from intermediate accepted representations instead of one final canonical representation.

An additional SQLite/PostgreSQL probe confirmed latest-edit reads did not compare incoming bundles with cached bundled identities under other originals.
The RED matrix failed 14 deterministic cases before production changes.
Commit `e3c38f43d` centralizes final top-level-plus-bundled identity reconciliation and makes all 18 new cases pass across full resolution, room scan, generic visible resolution, SQLite, and PostgreSQL.
The owning suites passed for `test_thread_history.py`, `test_event_cache.py`, `test_stale_stream_cleanup.py`, `test_event_cache_backends.py`, and `test_matrix_cache_interaction_contract.py`.
Ruff, ty, Tach, pre-commit on changed files, and `git diff --check` pass.
Production delta for this correction is `+166/-66`, net `+100`; the cache redaction helper moved to the shared replacement seam.

## Final gates

After the last code commit:

- Remove this file in a dedicated final commit.
- Run fresh independent exact-head Codex reviews.
- Require terminal green GitHub CI and zero unresolved current review threads.
- Run full pytest, relevant SQLite/PostgreSQL backend tests, all-file pre-commit, Tach, and `git diff --check`.
- Read and run the preserved real-Tuwunel procedure on the same exact head, retaining artifacts.
- Any code change invalidates all reviews and gates.
