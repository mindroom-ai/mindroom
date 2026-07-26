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

## Active correctness blocker

Fresh exact-head review reproduced a room-scan identity-state loss:

1. Two conflicting explicit representations of event ID `E` correctly quarantine `E` while scanning.
2. The scan retains only surviving source values and separately records `E` in `conflicting_event_ids`.
3. Final canonicalization ignores the prior conflict set and sees only a root bundling `E`.
4. The bundled representation resurrects `E` and edits the root despite the immutable identity conflict.

Strict TDD order:

- Add an actual room-scan regression with bundled `E` plus two conflicting explicit `E` representations.
- Prove RED before production edits.
- Carry prior conflict evidence through final canonicalization/scrubbing at the owning seam.
- Run owning full-resolution tests, SQLite/PostgreSQL parity where affected, then all final gates.

## Final gates

After the last code commit:

- Remove this file in a dedicated final commit.
- Run fresh independent exact-head Codex reviews.
- Require terminal green GitHub CI and zero unresolved current review threads.
- Run full pytest, relevant SQLite/PostgreSQL backend tests, all-file pre-commit, Tach, and `git diff --check`.
- Read and run the preserved real-Tuwunel procedure on the same exact head, retaining artifacts.
- Any code change invalidates all reviews and gates.
