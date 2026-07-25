# Thread edit integrity status

## Branch and pull request

- Branch: `fix/thread-edit-integrity`.
- Pull request: https://github.com/mindroom-ai/mindroom/pull/1641.
- Merge base: `66dd4f4a68bcfd1a5e43b2cac20a1b464f306ab1`.
- Rejected review head: `50bb003fb3408f052e6f97d1b7d189f3aece92c0`.
- Current pushed code head: `5aa167b24aa8fa7f2bbb7a61cfbd72aac356aa5c`.
- Never amend or force-push.
- Never merge this pull request from the agent task.

## Current gate state

- Fresh native Codex `gpt-5.6-sol` at `xhigh` returned `CHANGES REQUIRED` on exact head `50bb003fb3408f052e6f97d1b7d189f3aece92c0`.
- The review reproduced an unhashable explicit `room_id` crash and acceptance of approval replacements without an object `m.new_content`.
- Follow-up commit `5aa167b24aa8fa7f2bbb7a61cfbd72aac356aa5c` fixes both code blockers and invalidates every earlier review, CI, and live result.
- The tracked living handoff remains only for crash recovery while implementation is active.
- Remove this file before freezing the next exact review and live candidate.
- Fresh native Codex and explicit Claude Opus 5 high reviews have not started on the corrected head.
- Real-Tuwunel has not run.

## Current implementation

- `src/mindroom/matrix/replacements.py` owns bundled flattening, identity and scope validation, canonical `(origin_server_ts, event_id)` ordering, and content projection.
- Replacement scope now validates explicit room IDs as non-empty strings and compares scalar room evidence without hashing untrusted JSON.
- Approval replacement validation now requires an object `content.m.new_content` and reads status only from that replacement content.
- Missing or non-object approval replacement content falls back to an older valid edit or the original status.
- SQLite and PostgreSQL latest-edit SQL owns cache scope, joins, and bytewise tie ordering while shared Python owns Matrix validity and malformed-newest fallback.
- Legacy sidecar persistence and reads revalidate their indexed event owner, including state and explicit wrong-room rejection.
- Full history, bundled preview, point lookup, snapshots, approval lookup, sidecar hydration, and cleanup consume the shared replacement seam.
- PostgreSQL schema v3 retains `idx_mindroom_event_cache_event_edits_room_original_ts` as its single narrowing index because query-level `COLLATE "C"` owns equal-timestamp correctness.

## Validation

- Focused replacement semantics, approval fallback, and SQLite cache coverage pass.
- The corrected focused selection reports `24 passed`.
- Approval startup fallback reports `6 passed`.
- Ruff formatting, Ruff lint, `ty`, Vulture, Tach dependency and interface checks, module privacy, commit hooks, and diff checks pass.
- Production source is `+752/-557`, net `+195` against the merge base, satisfying the hard net `<= +200` gate.
- No PostgreSQL, full pytest, Docker, all-file pre-commit, or real-Tuwunel run has been started for the corrected head.

## Required next steps

- Commit and push this living status.
- Refresh the PR body and campaign evidence with the exact corrected head and source counts.
- Inspect current GitHub CI and AI feedback against the exact code.
- Remove this living handoff and push the final freeze commit.
- Start fresh exact-head native Codex `gpt-5.6-sol` at `xhigh`.
- Start fresh Claude with explicit `--model=claude-opus-5 --effort=high` only when an Opus slot is free.
- Claim the serialized heavy resource gate before PostgreSQL fanout, full pytest, all-file hooks, Docker, or real-Tuwunel.
- Read the complete live instructions immediately before exact-head real-Tuwunel validation.
- Preserve every live artifact in the persistent campaign or repository artifact area.
- Restart all review and live gates if the branch head changes.
- Never merge.
