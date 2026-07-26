# PR #1641 thread-edit integrity status

## Exact `9ead99e87` review rejection

- PR: `https://github.com/mindroom-ai/mindroom/pull/1641`
- Branch: `fix/thread-edit-integrity`
- Rejected local, remote, and GitHub head: `9ead99e87e15f59e8c9368451110fdf18bdcc674`
- Base and merge base: `858282afc77adb480fa06cd9e4057d511ff861d5`
- Never amend, force-push, merge, or run real-Tuwunel on a rejected head.
- Verify `Bas Nijholt <bas@nijho.lt>` before every commit.

Both fresh independent exact-head reviews returned `CHANGES REQUIRED`.
The exact-head full suite, all-file hooks, Tach, and green GitHub jobs are historical after this rejection.

Confirmed blockers to reproduce independently before production edits:

1. Wrong-room or state duplicates are excluded from visible messages but still overwrite full-scan ordering and ancestry maps.
2. Full scan can accept one event ID simultaneously as a standalone message and an edit.
3. Cached encrypted or provisional edit representations can accept a same-ID bundled representation whose replacement target differs.
4. Legitimate same-target encrypted-to-clear and provisional-to-canonical transitions are accepted by cache admission but rejected later by strict replacement deduplication.
5. Point lookup can fall through to a raw explicit wrong-room server response after validated projection rejects it.

Strict TDD plan:

1. Add deterministic full-resolution RED cases for invalid duplicate ordering and standalone/edit identity collision.
2. Add SQLite and PostgreSQL RED cases for target-drifting encrypted/provisional representations.
3. Add full-resolution plus SQLite/PostgreSQL RED cases for legal encrypted-to-clear and provisional-to-canonical upgrades.
4. Add the wrong-room explicit point-response RED case at the conversation-cache seam.
5. Centralize one transition-aware event-identity reconciliation policy.
6. Make full scan, cache selection, cache admission, and point lookup consume the same policy.
7. Run focused, owning, full, static, review, CI, and exact-head live gates after a new handoff-free freeze.

Strict TDD evidence:

- All `14/14` new full-resolution, point-read, SQLite, and PostgreSQL variants failed before production edits.
- One transition-aware reconciler now owns immutable envelope, room, replacement-target, encrypted-to-clear, and provisional-to-canonical decisions.
- The first implementation over-rejected opaque non-edit relation corrections and poisoned invalid self-bundles.
- Existing monotonic point-payload and self-replacement tests caught both errors before commit.
- The second broad run exposed an equivalent explicit edit without embedded `room_id` failing to materialize its point row.
- The authoritative cache room scope now permits that identical explicit representation.
- The final exact regression neighborhood passes `23/23`.
- The two owning files pass `385/385` across SQLite and PostgreSQL.
- Ten replacement consumers pass `903/903`, including snapshots, approvals, media, previews, and cache interaction contracts.
- Import-graph, module-privacy, event-cache semantics, Tach split-boundary, compaction-invariant, Ruff, formatting, Tach, and diff checks pass.
- Current production correction is net negative: `+26/-70`, net `-44`.

Any commit invalidates all earlier review, CI, full-suite, PostgreSQL, and live evidence.
Preserve every failure artifact.
Remove this handoff only before the next final exact-head freeze.
Never merge.
