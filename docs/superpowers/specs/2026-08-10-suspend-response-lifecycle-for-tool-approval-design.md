# Suspend Response Lifecycle While Tool Approval Is Pending

## Context

MindRoom currently requests Matrix tool approval from inside the Agno tool hook and awaits the decision inline.
The hook runs inside the response operation owned by `ResponseLifecycleCoordinator.run_locked_response`.
Consequently, a long-lived approval retains the per-conversation lifecycle lock, active-turn bookkeeping, typing state, streaming resources, and the live Agno response coroutine.
Human and scheduled turns for the same conversation cannot dispatch until the approval resolves or the response is cancelled.

Issue #1796 requires pending approval to become durable suspended work rather than a wait inside a live response.

## Goals

- Release the response lifecycle lock and stop typing after an approval card is durably emitted.
- Preserve enough state to resume the exact paused tool call after process or configuration reload.
- Serialize approval, denial, and expiry continuations with ordinary turns for the same conversation.
- Complete the original visible response event instead of posting a second response for the same turn.
- Prevent duplicate decisions, recovery passes, or reloads from executing a tool side effect more than once.
- Settle visibly when the original execution entity can no longer be materialized.

## Non-Goals

- Allow later turns to bypass a response lock that remains held.
- Add concurrent mutation within one conversation.
- Redesign approval policy matching or the Matrix approval card format beyond fields required for continuation.
- Add approval support to OpenAI-compatible or voice surfaces that intentionally hide tools requiring Matrix approval.

## Considered Approaches

### Persist an Agno paused-run continuation

Mark functions that may require MindRoom approval as confirmation-gated before Agno executes them.
When Agno produces a paused run, evaluate the exact call against MindRoom policy, persist the continuation, emit the Matrix card, and return a suspended response outcome.
After a decision, materialize the entity again and continue the stored run with the exact recorded `ToolExecution`.

This approach reuses Agno's persisted paused-run representation, which already contains the run ID, session ID, tool-call ID, function name, and arguments.
It also keeps actual tool execution inside Agno's continuation path and existing MindRoom tool hooks.
This is the selected approach.

### Persist a custom callable continuation

The current hook could capture a Python callable plus arguments and arrange for a custom runner to invoke it later.
Callables and their live runtime dependencies are not restartable, and reconstructing them would duplicate Agno's tool-resolution and continuation behavior.
This approach is rejected.

### Keep the response coroutine and weaken lifecycle locking

MindRoom could retain the current awaited hook and let other turns bypass the lifecycle lock while it waits.
That would allow concurrent turns to mutate one conversation and directly violates the issue boundary.
This approach is rejected.

## Architecture

### Approval-gated tool surface

Toolkit assembly will mark a function as requiring Agno confirmation when MindRoom approval policy may require that function.
Static `auto_approve` rules remain ordinary functions.
Script-backed rules are conservatively marked because their decision depends on the exact arguments.
The existing inline approval wait in the tool hook will be removed from normal Matrix response execution so a continued approved call cannot request the same approval again.
Tool before-call and after-call hooks will continue to wrap actual execution.

When a conservatively marked script-backed call evaluates to auto-approved, MindRoom will immediately continue the paused run under the lifecycle already owned by that turn without emitting a card or suspending.
When policy evaluation fails, MindRoom will resolve the exact call as denied and continue the run with the same sanitized failure semantics used today.

### Durable continuation record

The event journal approval storage will gain a continuation record associated one-to-one with an approval ID.
The record will contain the approval ID, card transaction and event identity, room and resolved thread target, response event ID, requesting entity and entity kind, session and Agno run IDs, exact tool-call ID, tool name and arguments, requester identity, execution identity, expiry, decision, and continuation state.
Tool arguments used for execution will be stored exactly while the Matrix card continues to use the existing redacted preview and full-argument delivery rules.

Continuation state will use an explicit state machine:

```text
waiting_for_decision
  -> ready_approved | ready_denied | ready_expired
  -> claimed
  -> completed | terminal_failure
```

The durable decision transition will be first-writer-wins.
The claim transition will be compare-and-set so only one local task or recovery pass owns continuation execution.
Completion will be recorded after the continuation reaches a terminal response outcome.
A claimed continuation left by a stopped process will not be executed again because the process may have crossed the tool side-effect boundary before stopping.
Recovery will use persisted Agno completion when it proves the exact call finished, and otherwise settle the original response with a visible terminal failure describing the uncertain outcome.

### Suspending the response

