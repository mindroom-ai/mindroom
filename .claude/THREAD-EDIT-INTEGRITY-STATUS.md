# PR #1641 thread-edit integrity status

## Active correction after exact `d2ea24906` rejection

- PR: `https://github.com/mindroom-ai/mindroom/pull/1641`
- Branch: `fix/thread-edit-integrity`
- Rejected local, remote, and GitHub head: `d2ea24906f61ad08b844b0231d35604d7c99da8c`
- Current base and merge base: `858282afc77adb480fa06cd9e4057d511ff861d5`
- Never amend, force-push, or merge.
- Verify `Bas Nijholt <bas@nijho.lt>` before every commit.

Fresh independent exact-head review reproduced one production blocker.
One immutable replacement event ID can be bundled under original A while an explicit row with the same ID targets original B.
Full resolution applied the event ID to both originals, and both SQLite and PostgreSQL accepted both cache views without a tombstone.

Strict TDD evidence:

- RED full-resolution regression applied forged bodies to both root and reply.
- RED SQLite and PostgreSQL regressions returned the conflicting edit and created no tombstone.
- The cache regression covers bundled-first and explicit-first arrival, with the second representation arriving alone.
- Current focused GREEN selection passes `41` tests, including neighboring duplicate-identity, malformed-newest fallback, restart quarantine, and SQLite/PostgreSQL semantics cases.
- Ruff passes on every changed file.

Current uncommitted correction:

- `matrix/replacements.py` detects conflicting explicit and bundled replacement identities globally.
- Full thread resolution rejects every representation of a globally conflicting edit ID before hydration or projection.
- Shared cache admission compares top-level and bundled representations, including prior bundled-only rows found by backend-specific JSON lookup.
- Conflicts create durable tombstones, delete explicit/index rows, and scrub stored bundled copies.
- Cache reads union durable tombstones so a stale caller copy cannot revive a quarantined bundle.

Still required before the next freeze:

1. Run the broad owning Matrix/cache suites and static checks.
2. Commit and push the atomic correction with Bas identity.
3. Update the normal non-draft PR body and all external campaign ledgers to the new exact head.
4. Remove this handoff only in the final non-production freeze commit.
5. Restart fresh exact-head Codex review, GitHub CI, PostgreSQL stress, full pytest, Tach, all-file pre-commit, and `git diff --check`.
6. Only after review and CI pass, run the frozen real-Tuwunel replay on the same exact head.

Preserved live inputs:

- Harness SHA-256: `c91168f32354ebd142120158d60761ff6929927d02d4fcf6f131873d79ac755b`
- Scenario SHA-256: `6cb616b3c367f6b523f1e78b10196db3851baec640e9057d14533025eab8bfb4`
- nio exact: `e15f9e19ecbb8645564373d6c0fe7f7ffe06076f`

Any commit invalidates all earlier review, CI, full-suite, PostgreSQL, and live evidence.
Preserve every failure artifact.
Never merge.
