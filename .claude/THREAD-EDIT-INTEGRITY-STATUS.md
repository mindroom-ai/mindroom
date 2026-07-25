# Thread edit integrity gate status

Updated 2026-07-25 after the exact-`0bbfb0dad` review corrections.

## Exact target

- PR: `mindroom-ai/mindroom#1641`
- Latest production commit: `d6068fd1f1b251c5dfe5e08ec8919f9426722c4e`
- The branch head also contains this tracked living handoff; resolve its exact SHA from local, origin, and GitHub after recovery.
- Base and merge base: `ce8b1a6aea3485ebf3f3b4382c888801175880c2`
- Local, remote, and GitHub heads must agree before any gate is counted.
- The tracked worktree is clean.
- Three pre-existing untracked `.claude/TASK-*.md` notes are user-owned and must remain untouched.

## Active gates

- Fresh native Codex rejected exact `0bbfb0dad7be5567051e7021630d73bdd83f570f` with two independently reproduced correctness blockers.
- Orphan replacements could establish thread membership through `m.new_content`, and plaintext media replacements accepted non-MXC URLs.
- Commits `2295502b163c1169da40f5c5bb9166bf9e72134d` and `d6068fd1f1b251c5dfe5e08ec8919f9426722c4e` fix the owning seams.
- Direct regressions and owning lightweight suites pass.
- Ruff, format, `ty`, Tach, module privacy, Vulture, focused pre-commit, and diff checks pass.
- Normal commit hooks passed with the required `/Users/bas.nijholt/.local/bin/uv`; no hook was skipped.
- All exact-`0bbfb0dad` review, CI, local, all-file, and live evidence is historical after production changes.
- Fresh exact-head native Codex review and GitHub CI are active.
- PR #1666 owns the heavy slot.
- Do not run PostgreSQL fanout, full pytest, all-file hooks, Docker, or real-Tuwunel until #1666 merges and #1641 synchronizes its startup auto-resume fix.

## Live gate

- The prior exact-`8f9920653` launch failed before Tuwunel or MindRoom startup when the allocator was killed with signal 9 under host contention.
- That environment-only evidence is retained under `artifacts/pr1641-8f9920653-live/results/20260725T133755-ed19b8c1/`.
- The persistent live status is `pr1641_live_tuwunel.md`.
- The historical `/tmp/pr1641-live-tuwunel.md` command file is absent.
- Do not guess a replacement command.
- Reuse the campaign's persistent exact-provenance harness and fresh allocator ports after resource ownership is explicit.
- Redirect temporary runtime state into persistent campaign storage and preserve every failure artifact.

## Completion rule

- Every final gate must cover the exact current branch head after #1666 merges and this branch synchronizes it.
- Any head movement invalidates all current evidence.
- Remove this living handoff before declaring the PR merge-ready.
- Never merge.

## Focused and backend validation

- The focused non-PostgreSQL replacement/resolution selection passed `371/371`.
- The broad cache selection completed `457` tests.
- `455` passed directly; only the seeded PostgreSQL trace and PostgreSQL 45-thread fanout hit the global 60-second timeout while unrelated heavy jobs shared the host.
- Both unchanged stress workloads passed immediately with a diagnostic 180-second cap: seeded PostgreSQL `6.80s`, 45-thread PostgreSQL `11.32s`.
- Evidence is retained under `artifacts/pr1641-8f9920653-gates/`.
- No production or test source changed.

## Concurrent exact-head live evidence

- A separate exact-head live attempt reached the intentional restart after 73 realized operations.
- All bots reported stopped, but the process remained alive beyond the harness 20-second deadline and required SIGKILL.
- Failure evidence is retained under `artifacts/pr1641-8f9920653-live/results/20260725T134447-5d851f13/`.
- Exact head `8f99206533af9f4167ab3fff46204de9a2bbb66d` therefore has no live PASS and cannot be declared merge-ready.

