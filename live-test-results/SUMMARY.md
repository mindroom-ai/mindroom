# Live Test Validation Summary

This file intentionally keeps only the items that failed, were not rerun, or were not testable in the available environments.
The detailed evidence bundle and per-section reports were removed from this PR on purpose.

## Did Not Succeed

- `SCH-003` failed during rerun because the conditional schedule was stored as `0 9 * * *` instead of a polling cadence.
- The branch code was changed afterward to reject invalid conditional parses instead of silently storing that cron.
- That exact live scheduling flow was not rerun after the code change.

## Not Fully Retested

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
