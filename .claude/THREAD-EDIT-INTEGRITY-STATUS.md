# Thread edit integrity gate status

Updated 2026-07-25 after correcting the two malformed-relation blockers on exact current `main`.

## Exact target

- PR: `mindroom-ai/mindroom#1641`
- Branch: `fix/thread-edit-integrity`
- Latest production commit: `239e9203b28350c43a4afde42d3fd58f11e0ee6a`
- Current base and merge base: `e95fbe9a4bc069340fd36f333f3d7424657e1056`
- Latest integration commit: `6a7b3f473b089e283648cdc392df6e4fbf28b801`
- Latest pushed code head: `239e9203b28350c43a4afde42d3fd58f11e0ee6a`.
- This crash-handoff update will be the docs-only successor; resolve local, remote, and PR heads before counting a gate.
- The negative-root-proof follow-up, exact-current-main synchronization, and malformed-relation correction are pushed.
- The tracked tree will be clean after this crash-handoff successor is committed.
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
- `d8926444c` rejects state and wrong-room events from mutation root proofs and cleanup ancestry.
- `d8926444c` validates fetched replacements against their outer originals and preserves only original reply ancestry.
- `d8926444c` also memoizes one mutation resolution so root proof does not repeat the same durable index lookup.
- `ae5f72397` rejects fetched message envelopes that parse as `nio.BadEvent` before they can provide cleanup requester ancestry.
- `6eea89ebf` makes related replacements follow their original before consulting a legacy cached membership index.
- `6eea89ebf` also retains valid replacement rows as non-visible room-scan ancestry so replies targeting an edit remain in the original thread.
- The current correction requires canonical replacement validity before an edit may supply thread ancestry in point, scan, cache-certification, snapshot, cleanup, or mutation paths.
- The current correction rejects malformed `m.room.message` point events and cache rows before their raw relations can create durable thread indexes.
- Known invalid edits resolve room-level; unavailable replacement ancestry remains indeterminate so mutation writes fail closed.
- The current follow-up supplies the exact inbound edit source to canonical validation before it is persisted and reuses one fetched original for relation resolution.
- Intended-valid tests now use same-sender originals and complete point-event envelopes; missing-original mutation tests assert direct fail-closed invalidation without an obsolete index lookup.
- `9c25053bf` rejects malformed successful point lookups, malformed sync relations, and malformed cached child proof before mutation ancestry can trust them.
- `4cb69803f` centralizes valid thread-relation source admission in `matrix.media` and applies it to degraded replay, preventing malformed cached relations from suppressing a valid turn.
- `708ebfffa` synchronizes current `origin/main` without conflict after the two TDD corrections.
- `7c8adfb41` merges current `main`, including the bounded concurrent thread-cache repair from PR #1656.
- Seven conflicts were reconciled by retaining main's repair outcomes and live-delta replay while keeping #1641's strict room, timeline, event-envelope, and membership certification.
- A new TDD regression proves a wrong-room homeserver row is never installed into the durable cache through the new repair loop.
- Four incomplete main repair fixtures now use complete Matrix timeline envelopes; production validation was not weakened.
- `5b1486aa8` preserves room-level thread history after proving a candidate is not itself a thread root.
- `6a7b3f473` merges exact current `main` without conflict and preserves the canonical replacement and thread-membership architecture unchanged.
- Exact current `main` includes PR #1673 and the exact history for PR #668.
- PR #1671 is not present in this main head and was not introduced.
- `239e9203b` rejects parsed `nio.BadEvent` room-scan entries before they can supply reply ancestry.
- `239e9203b` reuses raw relation sources for metadata only after canonical room, timeline, event-type, and plaintext-envelope validation.
- The validated memo preserves the single-fetch contract while preventing malformed replacement originals from being reparsed into thread ancestry.

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
- The fresh exact-`592220fa2` native review found two further blockers: poisoned cached children could prove a false rich-reply root, and cleanup trusted state, wrong-room, and forged replacement ancestry.
- Both claims reproduced before implementation and are corrected in `d8926444c`.
- Exact-`592220fa2` GitHub pytest failed only two redundant-index-lookup expectations; the same production correction restores the one-lookup contract.
- The fresh exact-`9093c2a90` native review found one further blocker: a fetched original missing `msgtype` parsed as `nio.BadEvent` but still supplied attacker-controlled reply ancestry.
- The malformed-original case failed before implementation and passes after `ae5f72397`; valid originals, wrong-sender replacements, edit-of-edit targets, and invalid scope remain correct.
- Two fresh exact-`7f4a9eaea` native reviewers found a stale handoff count plus two blockers: a legacy edit index could override the original's membership, and cold scans dropped replies targeting valid edits.
- Both code claims failed deterministic regressions before implementation and pass after `6eea89ebf`; edits remain non-visible.
- Two fresh exact-`09090b1ec` native reviewers independently found that structurally invalid replacements still supplied ancestry after being retained as non-visible graph nodes.
- One reviewer also found that a successful `nio.BadEvent` message envelope could supply a raw thread relation and persist self/root indexes.
- Ten wrong-sender, malformed, wrong-type, edit-of-edit, cache-certification, point-read, and SQLite/PostgreSQL index variants failed before this correction.
- All exact claims were reproduced before implementation; the correction centralizes replacement validation in `thread_membership` and leaves storage/transport seams responsible only for source loading.
- Exact-`845b12e1d` full pytest exposed sixteen deterministic failures in the newly tightened seams.
- One was a real current-event visibility gap; the remaining failures were incomplete envelopes, cross-sender fixtures that claimed to be valid, or obsolete lookup-count expectations.
- The exact failed files were corrected without weakening production validation.
- The fresh exact-`ac144420b` native review found malformed `nio.BadEvent` relations still entering point mutation ancestry, sync admission, cached root proof, and degraded replay.
- All four claims reproduced as RED regressions before implementation and pass after `9c25053bf` plus `4cb69803f`.
- The same review rejected a suspected edit-of-edit root-proof claim because it did not reproduce.
- Exact-`ac144420b` review, CI, full-suite, and static evidence are stale after the production corrections and current-main merge.
- The fresh exact-`5f2c9ed49` correctness review found two additional malformed-source gaps in cold room scans and shared replacement/source memoization.
- Both claims reproduced as RED full-resolution, sync-graph, SQLite, and PostgreSQL regressions before implementation.
- The first attempted split metadata cache over-fetched valid sources and broke existing access contracts; root-cause tracing replaced it with validated source reuse rather than fixture churn or a production fallback.
- The exact-`5f2c9ed49` review and all earlier approvals are stale after `239e9203b`.
- The withdrawn edit-index timestamp-poison claim remains excluded because it required direct inconsistent SQL writes outside production paths.
- This correction adds `+196/-15` production lines, net `+181`.
- Production source against current `main` is `+1970/-1180`, net `+790`.
- The full-suite follow-up adds `+28/-4` production lines, net `+24`.
- The malformed mutation correction adds `+18/-6` production lines, net `+12`.
- The degraded replay follow-up adds `+16/-18` production lines, net `-2`.
- The latest malformed-relation correction adds `+10/-1` production lines, net `+9`.

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
- Nine new state, wrong-room, wrong-sender, forged-relation, and edit-of-edit regressions failed before `d8926444c` and pass afterward.
- The two exact GitHub pytest failures reproduce before `d8926444c` and pass afterward.
- The three owning cleanup, mutation, and read-guard files pass together after `d8926444c`.
- The nine-file Matrix/thread selection passes after `d8926444c`, including SQLite and PostgreSQL.
- Exact-head Tach and changed-file pre-commit pass after the new canonical replacement dependency was declared.
- Changed-file pre-commit passes, including Ruff, formatting, `ty`, Vulture, Tach, module privacy, and generated documentation checks.
- Ruff, format, `ty`, Vulture, Tach, module privacy, and normal commit hooks pass.
- The malformed-original regression failed before `ae5f72397` and passes afterward with six neighboring exact-fetch variants.
- The complete stale-stream cleanup file passes after `ae5f72397`.
- Ruff, format, `ty`, and normal commit hooks pass for `ae5f72397`.
- Exact-`7f4a9eaea` PostgreSQL/cache suites passed `285` tests, full pytest passed `12041` tests with `54` skipped, and Tach plus all-file pre-commit passed.
- Exact-`7f4a9eaea` real-Tuwunel stopped all bots but hung during its first planned restart after `73` operations and required `SIGKILL`.
- The identical frozen harness, scenario, nio head, and Tuwunel image passed `200` operations against exact current `main`, so the liveness failure is specific to the PR predecessor.
- The two edit-membership regressions failed before `6eea89ebf`; both and their complete owning files pass afterward (`147` tests).
- Ruff, format, `ty`, Tach, module privacy, and normal commit hooks pass for `6eea89ebf`.
- The ten new exact review cases fail before the current correction; all thirteen parametrized regression variants pass afterward across SQLite and PostgreSQL.
- Three neighboring live/outbound mutation tests initially caught missing current-source and unavailable-original handling; all three pass after the source is seeded and unavailable ancestry remains indeterminate.
- The complete twelve-file owning selection passes `792` tests, including SQLite, PostgreSQL, 45-thread fanout, seeded fuzz, snapshots, mutations, tags, room scans, and cleanup.
- The exact sixteen full-suite failures reproduce before the follow-up.
- All `54` directly affected tests pass afterward.
- The expanded seventeen-file owning and affected selection passes `958` tests, including SQLite, PostgreSQL, fanout, seeded fuzz, inbound context, sync/live mutation, and turn control.
- Ruff, format, `ty`, Tach dependencies/interfaces, and diff checks pass for the current correction.
- Ruff, format, `ty`, Tach dependencies/interfaces, and diff checks pass for the full-suite follow-up.
- The four malformed-relation cases failed before implementation and their nine state, wrong-room, malformed, point, sync, root-proof, and replay variants pass afterward.
- The expanded live-coalescing, mutation, membership, read-guard, tag, and media owning selection passes after synchronizing current `main`.
- Ruff, format, `ty`, Tach dependencies/interfaces, module privacy, focused pre-commit, and diff checks pass for both malformed-relation commits.
- Exact-`5f2c9ed49` full pytest passed `12014` tests with `54` skipped before current `main` advanced; it is historical evidence only.
- The wrong-room repair-loop regression failed before integration and passes after the store guard.
- The merged history/cache owning cluster passes `473` tests, including SQLite and PostgreSQL.
- Main's new repair, backfill, backend, parallelism, read-guard, and conversation-resolution cluster passes `120` tests.
- The merge commit hooks pass Ruff, formatting, `ty`, Vulture, Tach, module privacy, and generated-documentation checks.
- Exact-main synchronization passes `128` focused chunking, resumable-refresh, and thread-membership tests.
- Ruff, format, and `ty` pass for every incoming-main file plus the latest thread-membership correction.
- `git diff --check` passes after the exact-main merge.
- Five new cold-scan, sync-graph, negative-proof, SQLite, and PostgreSQL variants failed before their owning fixes and pass afterward.
- The complete five-file history, membership, mutation, context, and snapshot owning cluster passes `324` tests after the correction.
- The exact independent reviewer probe now retains only the valid root and child; the malformed ancestor, its edit, and its indirect reply are excluded.
- Focused Ruff, formatting, `ty`, Vulture, Tach, module-privacy, and commit hooks pass for `239e9203b`.
- Git author was verified as `Bas Nijholt <bas@nijho.lt>` before every new commit.

## Pending exact-head gates

- Commit and push this crash-handoff successor, then freeze its exact head.
- Fresh CI must complete on the final exact head.
- A fresh independent native Codex correctness review is required on the final exact head.
- Exact-head PostgreSQL owning and stress selections are required.
- Exact-head full pytest is required.
- Exact-head Tach and all-file pre-commit are required.
- Exact-head real-Tuwunel validation is required using isolated persistent evidence.
- Any branch-head change invalidates every review, CI, and live result.

## Completion rule

- Preserve all failure artifacts and exact-source provenance.
- Verify every current GitHub comment against the exact code before changing it.
- Remove this living handoff in a final non-production commit only after all exact-head gates pass.
- That removal changes the exact head, so rerun the required lightweight exact-head checks and confirm no production diff changed.
- Never merge.
