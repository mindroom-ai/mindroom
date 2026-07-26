# PR #1641 thread edit integrity handoff

## Exact state

- PR: `#1641`
- Branch: `fix/thread-edit-integrity`
- Production and test correction head: `8c6e4f2eff4ebb8427ac2fb71896cb01a3a40489`
- The published status-only successor contains this file, so resolve its exact SHA from the branch instead of storing a self-invalidating SHA here.
- Base and merge base: `858282afc77adb480fa06cd9e4057d511ff861d5`
- Production diff: `+3367/-1285`, net `+2082`.
- Test diff: `+12731/-1416`, net `+11315`.
- The tracked tree is clean.
- Preserve the three untracked `.claude/TASK-*` files and `artifacts/`.
- Never amend, force-push, merge, or store durable evidence in a temporary directory.

## Latest verified corrections

- `92eb60daf` blocks late encrypted `m.replace` events when their original has a durable redaction tombstone.
- The regression passes on SQLite and PostgreSQL.
- The Greptile P1 thread is fixed, replied to, and resolved.
- `276a0df6e` reconciles mutable bundled replacement observations by the same visible-surface validator used for final selection.
- Equal timestamps use lexicographically greatest event ID.
- Older bundle refreshes cannot replace newer evidence.
- A malformed newest room-message or approval bundle falls back to the older valid replacement.
- Contradictory representations of one bundled edit event ID are quarantined instead of becoming last-write-wins.
- Full resolution, SQLite, and PostgreSQL cover both duplicate-observation arrival orders.
- The new bundle matrix passes `24` room-message cases and `4` approval cases.
- Existing sparse aggregation, bundled redaction fallback, cached conflict, approval, canonical-upgrade, and retained-merge regressions pass.
- The relevant six-file owning and adjacent selection passes.
- Import-graph, Ruff, formatting, `ty`, Tach dependencies/interfaces, commit hooks, and `git diff --check` pass.
- Both independent exact-`7f4003523` Codex reviews reproduced two blockers.
- `8c6e4f2ef` reconciles immutable bundled edit identity before applying the visible-surface validator.
- A same-ID encrypted-to-clear observation with mismatched timestamps is quarantined; a legal equal-timestamp upgrade remains valid.
- Encrypted replacement relations are indexed so redacting an original tombstones a dependent encrypted edit whether the edit arrives before or after the redaction.
- Strict RED failed six identity-before-projection cases and both edit-before-redaction cache backends before production changes.
- Focused GREEN passes `16` exact cases and the wider full-resolution/SQLite/PostgreSQL bundle-redaction matrix passes `73` cases.
- Changed-file pre-commit, Ruff, formatting, `ty`, Tach, module privacy, commit hooks, and `git diff --check` pass.

## Gate status

- Every approval, CI result, PostgreSQL/full suite, all-file hook run, and live run before `8c6e4f2ef` is stale.
- GitHub CI for `8c6e4f2ef` is running.
- Fresh independent exact-head Codex review has not started yet.
- Full pytest, explicit PostgreSQL stress, all-file pre-commit, and real-Tuwunel have not run on `8c6e4f2ef`.
- Real-Tuwunel remains the final exact-head gate after all code and handoff changes.

## Remaining sequence

1. Commit and push this crash-safe handoff with Bas Nijholt author identity.
2. Update the PR body and campaign ledgers to exact `8c6e4f2ef`.
3. Run fresh independent exact-head Codex review and verify every current GitHub comment.
4. Fix only reproduced blockers with strict RED-first regressions.
5. On a stable code head, run full pytest, explicit PostgreSQL stress, Tach, all-file pre-commit, and terminal CI.
6. Remove this handoff in a final commit and restart every exact-head gate.
7. Run the frozen isolated real-Tuwunel validation last and preserve evidence.
8. Never merge.
