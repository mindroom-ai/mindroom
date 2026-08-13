# PR #1807 Legacy Approval Removal Handoff

## Checkout

PR: https://github.com/mindroom-ai/mindroom/pull/1807

Remote branch: `fix/1796-native-approval-continuation`

Expected handoff head: `9cfd05b2bc877c2ee957f5a54aef100bdcbede17`

Expected base at handoff: `dae462e9574bdb099c614574b5b0b4dac75ccbc9`

```bash
git fetch origin
git switch --track origin/fix/1796-native-approval-continuation
git rev-parse HEAD
```

If the branch already exists locally:

```bash
git switch fix/1796-native-approval-continuation
git pull --ff-only
```

Do not rebase or force-push.

Append normal commits and push them to `origin/fix/1796-native-approval-continuation`.

Do not merge the PR.

The maintainer will review and merge it.

## Objective

Fix issue #1796 by using Agno's persisted native tool-confirmation pause as the only execution approval boundary.

Delete the legacy live-waiter approval execution path instead of retaining two parallel approval state machines.

Make the total implementation materially simpler and smaller while preserving crash safety, restart safety, exact-call approval, and normal response lifecycle behavior.

## Product decision

Backward-compatible execution behavior is not required.

Approval-gated tools on surfaces that cannot suspend and resume an Agno run must be hidden or rejected fail-closed.

Do not keep the legacy waiter path for Dynamic Workflow, voice, OpenAI-compatible, RTC, or another non-resumable surface.

The deployment does have users, so existing persisted data must remain readable and safe.

Old pending approval cards may be terminally expired or denied during startup.

Old approval decisions must never cause legacy execution to resume.

Do not delete databases, require empty state, fail startup on old rows, or reinterpret an old card as permission to run a tool.

## Why this redesign is required

The current exact diff is 27 files and roughly `+4699/-34`.

Production is roughly `+2500/-32`, while tests add roughly 2,171 lines.

The additive shape is real, not merge noise.

The PR currently layers a durable continuation protocol beside the old live waiter protocol.

It splits ownership across the continuation database, approval-card journal, response outbox, stale-stream recovery, response lifecycle, and in-memory retry tasks.

Repeated zero-tolerance reviews found new crash gaps at those boundaries.

## Intended architecture

One supported Matrix approval flow:

```text
model run
  -> Agno returns a persisted paused run with exact tool-call IDs and arguments
  -> MindRoom persists source-keyed continuation ownership
  -> MindRoom publishes durable Matrix approval cards
  -> response lifecycle ends and releases typing plus conversation lock
  -> decision, denial, or expiry makes the continuation ready exactly once
  -> normal per-conversation serializer claims it
  -> normal stoppable response lifecycle calls Agno acontinue_run
  -> normal final delivery, hooks, tool traces, summaries, and post-effects run
```

The approval-card journal remains the durable Matrix transport record.

The continuation store remains the durable paused-run owner.

There must be one coordinator for card publication and one coordinator for continuation lifecycle settlement.

No live Future may wait for a human decision.

No conversation lock or response coroutine may remain alive while approval is pending.

## Delete the legacy execution path

Remove or reduce these concrete areas after proving callers are migrated:

- `ApprovalManager.request_approval` and `_LiveApprovalWaiter` ownership.
- Live waiter maps, futures, waiter cancellation, waiter timeout, and waiter bind/recovery helpers in `approval_manager.py`.
- `request_tool_approval_for_call` as an execution gate.
- `_maybe_block_for_tool_approval` from the tool-hook execution chain.
- `native_approval_continuation` and `native_approval_continuation_active` once there is no legacy hook to bypass.
- `_CURRENT_TOOL_CALL_ID` patching if it has no non-legacy consumer.
- Duplicate live versus detached send/bind/expiry/recovery orchestration.
- Tests that exist only to preserve live-waiter behavior.

Keep the durable card schema and startup reader capable of consuming old rows.

Refactor shared card publication, acknowledgement, resolution, expiry, and startup settlement rather than deleting the journal substrate.

## Non-resumable surfaces

Audit every direct `Agent(...)`, `create_agent(...)`, and toolkit-construction surface.

Normal Matrix agent and team response runners must mark gated functions with Agno `requires_confirmation` and support native pause continuation.

Non-resumable surfaces must not expose approval-gated functions.

Known surfaces to verify:

- Dynamic Workflow ephemeral participants construct `Agent(...)` directly.
- Dynamic Workflow nested room-agent participants do not own a top-level response continuation.
- OpenAI-compatible requests already prune tools that may require approval.
- Matrix RTC and voice already hide tools needing text approval UI, but must be reverified after legacy deletion.
- Cached or direct agent instances must not accidentally expose gated tools outside a continuation-capable response runner.

Prefer one explicit capability at agent/toolkit construction, such as whether native approval suspension is supported.

Do not reuse an unrelated flag to infer this capability.

## Existing data migration behavior

Add tests using legacy-format stored approval-card rows with no continuation metadata.

Required outcomes:

