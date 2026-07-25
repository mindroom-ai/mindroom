# Thread edit integrity status

## Current state

- PR: `https://github.com/mindroom-ai/mindroom/pull/1641`.
- Branch: `fix/thread-edit-integrity`.
- Exact local, remote, and PR head before the active correction: `8aca4563c3e1d520276335ada6bb26b090fd7cb6`.
- Production source remains `+786/-608`, net `+178`.
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

- Exact-`8aca4563c` native Codex independently found that redacting an explicit edit can leave the same edit visible through the original event's bundled `unsigned.m.relations.m.replace`.
- The claim must be independently reproduced before any production edit.
- If valid, add backend-neutral and SQLite/PostgreSQL regressions at the owning cache seam and reject tombstoned bundled edit event IDs.
- Any production correction must remain under the hard net `+200` source ceiling.

## Required gates after the final correction

- Fresh exact-head native Codex `gpt-5.6-sol` xhigh review.
- Green exact-head GitHub CI.
- Serialized PostgreSQL focused coverage, full pytest, Tach, and all-file pre-commit only after PR #1639 explicitly releases the heavy slot.
- Exact-head real-Tuwunel gate from `/tmp/pr1641-live-tuwunel.md` only while owning the heavy slot.
- Remove this tracked handoff only at the next stable freeze.
- Never merge.

## Durable evidence

- `pr1641_pytest_8aca4563.md` records the exact pytest failure and classification.
- `pr1641_ci_8aca4563.md` records the independent amd64 infrastructure failure and same-head rerun.
- `pr1641_codex_8aca4563.md` is the expected report for the rejected exact-head native review.
- `thread_edit_integrity_agent.md`, `MERGE-GATES.md`, and `RESOURCE-GATE.md` remain the campaign ledgers.
