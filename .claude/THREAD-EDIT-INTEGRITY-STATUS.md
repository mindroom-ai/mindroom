# Thread edit integrity status

Current PR: #1641.
Current pushed head: `6bcc77fb8`.
Current production head: `6bcc77fb8`.
Current production diff: `+786/-608`, net `+178`; hard ceiling is net `+200`.

Exact-head GitHub pytest failed six bundled-edit regressions.
Both cache wrappers returned `None` when no explicit indexed edit existed, so they no longer considered a valid replacement bundled on the original event.
That production regression broke SQLite and PostgreSQL cache selection, cached point and snapshot reads, and approval startup cleanup.
The explicit cached row is already validated independently by `load_latest_edit_row`.
Both wrappers now select across that validated row and the original event's bundled candidate.
The thread-preview failure was a stale test fixture whose downloaded sidecar contained bare visible content instead of the full outer replacement payload that `prepare_large_message` uploads.
The fixture now carries the required canonical `m.new_content` and unchanged `m.relates_to`.
All four runnable exact failed tests pass locally.
The full message-content and Matrix-message-tool files pass with `149` tests.
Ruff, format, `ty`, targeted pre-commit, Tach dependencies/interfaces, module privacy, and diff checks pass.
The two PostgreSQL cases remain delegated to GitHub CI because PR #1641 does not own the heavy slot.

Exact-`b06bdcb3c` native Codex `gpt-5.6-sol` xhigh review confirmed the cache API blocker and independently classified the thread-preview failure as a stale fixture.
Its verdict is stale after `6bcc77fb8` but remains useful evidence for the rejected head.
Claude Opus is advisory-only and never a required approval.
No PR #1641 Opus process or queue is active.

Do not start PostgreSQL, full pytest, Docker, all-file hooks, or real-Tuwunel until the resource ledger explicitly grants PR #1641 the heavy slot.
The resource ledger currently names PR #1646 as owner.
Fresh exact-head native Codex and GitHub CI are required on a new frozen documentation head.
PostgreSQL, full pytest, all-file hooks, and real-Tuwunel remain required after resource-slot ownership.

Preserve the three untracked task prompts.
Remove this living handoff only in the final freeze commit before exact-head review and validation.
Do not merge, amend, force-push, or use temporary worktrees or evidence.
