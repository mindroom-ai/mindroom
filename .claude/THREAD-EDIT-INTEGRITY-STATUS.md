# PR #1641 thread edit integrity handoff

## Exact state

- PR: `#1641`
- Branch: `fix/thread-edit-integrity`
- Published head before this correction: `d84284c2002a5d8c1351c74b0d62fc4943458a4b`
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

## Remaining sequence

1. Commit the identity/aggregation correction and exact-ack correction separately with Bas author identity.
2. Push normally and update the PR body plus external campaign ledgers to the exact new head.
3. Run fresh exact-head independent Codex correctness review and ingest every current GitHub comment.
4. Run full pytest, explicit PostgreSQL backend stress, all-file pre-commit, Tach, and CI on one unchanged head.
5. Delete this living handoff in a final commit, then restart every exact-head approval and validation invalidated by that commit.
6. Run the preserved real-Tuwunel gate last on that exact unchanged final head and retain all evidence.
7. Never merge.