## Fresh review fixes in progress

- Both fresh Codex reviewers independently reproduced bundled-replacement sibling loss on redaction.
- One reviewer also reproduced cleanup tail ordering by the original rather than visible edit timestamp.
- Both regressions failed on the pushed head and pass with the local corrections.
- `tests/test_event_cache.py` plus `tests/test_stale_stream_cleanup.py` pass `258/258`, including SQLite and PostgreSQL.
- The bundled-redaction correction is staged.
- The visible-ordering correction is safely retained in Git stash `pr1641-visible-ordering` solely to keep the commits atomic.
- Do not lose or drop that stash before restoring and committing it.
- Normal commit hooks are currently being killed with signal 9 while an unrelated full-xdist run occupies the host.
- The exact staged pre-commit selection passes when run directly; no hook bypass is authorized.

## Second isolated live result

- A second exact-head run realized 203 operations and preserved a complete failure bundle.
- It failed the final response audit because the reply to operation 155 remained `**[Response interrupted]**`.
- Evidence is retained under `artifacts/pr1641-8f9920653-live-clean/results/20260725T135704-c0921f22/`.
- Diagnostics recorded zero cache-coordinator timeouts, zero dispatch-read timeouts, and zero event-loop stalls.
- This is an additional exact-head live blocker.

## Pushed review corrections

- Commit `91538188aa935262e8ac64bcacf235d96aea9e40` preserves surviving bundled replacement candidates after redaction.
- Commit `06b0c03daa1ffdc139a22f8119a9a546cf026a71` orders the visible cleanup tail by accepted edit time.
- Both commits were authored by `Bas Nijholt <bas@nijho.lt>` and passed normal commit hooks.
- Local, remote, and GitHub PR heads now agree at `06b0c03daa1ffdc139a22f8119a9a546cf026a71`.
- Base and merge base remain `ce8b1a6aea3485ebf3f3b4382c888801175880c2`.
- The tracked tree is clean.
- The PR body is current and uses only repository-relative paths.
- Every gate recorded for `8f99206533af9f4167ab3fff46204de9a2bbb66d` is now historical.
- Fresh exact-head CI and two independent native Codex reviews are active.
- PostgreSQL, full pytest, Tach, all-file hooks, and real-Tuwunel must run again on `06b0c03daa1ffdc139a22f8119a9a546cf026a71`.

## Exact-head validation complete

- The owning replacement and cleanup suite passes `258/258`.
- The focused resolution, approval, and media suite passes `372/372`.
- The SQLite/PostgreSQL backend matrix passes `287/287`.
- The PostgreSQL 45-thread fanout passes in `11.57s`, and the seeded concurrent trace passes in `3.81s`.
- Full pytest passes `11836` tests with `54` skipped.
- Tach dependency and interface validation passes.
- Every all-file pre-commit hook passes in a clean detached exact-head worktree.
- GitHub exact-head CI is fully green, including pytest, smoke, both image architectures, plugins, Tach, security, and AI checks.
- The frozen real-Tuwunel trace passes `200` operations in `124.9s` with `45` roots, `135` canonical replies, one MindRoom restart, one outage, and zero cache-coordinator timeouts, dispatch-read timeouts, or event-loop stalls.
- Live cleanup and final exact-source provenance pass.
- The PASS receipt is `artifacts/pr1641-06b0c03da-live/results/receipts/20260725T142213-3eef9a1f.json`.
- No unresolved GitHub review thread exists.
- One fresh independent native Codex correctness review remains active.
- The two earlier native workers returned only source-size summaries after prompt steering, so they are not counted as correctness approvals.

## Exact-06b review rejection

