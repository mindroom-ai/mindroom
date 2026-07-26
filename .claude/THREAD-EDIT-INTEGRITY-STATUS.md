# PR #1641 thread-edit integrity status

## Active correction after exact `a0f30bc3` rejection

- PR: `https://github.com/mindroom-ai/mindroom/pull/1641`
- Branch: `fix/thread-edit-integrity`
- Rejected local, remote, and GitHub head: `a0f30bc3b96cf753d63716763fa898f2746d2043`
- Current base and merge base: `858282afc77adb480fa06cd9e4057d511ff861d5`
- Never amend, force-push, or merge.
- Verify `Bas Nijholt <bas@nijho.lt>` before every commit.

Fresh independent exact-head review reproduced one remaining production blocker.
Cached latest-edit lookup loads explicit rows only through the requested original ID.
If the cache already contains explicit edit `E -> B`, an unstored fetched original `A` can bundle a conflicting representation `E -> A`.
The conflicting cached row is invisible to selection for `A`, so the first point read and thread preview can expose the bundled forged body before persistence quarantines `E`.

Strict TDD plan:

1. Add SQLite and PostgreSQL RED regressions for cached `E -> B` plus unstored bundled `E -> A`.
2. Cover direct cache lookup, first network point read, thread-root preview, and older-valid fallback.
3. Make cache candidate collection load cached payloads for every bundled edit ID regardless of indexed original.
4. Reuse the same immutable-identity conflict rule in bundled preview selection.
5. Run focused and broad tests before an atomic commit and normal push.

Strict TDD evidence:

- Initial RED run failed all `8/8` cross-target cases across SQLite and PostgreSQL.
- The first implementation correctly blocked the forgery but also rejected a legitimate cached-ciphertext to clear-bundle upgrade.
- Four additional SQLite/PostgreSQL RED cases caught that over-strict behavior before commit.
- Shared cache observation semantics now distinguish compatible encrypted/provisional upgrades from immutable conflicts.
- Cached lookup and bundled preview both consume `get_latest_edit()` as the single candidate-selection contract.
- Exact focused GREEN matrix passes `12/12`.
- Nine owning cache/history/snapshot/approval/media files pass `801/801` across SQLite and PostgreSQL.
- Ruff, formatting, Tach dependencies/interfaces, and `git diff --check` pass.
- Current production correction is `+118/-21`, net `+97`; tests are `+274/-5`, net `+269`.

Exact `a0f30bc3` evidence is stale for the next code head:

- Broad Matrix/cache suite: `632` tests passed.
- Full pytest retry: `12252` tests passed, `54` skipped.
- Tach, all-file pre-commit, and `git diff --check` passed.
- GitHub pytest, smoke, Tach, docs, security, plugin fleet, and all four image builds passed.
- Real-Tuwunel was not launched.

Preserved live inputs:

- Harness SHA-256: `c91168f32354ebd142120158d60761ff6929927d02d4fcf6f131873d79ac755b`
- Scenario SHA-256: `6cb616b3c367f6b523f1e78b10196db3851baec640e9057d14533025eab8bfb4`
- nio exact: `e15f9e19ecbb8645564373d6c0fe7f7ffe06076f`

Any commit invalidates all earlier review, CI, full-suite, PostgreSQL, and live evidence.
Preserve every failure artifact.
Remove this handoff only before the next final exact-head freeze.
Never merge.