The agent or team response driver will recognize an Agno paused run and return a distinct suspended outcome rather than treating it as a generic interrupted or completed response.
Before returning, the approval continuation and card must both be durable.
The response runner will persist the turn as `waiting_for_approval`, finalize the current visible event as pending approval, clean up streaming and typing state, and exit `run_locked_response` normally.
Exiting the locked operation releases the lifecycle lock and active-turn signal through the existing coordinator cleanup.
No live Agno agent, team, Matrix client, response task, or waiter future will be retained by the pending approval.
The suspended response will durably hand its admitted source events from the event journal to the continuation record before the response task exits.
That handoff settles the journal callback obligation while the turn record remains non-terminal, so the original source is not replayed and does not remain a live deferral in front of later room events.

### Decision and serialized resume

Matrix approval actions will commit the first valid decision to the continuation record before updating the card.
The decision handler will enqueue a wake-up with a process-local continuation worker and return without running the tool inline in the Matrix callback.
Expiry and startup recovery will enqueue through the same path.

The worker will claim a ready continuation and run it through `ResponseLifecycleCoordinator.run_locked_target_operation` for the stored room and resolved thread target.
That coordinator is the normal per-conversation serializer used by response turns, so a later human or scheduled turn that acquired the lock first completes before the continuation resumes.
The continuation will materialize the stored entity under the stored execution identity, load the paused Agno run by session and run ID, verify the exact stored tool-call identity and arguments, apply only the committed decision to that call, and call Agno's continuation API.

Approved calls execute once through the normal tool hook chain.
Denied or expired calls are returned to Agno as rejected and never invoke the tool body.
The response runner then edits and completes the original response event using the ordinary delivery gateway and post-response effects.

### Reload and recovery

Approval shutdown will stop process-local workers without expiring durable pending approvals merely because the runtime is reloading.
Startup will scan unresolved and ready continuation records.
Unresolved unexpired records will keep their existing approval cards pending and arm expiry processing.
Ready unclaimed records will be enqueued for serialized continuation.
Abandoned claimed records will recover a proven persisted result or settle visibly without invoking the tool again.
Expired records will atomically receive an expiry decision and use the same continuation path as an explicit denial.

If the stored agent or team no longer exists or cannot be materialized, the worker will edit the original response with a visible terminal explanation and mark the continuation `terminal_failure`.
This settlement will not execute the tool.

## Delivery and Conversation Semantics

The original response event remains the sole visible response owned by the source turn.
While suspended, its status indicates that tool approval is pending and typing is off.
Later turns may produce newer Matrix events while the approval remains unresolved.
When the continuation finishes, editing the earlier response preserves one-turn-to-one-response identity even if its final edit appears after later events.

The original source turn remains durably non-terminal while its continuation waits, but its event-journal callback obligation transfers atomically to the continuation record at suspension.
The continuation's terminal response completes the turn record without requiring the original Matrix event to be dispatched again.

## Failure Handling

- Failure to persist the continuation prevents the approval card from becoming actionable and settles the call as denied.
- Failure to send or durably bind the card uses the existing fail-closed approval result and does not suspend the response.
- Failure to edit a decided card does not change the committed decision and remains recoverable for redelivery.
- Failure to materialize the entity produces a visible terminal response and a durable terminal continuation state.
- Failure during resumed generation follows ordinary response error and interrupted-replay handling without releasing the continuation claim for duplicate tool execution.
- Process termination after a continuation claim never releases that claim for another tool execution.
- Recovery reuses a persisted completed Agno result when available and otherwise produces a visible uncertain-outcome failure without re-running the call.

## Testing

Focused tests will cover the following behavior:

- A response that emits an approval card leaves `has_active_response_for_target` false and clears typing after suspension.
- A later human event for the same conversation dispatches while approval remains pending.
- A scheduled event for the same conversation dispatches while approval remains pending.
- Approval, denial, and expiry each enqueue and complete exactly one serialized continuation.
- An approved continuation executes only the stored tool-call ID with the stored arguments.
- Duplicate Matrix decisions, duplicate worker wake-ups, and startup replay do not invoke the tool twice.
- A crash-recovery simulation after continuation claim does not re-run a tool when completion cannot be proven.
- Reload preserves an unresolved approval and restores its expiry or decision continuation.
- Reload after a committed decision resumes the continuation exactly once.
- Removal of the requesting entity settles the original response visibly without executing the tool.
- Streaming and non-streaming response paths both edit the original response event after continuation.
- Existing approval redaction, authorization, policy script, hook ordering, transport, and card-resolution tests remain green.

## Acceptance Mapping

The suspended response outcome releases lifecycle ownership and typing for the first acceptance criterion.
Normal lock acquisition by later human and scheduled turns covers the second and third criteria.
The durable decision state plus continuation worker covers approval, denial, and expiry.
First-writer-wins decisions and compare-and-set claims cover duplicate decision and recovery handling.
Startup scanning and entity materialization failure settlement cover reload behavior.
The focused test matrix explicitly covers lock release, typing cleanup, reload, and duplicate decisions.
