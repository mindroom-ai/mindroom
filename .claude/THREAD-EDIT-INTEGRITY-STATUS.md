# PR #1641 thread edit integrity handoff

## Exact state

- PR: `#1641`
- Branch: `fix/thread-edit-integrity`
- Published predecessor: `10efcb9b7bb24aa9cb0a605fbcf01f9abdce5c86`
- Production and test correction head: `6ffe1b3618131333d09e8c26fa2f753a96f42596`
- Base and merge base: `858282afc77adb480fa06cd9e4057d511ff861d5`
- The working tree contains one narrow correction for two independently reproduced final-review blockers.
- Preserve the three untracked `.claude/TASK-*` files and `artifacts/`.
- Never amend, force-push, merge, or store durable evidence in a temporary directory.

## Reproduced blockers

1. A later sparse canonical view of an already cached event erased the existing valid `unsigned.m.relations.m.replace` bundle.
   SQLite and PostgreSQL then lost the only edit evidence and returned the original body.
2. `EventInfo` stripped whitespace from opaque Matrix event IDs.
   Padded thread and reply targets therefore aliased valid event IDs and entered cache indexing or transitive membership resolution.

## Strict TDD evidence

- Sparse bundle preservation failed four rich-to-sparse cases across SQLite and PostgreSQL before the fix; sparse-to-rich controls already passed.
- Padded relation IDs failed four `EventInfo` cases, both cache backends, and the membership seam before the fix.
- The shared representation observer now carries forward only a valid replacement bundle when two non-provisional canonical views have identical type and content and the later view has no valid bundle.
- Encrypted-to-clear and provisional-to-canonical upgrades still discard superseded bundles.
- Event IDs are accepted only when they are nonempty strings already equal to their stripped form; relation traversal never normalizes them.
- The combined new regression set passes `15` cases.
- The exact eight previously exposed upgrade/malformed regressions and all new cases pass in a `29`-case selection.
- Five owning/adjacent suites pass, including full SQLite/PostgreSQL cache coverage, relation parsing, membership, history, and replacement semantics.
- Six more backend/snapshot/interaction/cleanup/approval/reuse suites pass.
- Ruff, formatting, `ty`, and `git diff --check` pass.

## Remaining sequence

1. Commit and push the narrow production/tests correction with Bas Nijholt author identity.
2. Commit and push this crash-safe handoff separately.
3. Update the PR body and all campaign ledgers to the exact pushed head.
4. Run fresh independent exact-head Codex reviews and verify every current GitHub comment.
5. Run full pytest, explicit PostgreSQL stress, Tach, all-file pre-commit, and terminal CI on one unchanged head.
6. Remove this handoff in a final commit and restart every exact-head gate.
7. Run isolated real-Tuwunel last on the final unchanged head and preserve evidence.
8. Never merge.