- Startup succeeds with the old rows present.
- An unresolved old card becomes visibly terminal when Matrix transport is available.
- A temporary Matrix failure leaves the old card recoverable for a later startup retry.
- A recorded old decision may have its existing visible resolution redelivered, but it never runs a tool.
- Unknown or malformed old rows fail closed and do not crash the runtime.
- Existing current-format continuation rows remain recoverable or are cleanly terminalized.
- Schema changes are additive/tolerant, with no destructive migration.

## Verified current-head blockers

The independent exact-head review at `9cfd05b2b` returned `CHANGES REQUIRED`.

Treat every item below as a blocker, not optional cleanup.

1. Approval waiting messages use ordinary `STREAM_STATUS_PENDING`.

   Startup stale-stream cleanup runs before continuation recovery, terminalizes a valid pending approval, and may auto-resume a duplicate model turn.

   Give approval waiting messages a distinct durable classification or make stale cleanup consult continuation ownership before editing or resuming.

2. Cancellation inside `_suspend_for_approval` bypasses normal cancellation settlement.

   Cancellation during policy evaluation, waiting-message delivery, callback, or card publication can leave `publishing` or partial `pending` ownership stranded.

3. INITIAL response outbox acknowledgement and continuation event binding are separate commits.

   A crash between them leaves a visible waiting event whose continuation lacks its event ID.

   Recovery currently sends a second terminal message instead of editing the waiting event.

4. Continuation resumption bypasses `ResponseAttemptRunner`, `StopManager`, and the full `ResponseLifecycle`.

   An approved continuation cannot be stopped normally.

5. The waiting placeholder is finalized as a successful response.

   Hooks and summaries can see `Waiting for approval`, while the real continued answer skips normal hooks, tool traces, interactive registration, summaries, and post-effects.

6. Failure is persisted before its terminal edit succeeds.

   A failed edit leaves a nonrecoverable row with a visibly pending response.

7. A crash after FINAL outbox enqueue but before continuation completion leaves `claimed`.

   Startup may overwrite the already-durable successful final payload with a failure payload.

   Reconcile the response outbox before deciding a claimed continuation failed.

8. A configured but permanently failed agent bot is retried forever.

   Pass permanent-failure availability into continuation dispatch and use the router to settle visibly.

9. Team continuation setup can leak member runtime databases when scope opening raises before cleanup ownership begins.

   Put scope opening inside the cleanup `try/finally` and add a throwing-scope regression.

## Simplification targets

Do not merely fix the nine blockers on top of the current structure.

First collapse ownership boundaries and delete legacy code.

Concrete duplication to remove:

- Initial and chained pause handling duplicate tool identification, approval policy evaluation, waiting text, call construction, card publication, and card attachment in `response_runner.py`.
- Live and detached approval handling duplicate card claim, send, bind, cancellation, expiry, response resolution, and recovery in `approval_manager.py`.
- Expiry ownership currently exists in both the approval manager and approval transport.

Extract only small focused units with immediate use.

Keep `bot.py` and `orchestrator.py` as lifecycle wiring shells.

The final production diff should show meaningful deletion.

Do not optimize for an arbitrary line target, but explain any large remaining additive subsystem.

## TDD order

Write and run each regression before changing its production path.

Suggested order:

1. Legacy-data startup and fail-closed migration tests.
2. Non-resumable surfaces hide gated tools.
3. Remove live-waiter execution and its tests.
4. Consolidate card publication plus expiry ownership.
5. Distinct approval-waiting restart classification.
6. Suspension cancellation and publish/bind crash gaps.
7. Resume through normal stop-aware response lifecycle and final effects.
8. FINAL outbox reconciliation and failure delivery durability.
9. Permanent entity failure and team scope-open cleanup.
10. Delete redundant helpers, branches, tests, imports, and allowlist entries.

## Verification

In a fresh checkout first run:

```bash
uv sync --all-extras
```

Run focused approval and response suites throughout:

```bash
uv run pytest \
  tests/test_approval_continuation_store.py \
  tests/test_tool_approval.py \
  tests/test_tool_hooks.py \
  tests/test_response_runner_agent.py \
  tests/test_response_runner_focused.py \
  tests/test_response_turn.py \
  tests/test_streaming.py \
  tests/test_ai_user_id.py \
  tests/test_team_media_fallback.py \
  tests/test_agents.py \
  tests/test_sync_task_cancellation.py \
  tests/test_orchestrator_runtime.py \
  tests/test_bot_reactions_approvals.py \
  tests/test_dynamic_tool_continuation_delivery.py \
  -q -n 0 --no-cov --disable-warnings --maxfail=1
```

Before pushing the final state:

```bash
uv run pytest -q --disable-warnings --maxfail=1
uv run pre-commit run --all-files
git diff --check
```

Then obtain a fresh independent zero-tolerance review of the exact pushed head against the live main base.

Wait for all GitHub CI and AI review checks.

Address every valid review comment.

Do not merge.

## Current verified state

At handoff, the branch is clean, pushed, and GitHub reports the PR mergeable.

The affected local suite and all pre-commit hooks passed after the most recent main merge.

Those passing tests do not cover the cross-owner blockers listed above.

The PR body currently claims an independent approval, but the latest exact-head review supersedes that claim with `CHANGES REQUIRED`.

Update the PR body before declaring it ready.

Remove this handoff document from the branch before final merge.
