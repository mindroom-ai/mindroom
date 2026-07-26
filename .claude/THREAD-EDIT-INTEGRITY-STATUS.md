# PR #1641 thread edit integrity handoff

## Exact state

- PR: `#1641`
- Branch: `fix/thread-edit-integrity`
- Production and test correction head: `e77d32752a33e41ef724f87e7f5efd077ea4c257`
- This tracked handoff is a status-only successor; resolve its exact self-referential head with `git rev-parse HEAD`.
- Current base and merge-base: `858282afc77adb480fa06cd9e4057d511ff861d5`
- Never amend, force-push, merge, or store durable evidence in a temporary directory.
- Preserve the three untracked `.claude/TASK-*` files and `artifacts/`.

## Current exact-head blockers

Fresh independent review of `d84284c2` reproduced three production blockers.

1. A sparse retained view of an already fetched event overwrote fetched `unsigned.m.relations`, deleting the server-provided bundled replacement.
2. Repair acknowledgement snapshotted one retained representation, awaited a cache read, then deleted a same-ID overwrite by ID alone.
3. A redacted cached representation covered same-ID retained evidence without validating sender, room, state, type, or timestamp identity.

The append path had the same ID-only acknowledgement race after incremental revalidation.

## Correction and TDD evidence

- Strict RED: 13 atomic-ack and redacted-identity cases failed before production changes.
- Strict RED: both fetched-rich/retained-sparse and fetched-sparse/retained-rich aggregation cases failed before the coverage correction.
- Retained acknowledgement now performs one synchronous exact-representation compare-and-delete in `ThreadRepairRegistry`.
- Both repair and incremental append pass the exact representation they proved durable.
- Redacted coverage now requires matching immutable event identity.
- Canonical same-payload fetched views cover retained duplicates without comparing optional server-generated `unsigned` data.
- Fetched `unsigned` remains authoritative, while opaque-to-clear and provisional-to-canonical upgrades remain legal.
- Focused corrected selection: 16 passed.
- Five affected files across SQLite and PostgreSQL: passed.
- Four adjacent membership/resolver/thread-context suites: passed.
- Ruff, ty, Tach, and `git diff --check`: passed.
- Fresh reviewers found no fourth blocker in the dirty correction.
- Aggregation and redacted-identity commit: `a4ac722121db7f92f03658259e0543a0afc7da43`.
- Exact-acknowledgement commit: `e77d32752a33e41ef724f87e7f5efd077ea4c257`.

## Remaining sequence

1. Update the PR body to this exact head and run fresh exact-head independent Codex correctness review.
2. Ingest every current GitHub comment.
3. Run full pytest, explicit PostgreSQL backend stress, all-file pre-commit, Tach, and CI on one unchanged head.
4. Delete this living handoff in a final commit, then restart every exact-head approval and validation invalidated by that commit.
5. Run the preserved real-Tuwunel gate last on that exact unchanged final head and retain all evidence.
6. Never merge.
