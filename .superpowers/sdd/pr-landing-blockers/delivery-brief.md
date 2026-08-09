# Delivery recovery ordering blocker

Base commit: `81191c6ad44b18c9e7a93c10245e557aa1cc92b6`.

Fix the current `ResponseDelivery.recover()` race in which recovery lists an unacknowledged INITIAL delivery, verifies that FINAL is absent, pauses before the INITIAL Matrix request is accepted, live FINAL delivery succeeds under its distinct transaction ID, and recovery then sends the stale INITIAL placeholder after the final answer.

Requirements:

- Use test-driven development and record the exact RED command and failure before production edits.
- Preserve the current durable outbox, frozen payload, device-adoption, transaction-ID deduplication, membership-fence, and acknowledgement-race semantics.
- Make the INITIAL-versus-FINAL decision atomic or serialized under the existing delivery state owner for one turn.
- Once FINAL is present, claimed, in flight, acknowledged, or otherwise owns visible delivery, INITIAL must never become newly visible afterward.
- A failed INITIAL attempt that was never accepted by Matrix must remain recoverable when no FINAL exists.
- Do not globally serialize unrelated turns or rooms.
- Do not merge the durable send/edit state machines or add a test-only production branch.
- Include the exact adversarial interleaving with a fake Matrix server that deduplicates accepted `(device_id, transaction_id)` pairs.
- Cover SQLite and PostgreSQL wherever the existing response-outbox fixtures support both.
- Run `uv sync --all-extras` in this fresh worktree before verification.
- Run focused tests, Ruff, format check, ty for touched files, Tach if boundaries change, and `git diff --check`.
- Self-review the full base-to-head diff, commit the change, and leave the worktree clean.

Write the implementation report to `.superpowers/sdd/pr-landing-blockers/delivery-report.md` with: RED evidence, design, files changed, verification commands/results, production/test LOC, and remaining concerns.
