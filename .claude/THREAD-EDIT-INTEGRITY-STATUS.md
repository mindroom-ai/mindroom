# PR #1641 thread-edit integrity handoff

## Current exact state

- PR: `https://github.com/mindroom-ai/mindroom/pull/1641`
- Branch: `fix/thread-edit-integrity`
- Published head before active correction: `719307bc4a2c572fae15983d0245f72c345cf861`
- Base and merge-base: `858282afc77adb480fa06cd9e4057d511ff861d5`
- Git author required before every commit: `Bas Nijholt <bas@nijho.lt>`
- Never amend, force-push, merge, or use temporary directories for durable work.

## Exact-head evidence now invalidated

Exact `719307bc4` passed full pytest (`12268` passed, 54 skipped), the selected PostgreSQL stress matrix (`14` passed), and the earlier owning suites.
GitHub builds and static checks were green while smoke, pytest, and Greptile were still running.
Real Tuwunel had not run.
These results remain diagnostic evidence but cannot certify a corrected head.

## Active correction

Two fresh exact-`719307bc4` reviewers reproduced five blockers before production changes:

1. Final room-scan canonicalization forgot already-quarantined event IDs and resurrected a conflicting bundled edit.
2. Trusted provisional plaintext outbound events conflicted with opaque encrypted sync echoes and were tombstoned in both arrival orders.
3. Bundles from superseded provisional or opaque top-level representations poisoned the final canonical identity set.
4. Scrubbing a bundled edit did not advance the parent point row's `write_seq`, so process-local resolution reuse served the redacted edit.
5. A definitive `NOT_A_THREAD_ROOT` proof was overwritten by a stale advisory thread index.

Strict TDD evidence:

- The first two regressions failed `6/6` cases before their production correction.
- The remaining three regression families failed `18/18` cases before their production correction.
- The combined matrix now passes `24/24`, including both SQLite and PostgreSQL.
- Full owning suites pass for thread history, cache mutations, event cache, resolution reuse, and membership resolution.
- Ruff, formatting, ty, Tach, and `git diff --check` pass.

The correction keeps one two-phase identity policy: settle final top-level representations, then observe only their bundles.
Room scans carry prior conflict evidence into that canonical pass.
Provisional plaintext is preserved over opaque sync echoes until a canonical clear echo arrives.
Bundled scrubs advance the durable point revision in both backends.
Definitive relation/root proof now wins over stale cache indexes.

## Final gates

After the last code commit:

- Remove this file in a dedicated final commit.
- Run fresh independent exact-head Codex reviews.
- Require terminal green GitHub CI and zero unresolved current review threads.
- Run full pytest, relevant SQLite/PostgreSQL backend tests, all-file pre-commit, Tach, and `git diff --check`.
- Read and run the preserved real-Tuwunel procedure on the same exact head, retaining artifacts.
- Any code change invalidates all reviews and gates.
