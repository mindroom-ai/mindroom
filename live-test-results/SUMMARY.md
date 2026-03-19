# Live Test Validation Summary

This file intentionally keeps only the current high-signal status for the PR.
The detailed evidence bundle and per-section reports were removed from the PR on purpose.

## Issues Found And Addressed

- `CORE-001`: Doctor mem0 URL mismatch was fixed on this branch.
- `ROOM-005`: Directory visibility reconciliation failure reporting was fixed on this branch.
- `ROOM-006` and `ROOM-007`: Root-space orphan cleanup behavior was fixed on this branch.
- `UI-005`: Dashboard overview blank on initial load was fixed on this branch.
- Agent card pluralization (`1 tools`) was fixed on this branch.
- The local Matrix host-port regression from `8008` to `8108` was fixed on this branch.
- The detailed validation evidence bundle was removed from this PR instead of being kept as merge payload.

## Addressed But Not Fully Revalidated

- `SCH-003` failed during rerun because a conditional schedule was stored as `0 9 * * *` instead of a polling cadence.
- The branch code now rejects invalid conditional parses instead of autocorrecting or silently storing that cron.
- That exact live scheduling flow was not rerun after the final simplification.
- The full 186-item live checklist was not rerun end-to-end after the final fixes on this branch.

## Not Tested Or Environment Blocked

- `CORE-007` was not tested because it requires a hosted pairing flow from `chat.mindroom.chat`.
- `MSG-009` was not tested because it requires network-level reconnect simulation.
- `MEDIA-002` was not tested because Matrix E2EE validation requires a full Element client for key sharing.
- `MEM-007` was not tested because it requires an external git-backed knowledge base.
- `MEM-016` was not tested because the `sentence_transformers` embedder scenario was not available locally.
- Section 8 authorization coverage was not rerun in a dedicated multi-user setup.
- `OAI-007` was not rerun for the exact private or incompatible agent rejection case on the temporary `/v1` instance.
- `OAI-012` was not rerun for the exact auto-model session binding case.
- `INT-003` was not rerun end-to-end for the OAuth-backed integration flow.
- `INT-004` was not rerun for the `matrix_message` integration bucket.
- `INT-006` was not rerun for the `claude_agent` integration bucket.
- `INT-007` was not rerun for attachment boundary behavior.
- `INT-009` was not rerun for the Google configure or reset lifecycle.
- `INT-010` was not tested because it requires a real Home Assistant instance.
- `INT-011` was not tested because it requires a multi-user scope-permutation setup.
- `INT-012` was not rerun for callback stale or mismatched state handling.
- Section 15 SaaS platform flows were not executed because no local Supabase or Stripe-backed SaaS environment was available.
- Local dashboard screenshot reruns were not executed because the Chromium runtime was missing `libglib-2.0.so.0`.
