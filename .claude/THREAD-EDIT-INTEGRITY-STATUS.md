# Thread edit integrity status

## Branch and pull request

- Branch: `fix/thread-edit-integrity`.
- Pull request: https://github.com/mindroom-ai/mindroom/pull/1641.
- Merge base: `66dd4f4a68bcfd1a5e43b2cac20a1b464f306ab1`.
- Rejected review head: `3f71797848ae0ea8011b0cdb544143f79eb76bbd`.
- Current pushed code head: `09bfb180cf6b9400b9afd09f1a40761726d74fc9`.
- Never amend or force-push.
- Never merge this pull request from the agent task.

## Current gate state

- Fresh native Codex `gpt-5.6-sol` at `xhigh` returned `CHANGES REQUIRED` on exact head `3f71797848ae0ea8011b0cdb544143f79eb76bbd`.
- The review reproduced bundled replacement omission from stale-cleanup projection and relation-bearing events admitted as `/threads` roots.
- Follow-up commit `09bfb180cf6b9400b9afd09f1a40761726d74fc9` fixes both code blockers and invalidates every earlier review, CI, and live result.
- The first exact-`3f7179784` Claude Opus 5 high launch received Vertex `429 RESOURCE_EXHAUSTED` before inference and produced no verdict.
- The tracked living handoff is restored for crash recovery while implementation is active.
- Remove this file before freezing the next exact review and live candidate.
- Real-Tuwunel has not run.

## Current implementation

- `src/mindroom/matrix/replacements.py` owns bundled flattening, identity and scope validation, canonical `(origin_server_ts, event_id)` ordering, and content projection.
- Generic visible-message resolution now collects bundled candidates from every original before canonical validation and projection.
- Stale cleanup therefore sees terminal bundled stream replacements instead of repairing the stale original.
- `/threads` accepts only relation-free roots through canonical `EventInfo.can_be_thread_root`, in addition to state and explicit room-scope rejection.
- Replacement scope validates explicit room IDs as non-empty strings and compares scalar room evidence without hashing untrusted JSON.
- Approval replacement validation requires an object `content.m.new_content` and reads status only from that replacement content.
- SQLite and PostgreSQL SQL owns cache scope, joins, and bytewise tie ordering while shared Python owns Matrix validity and malformed-newest fallback.
- Legacy sidecar persistence and reads revalidate their indexed event owner.
- PostgreSQL schema v3 retains `idx_mindroom_event_cache_event_edits_room_original_ts` as its single narrowing index because query-level `COLLATE "C"` owns equal-timestamp correctness.

## Validation

- The new bundled-terminal stale-cleanup and relation-bearing `/threads` root regressions pass.
- Adjacent visible-resolution, completed-cleanup, and thread-page tests pass.
- Ruff formatting, Ruff lint, `ty`, Vulture, Tach dependency and interface checks, module privacy, commit hooks, and diff checks pass.
- Production source is `+756/-557`, net `+199` against the merge base, satisfying the hard net `<= +200` gate.
- GitHub CI from the rejected head was green except pending Greptile before this code commit invalidated it.
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
