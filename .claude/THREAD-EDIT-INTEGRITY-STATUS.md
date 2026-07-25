# Thread edit integrity status

## Current state

- PR: `https://github.com/mindroom-ai/mindroom/pull/1641`.
- Branch: `fix/thread-edit-integrity`.
- Exact pushed code head: `a5a26be16c33c7684f14a9b753b40a4445b14d73`.
- Production source is `+966/-766`, net `+200` against exact `origin/main` `5f062224a1f490a91a72c555bf2fa0ca59c096b3`.
- Git author and committer must resolve to `Bas Nijholt <bas@nijho.lt>` before every commit.
- Never amend, force-push, merge, or open the PR as draft.
- Preserve the three untracked `.claude/TASK-*.md` files.

## Active pytest correction

- Exact GitHub pytest job `89649000301` failed sixteen edit-regeneration tests after `11661` passed.
- The production replacement validator is correct.
- The intended-valid tests construct complete `nio.RoomMessageText` events and then replace `event.source` with incomplete envelopes.
- The streaming case additionally omits `msgtype` from the outer edit content and `m.new_content`.
- Exact local reproduction of `tests/test_edit_response_regeneration.py::test_bot_regenerates_response_on_edit` fails with zero regeneration calls.
- Correct only the stale valid-edit fixtures.
- Run all sixteen exact failures before the next push.
- Commit the fixture correction separately, push normally, then freeze on another small status-removal commit.

## Independent review finding

- Exact-`8aca4563c` native Codex returned `CHANGES REQUIRED` with two blockers.
- Redacting an explicit edit leaves the same edit visible through the original event's bundled `unsigned.m.relations.m.replace`.
- A SQLite probe independently reproduces `$edit` surviving latest-edit lookup after a successful redaction removes its explicit row.
- A wrong-room inline edit independently reproduces one regeneration when explicit source room `!other:example.org` disagrees with authoritative room `!room:example.org`.
- Add backend-neutral and SQLite/PostgreSQL regressions at the owning cache seam and reject tombstoned bundled edit event IDs.
- Pass the authoritative room through direct extraction and reject conflicting explicit room evidence.
- Any production correction must remain under the hard net `+200` source ceiling.

## Resolved current-main conflict

- Merge commit `8a384affc9fb577d732cb4987b5aed12f0885680` incorporates exact `origin/main` head `5f062224a1f490a91a72c555bf2fa0ca59c096b3`.
- The only content conflict was `_bulk_scan_thread_event_sources`.
- The resolution preserves main's `max_scan_pages` validation and the branch's canonical `ThreadEditCandidatesByOriginalEventId`.
- All three bulk-backfill tests and Tach pass on the merge.
- The merge commit is pushed.

## Active blocker corrections

- Commit `791cf005c` rejects explicit wrong-room edits before extraction and passes authoritative `room.room_id` from `EditRegenerator`.
- All `93` edit-regenerator and message-content tests pass.
- The active cache correction transactionally removes a redacted edit from the original event's bundled replacement metadata in both backends.
- Cache admission also treats bundled edit IDs as tombstone candidates, so a later original carrying a redacted bundle cannot resurrect it.
- Backend-neutral regressions cover latest-edit, point, snapshot, thread-row, approval, and redact-before-store surfaces.
- All `22` backend-neutral semantics tests, both corrected SQLite projections, and five adjacent SQLite redaction tests pass.
- PostgreSQL execution remains closed behind the shared heavy owner.
- Projected production source is `+833/-634`, net `+199` against current `origin/main`.

## Required gates after the final correction

- Fresh exact-head native Codex `gpt-5.6-sol` xhigh review.
- Green exact-head GitHub CI.
- Serialized PostgreSQL focused coverage, full pytest, Tach, and all-file pre-commit only after PR #1646 explicitly releases the heavy slot.
- Exact-head real-Tuwunel gate from `/tmp/pr1641-live-tuwunel.md` only while owning the heavy slot.
- Remove this tracked handoff only at the next stable freeze.
- Never merge.

## Durable evidence