- Fresh independent native Codex returned `CHANGES REQUIRED` on exact `06b0c03daa1ffdc139a22f8119a9a546cf026a71`.
- A state `m.room.message` row could enter snapshot membership resolution, seed a root, and authorize an indirect agent reply even though final candidate filtering rejected the state row itself.
- An edited parent's relation identity remained its original event ID while equal-time ordering keyed the parent by its visible edit ID, so the child edge disappeared and cleanup could select the edited parent as the thread tail.
- Both failures reproduced directly against the exact pushed head.
- The state-membership bug was introduced earlier in this PR by `5d36a92709`.
- The edited-parent ordering failure became reachable through the latest `06b0c03daa` visible-timestamp correction.
- Narrow owning-seam fixes and deterministic SQLite/PostgreSQL plus cleanup regressions pass `131/131`.
- All exact-`06b0c03daa` CI, full, all-file, review, and live evidence is now historical after the working-tree source changes.
- Commit and push the two fixes atomically, then restart every exact-head gate.

## Pushed exact-c828 correction

- Commit `835e45c0b714db87f0e6289dce783302f589d668` rejects state and invalid message rows before snapshot graph membership resolution.
- Commit `c828022eaba9a68e0b4b41e5b27f8241b685db0f` maps original relation targets to their selected visible event IDs before equal-time ordering.
- Both commits use `Bas Nijholt <bas@nijho.lt>` and passed normal commit hooks.
- Local and remote branch heads agree at exact `c828022eaba9a68e0b4b41e5b27f8241b685db0f`.
- The tracked tree is clean.
- The correction changes production by `+23/-8`, net `+15`.
- PR-wide production source is `+1516/-1053`, net `+463`.
- The owning SQLite/PostgreSQL and cleanup selection passes `131/131`.
- Fresh exact-head focused/backend/full/Tach/all-file, GitHub CI, native Codex review, and real-Tuwunel gates are required.

## Exact-0bb review rejection and pushed correction

- Exact head `0bbfb0dad7be5567051e7021630d73bdd83f570f` passed focused validation, SQLite/PostgreSQL backend validation, fixed-width full pytest with `11844` passed and `54` skipped, Tach, and every all-file hook.
- Fresh independent native Codex nevertheless returned `CHANGES REQUIRED` with two directly reproduced blockers.
- An orphan replacement could use an `m.thread` relation in `m.new_content` to certify a root and create point-cache thread index rows despite its original being absent.
- Plaintext `m.image`, `m.audio`, `m.video`, and `m.file` replacements accepted non-MXC transport URLs because only encrypted media URLs were checked.
- Commit `2295502b163c1169da40f5c5bb9166bf9e72134d` makes local history replacements inherit membership only from a resolved original, prevents replacement content from proving a root, and prevents point-cache thread rows from raw replacement content.
- Commit `d6068fd1f1b251c5dfe5e08ec8919f9426722c4e` requires valid MXC URLs for plaintext and encrypted media replacement layers.
- Both commits are authored by `Bas Nijholt <bas@nijho.lt>`, passed normal commit hooks, and are pushed.
- Production commit `d6068fd1f1b251c5dfe5e08ec8919f9426722c4e` is pushed; the tracked living-handoff commit follows it.
- Direct orphan certification, point-index, four-media-type URI, and SQLite malformed-newest fallback regressions pass.
- Owning thread-history, membership, media, reuse, cleanup, cache-mutation, sync, and read-guard selections pass.
- Ruff, format, `ty`, Tach, Vulture, module privacy, focused pre-commit, commit hooks, and diff checks pass.
- The correction changes production by `+16/-11`, net `+5`; PR-wide production is `+1544/-1070`, net `+474`.
- Exact-`0bbfb0dad` review, CI, full, all-file, and live evidence is historical after the code commits.
- Fresh exact-branch-head native Codex review and GitHub CI are active.
- PR #1666 exact `a7e81a81627da89a7c993b96dc2ea71ac81fe184` owns the heavy slot.
- Do not run PostgreSQL fanout, full pytest, all-file hooks, Docker, or real-Tuwunel until #1666 merges and #1641 synchronizes its startup auto-resume fix.
- Never merge.
