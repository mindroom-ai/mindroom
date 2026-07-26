# Thread edit integrity gate status

Updated 2026-07-25 after correcting the exact-`861eea90b` root-promotion review blocker.

## Exact target

- PR: `mindroom-ai/mindroom#1641`
- Branch: `fix/thread-edit-integrity`
- Latest production commit: `ce2c8e391108da90f2e8c5b6085f7231e9ad4e95`
- Current base and merge base: `f6190d4c2457381e63f40f99fb27e794ae8667b8`
- Current branch and PR head contain only the crash-handoff successor after the latest production commit.
- The production, test, and documentation corrections are pushed.
- Tracked working tree is clean.
- Resolve local, remote, and PR heads before counting any exact-head gate.
- Resolve and compare local, origin, and GitHub heads before counting any gate.
- Three pre-existing untracked `.claude/TASK-*.md` notes are user-owned and must remain untouched.

## Current correction set

- `6eaa95b61` makes replacements follow the outer `m.replace` target instead of trusting relations inside `m.new_content`.
- `57d53a6f9` allows legal rich replies with no primary `rel_type` to become thread roots.
- `1a7ed86de` prevents durable edit tombstones from resurfacing through cache-miss point reads or thread previews.
- `b27420c70` rejects explicit wrong-room payloads at the shared cache-write seam, restores SQL-level recent-event limits, and removes unreachable payload/index guards.
- `42c316201` synchronizes current `main`, including merged startup auto-resume and mixed-requester replay corrections.
- `7f3891d6d` updates eighteen stale test contracts and incomplete fixtures after the canonical edit-identity and rich-reply-root fixes.
- `8c4b3b1be` rejects state and wrong-room point-event envelopes before they can supply relation ancestry.
- `8c4b3b1be` also requires current-room message-capable timeline scope before raw cached relations can suppress a turn.
- `27a57ffd0` proves a related rich reply is not already a thread root before following its reply ancestry.
- `27a57ffd0` also covers the trusted-automation rich-reply fallback and corrects cache documentation that still described removed read-time payload/index guards and recent-event cursors.
- `e14938185` proves current rich-reply root status before accepting inherited indexes, gives batch projection the same current-root semantics, and preserves mutation-time inherited-index proof.
- `0f439bd2a` rejects invalid successful point lookups and legacy wrong-room cache rows in snapshot and replay consumers while retaining edits as non-visible snapshot ancestry nodes.
- `ce2c8e391` transactionally invalidates a rich reply's old parent snapshot when its first explicit child promotes it to a thread root.

## Reconciled review status

- The complete Opus review, Codex review, and reconciled `DEBATE.md` from the `ssh pc` worktrees were read.
- All six consensus blockers are addressed in production or tests.
- The tracked living handoff intentionally remains only while final gates are active.
- Claude's exact-`0b927ee66` red-suite report is valid about the eighteen failures but wrong that legacy wrong-room rows now flow through; read certification still rejects them.
- A fresh native Codex review found two additional state-event relation gaps, and both reproduced deterministically before correction.
- The remote exact-`0b927ee66` `REVIEW2.md` correctly found that a proven rich-reply root reached mid-walk inherited its parent's thread.
- The mid-walk bug reproduced on the pushed predecessor, and the fix moves the existing proof before ancestry with zero net production-line growth.
- One fresh exact-`d91751bc1` reviewer independently found the same stale cache-documentation contract; the other approved with no finding.
- Two exact-`3e0575aa7` native reviewers reproduced stale inherited root precedence, missing batch root promotion, invalid successful lookup fallback, wrong-room replay/snapshot reads, and replacement-node snapshot ancestry.
- Every exact-`3e0575aa7` finding is corrected in `e14938185` and `0f439bd2a`; those old review verdicts and CI results are now stale.
- The fresh exact-`861eea90b` native review found one further blocker: durable root promotion rewrote the rich reply's index but left its old parent snapshot certified.
- The claim failed four regression variants before implementation and passes after `ce2c8e391`; the reviewer found no second blocker.
- The withdrawn edit-index timestamp-poison claim remains excluded because it required direct inconsistent SQL writes outside production paths.
- Production source against current `main` is `+1601/-1138`, net `+463`.

## Validation already completed

- Forged edit thread-identity owning suites: `135` passed.
- Rich-reply root owning suites: `170` passed.
- Tombstoned point-read and preview suites: `377` passed, including SQLite and PostgreSQL.
- Cache, backend, and agent-message snapshot suites after source reset: `303` passed, including SQLite and PostgreSQL.
- The same `303` tests passed again after merging current `main`.
- All `18` exact failed tests plus one legacy wrong-room read regression pass after test correction.
- Expanded owning suites pass across SQLite and PostgreSQL after the state-event fixes.
- The complete eight-file rich-root, membership, tag, mode, mutation, sync, write-coordination, and coalescing selection passes after the mid-walk correction.
- The exact five GitHub pytest failures on `3e0575aa7` now pass with corrected production-shaped root-proof and cross-room fixtures.
- The seven affected owning files pass together, including SQLite and PostgreSQL snapshot regressions for legacy wrong-room rows and reply-to-edit ancestry.
- Root-promotion invalidation failed before implementation and now passes all four SQLite/PostgreSQL point-store/thread-append variants.
- The `304` focused event-cache, cache-semantics, mutation, and membership tests pass after the fix and after merging current `main`.
- Changed-file pre-commit passes, including Ruff, formatting, `ty`, Vulture, Tach, module privacy, and generated documentation checks.
- Ruff, format, `ty`, Vulture, Tach, module privacy, and normal commit hooks pass.
- Git author was verified as `Bas Nijholt <bas@nijho.lt>` before every new commit.

## Pending exact-head gates

- Fresh CI must complete on the current crash-handoff successor head.
- A fresh independent native Codex correctness review is required on that exact head.
- Exact-head PostgreSQL owning and stress selections are required.
- Exact-head full pytest is required.
- Exact-head Tach and all-file pre-commit are required.
- Exact-head real-Tuwunel validation is required after merged #1666 and #1667, using isolated persistent evidence on `ssh pc`.
- Any branch-head change invalidates every review, CI, and live result.

## Completion rule

- Preserve all failure artifacts and exact-source provenance.
- Verify every current GitHub comment against the exact code before changing it.
- Remove this living handoff in a final non-production commit only after all exact-head gates pass.
- That removal changes the exact head, so rerun the required lightweight exact-head checks and confirm no production diff changed.
- Never merge.