- `pr1641_pytest_8aca4563.md` records the exact pytest failure and classification.
- `pr1641_ci_8aca4563.md` records the independent amd64 infrastructure failure and same-head rerun.
- `pr1641_codex_8aca4563.md` is the expected report for the rejected exact-head native review.
- `pr1641_codex_8aca_reproduction.md` records independent reproduction of both blockers.
- `thread_edit_integrity_agent.md`, `MERGE-GATES.md`, and `RESOURCE-GATE.md` remain the campaign ledgers.

## Generic sidecar-owner correction

- Exact implementation commit `e994e7f46333f17dea4914a0abc7310bc766caf6` fixes a verified CodeRabbit blocker.
- The cache contract allows only `m.room.message` events to own durable sidecar plaintext.
- Generic timeline events now fail both new ownership registration and legacy-reference revalidation at the shared `event_mxc_urls` seam.
- Pure selector and SQLite backend regressions pass all four focused cases.
- Ruff, formatting, type, Vulture, Tach, module privacy, diff, and commit hooks pass.
- Production source is `+813/-613`, net `+200` against merge base `5f062224a1f490a91a72c555bf2fa0ca59c096b3`.
- The exact-`dfceccb29` native review and CI became stale when this correction moved the head.
- Fresh exact-head native Codex and GitHub CI are required after this status commit is pushed.
- PostgreSQL, full pytest, all-file hooks, Docker, and real-Tuwunel remain closed behind PR #1646.

## Exact-6db requester and relation-ownership correction

- Fresh exact-`b6f7d4a25` Codex review independently reproduced two blockers.
- State and explicit wrong-room message-shaped events entered sync thread handling, page-local relation ownership, event-to-thread indexes, edit indexes, and redaction dependencies.
- A late original carrying a tombstoned bundled replacement was dropped instead of retained unedited.
- The superseded exact-`dfceccb29` review also independently reproduced cross-requester edit regeneration, relation-bearing cached roots, and live resurrection of tombstoned bundled edits.
- Commit `cb3a21515610711f0390d70bc9ac050519c13b5c` binds regeneration to the durable requester.
- Commit `6dbf6fbf555026708feb2182297087d659d73f27` centralizes non-state room scope, rejects invalid relation ownership at every derived-index seam, sanitizes bundled replacements without dropping the original, and filters durable tombstones during live projection.
- Page-local invalid message-shaped events retain only sanitized room-level metadata, so same-page redactions cannot invalidate a thread.
- Five exact SQLite and sync regressions failed before the correction and pass after it.
- The requester, poisoned-root, live bundled-redaction, invalid-index, and page-local redaction regressions pass.
- All `159` focused replacement, reuse, edit-regenerator, message-content, and media parsing tests pass.
- The non-PostgreSQL `tests/test_event_cache.py` selection and `21` focused approval tests pass.
- Ruff, formatting, `ty`, Tach dependency/interface checks, module privacy, diff checks, and both commit hook runs pass.
- PostgreSQL was not run locally because PR #1646 owns the serialized heavy slot.
- Every approval, CI result, and live result before exact `6dbf6fbf5` is stale.
- Fresh exact-head Codex review, GitHub CI, PostgreSQL fanout, full pytest, all-file hooks, and real-Tuwunel remain required.
- Merge gate: CLOSED.

## Exact source requester correction

- The PR #1646 real-Tuwunel ledger independently reproduces a valid coalesced edit whose source requester differs from the turn anchor requester.
- The exact-`dfceccb29` anti-hijack finding requires binding an edit to the requester of the exact source event, not the anchor requester.
- Commit `a5a26be16c33c7684f14a9b753b40a4445b14d73` adds `TurnRecord.requester_id_for_source()` as the durable source-ownership seam.
- Solo turns use the turn requester, coalesced turns use persisted per-source metadata, and missing or mismatched coalesced ownership fails closed.
- Exact regressions cover a valid non-anchor requester, a cross-requester hijack, and incomplete ownership metadata.
- All `27` edit-regenerator tests, all `104` handled-turn and turn-store tests, and the `160`-test replacement/reuse/edit/media selection pass.
- Ruff, formatting, `ty`, Tach dependencies/interfaces, and diff checks pass.
- Production remains at the hard source ceiling: `+966/-766`, net `+200`.
- The source correction is pushed; commit this handoff, refresh external ledgers, then start exact-head gates.
