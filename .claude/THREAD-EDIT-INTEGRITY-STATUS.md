# Thread edit integrity status

Current PR: #1641.
Current pushed head: `b06bdcb3c2d0706e9824a42113fed8a80a9552f9`.
Current production head: `add5eb73d8e58e86a9d6d14c1362fecb9080c2e5`.
Current production diff: `+782/-606`, net `+176`; hard ceiling is net `+200`.

Exact-head GitHub pytest failed six bundled-edit regressions.
Both cache wrappers return `None` when no explicit indexed edit exists, so they no longer consider a valid replacement bundled on the original event.
The same root cause breaks SQLite and PostgreSQL cache selection, cached point and snapshot reads, thread preview hydration, and approval startup cleanup.
The explicit cached row is already validated independently by `load_latest_edit_row`.
The narrow correction is to select across that validated row and the original event's bundled candidate in both backend wrappers.

Exact-`b06bdcb3c` native Codex `gpt-5.6-sol` xhigh review is active read-only.
Its verdict will be stale after the correction but remains useful evidence for this rejected head.
Claude Opus is advisory-only and never a required approval.
No PR #1641 Opus process or queue is active.

Do not start PostgreSQL, full pytest, Docker, all-file hooks, or real-Tuwunel until the resource ledger explicitly grants PR #1641 the heavy slot.
The resource ledger currently names PR #1646 as owner.
After a new stable pushed head, fresh exact-head native Codex, GitHub CI, PostgreSQL, full pytest, all-file hooks, and real-Tuwunel are required.

Preserve the three untracked task prompts.
Remove this living handoff only in the final freeze commit before exact-head review and validation.
Do not merge, amend, force-push, or use temporary worktrees or evidence.
