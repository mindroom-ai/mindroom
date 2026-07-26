# Thread edit integrity gate status

Updated 2026-07-25 after classifying the exact-`0b927ee66` full-suite failures and fresh review blockers.

## Exact target

- PR: `mindroom-ai/mindroom#1641`
- Branch: `fix/thread-edit-integrity`
- Latest production commit: `8c4b3b1be541004c6503718db1aa222abd0db6aa`
- Current base and merge base: `c1f812a1e15b3c6be05f0cf2720b44431d844087`
- Current branch and PR head contain only crash-handoff commits after the latest production commit.
- The production and test corrections are pushed.
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

## Reconciled review status

- The complete Opus review, Codex review, and reconciled `DEBATE.md` from the `ssh pc` worktrees were read.
- All six consensus blockers are addressed in production or tests.
- The tracked living handoff intentionally remains only while final gates are active.
- Claude's exact-`0b927ee66` red-suite report is valid about the eighteen failures but wrong that legacy wrong-room rows now flow through; read certification still rejects them.
- A fresh native Codex review found two additional state-event relation gaps, and both reproduced deterministically before correction.
- The withdrawn edit-index timestamp-poison claim remains excluded because it required direct inconsistent SQL writes outside production paths.
- Production source against current `main` is `+1483/-1103`, net `+380`.
- This is 109 net production lines smaller than the reviewed `+474` baseline and 61 lines smaller than the reconciled review estimate.

## Validation already completed

- Forged edit thread-identity owning suites: `135` passed.
- Rich-reply root owning suites: `170` passed.
- Tombstoned point-read and preview suites: `377` passed, including SQLite and PostgreSQL.
- Cache, backend, and agent-message snapshot suites after source reset: `303` passed, including SQLite and PostgreSQL.
- The same `303` tests passed again after merging current `main`.
- All `18` exact failed tests plus one legacy wrong-room read regression pass after test correction.
- Expanded owning suites pass across SQLite and PostgreSQL after the state-event fixes.
- Ruff, format, `ty`, Vulture, Tach, module privacy, and normal commit hooks pass.
- Git author was verified as `Bas Nijholt <bas@nijho.lt>` before every new commit.

## Pending exact-head gates

- GitHub pytest is red only on stale head `0b927ee66`; every other completed check there is green.
- Fresh CI and review are active on the new handoff-containing head.
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
