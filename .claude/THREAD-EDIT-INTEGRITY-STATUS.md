# Thread edit integrity status

Current PR: #1641.
Current pushed implementation head: `add5eb73d8e58e86a9d6d14c1362fecb9080c2e5`.
Current production head: `add5eb73d8e58e86a9d6d14c1362fecb9080c2e5`.
Current production diff: `+782/-606`, net `+176`; hard ceiling is net `+200`.

Exact-`243ec1250` native Codex `gpt-5.6-sol` xhigh review was `CHANGES REQUIRED`.
Commit `add5eb73d` fixes its confirmed v2 sidecar hydration blocker.
Hydrated replacements now require nio-valid message layers and the same replacement relation as their raw preview.
Direct/full history and bundled previews fall back to an older valid candidate.
Cached point projection rejects each hydrated-invalid candidate and asks the backend for the next canonical candidate.
The dead bundled-preview content parser was removed so every visible edit body uses the shared extraction seam.

Validation on the current implementation:

- `69` message-content tests pass.
- `27` edit-history tests pass.
- `28` SQLite edit-cache tests pass.
- The stale edit-sidecar cleanup regression passes.
- The three earlier SQLite snapshot regressions, two malformed-media regressions, and seeded SQLite cache fuzz pass.
- Ruff, formatting, `ty`, Tach dependencies/interfaces, commit hooks, and diff checks pass.
- PostgreSQL variants for malformed and retargeted hydrated sidecars are collected but not yet run.

Claude Opus is advisory-only and never required approval.
No PR #1641 Opus process or queue remains active.
An advisory Claude claim blocks only after deterministic reproduction or independent confirmation.
Fresh exact-head native Codex, GitHub CI, PostgreSQL, full pytest, all-file hooks, and real-Tuwunel remain required.

No PostgreSQL, full pytest, Docker, all-file, or live validation may start without explicit resource-gate ownership.
The current heavy owner is none, and PR #1641 has not claimed it.
All reviews, CI, heavy tests, and live evidence before `add5eb73d` are stale.
Remove this living handoff only in the final freeze commit before exact-head review and validation.
Do not merge, amend, force-push, or use temporary worktrees/evidence.
