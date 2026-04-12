# Bot Runtime Simplification Plan

## Why This Document Exists

The previous refactor improved file boundaries, but it did not remove enough conceptual complexity.
The main risk now is doing another extraction pass that only redistributes the same complexity again.
This plan exists to keep the next refactor subtractive.

## Problem Statement

One inbound turn is still controlled by multiple places.
Today the main control flow is split across `AgentBot`, `DispatchPlanner`, `ResponseCoordinator`, and edit-specific code in `bot.py`.
Planning and execution are still partially mixed.
Turn persistence still has two overlapping sources of truth.
Conversation identity is still represented by several near-duplicate shapes.

## Refactor Goal

Make one turn easy to trace from ingress to final recorded outcome.
Reduce the number of runtime owners and data models involved in that turn.
Delete overlapping representations instead of adding better wrappers around them.

## Success Criteria

A normal text turn can be traced in one control-flow entrypoint.
Exactly one component decides whether the turn is ignored, routed, handled as a command, rejected, or answered.
The planner has no delivery, AI, or persistence side effects.
Edit regeneration loads one durable turn record instead of reconciling multiple partial records.
`AgentBot` becomes a runtime shell again rather than a partial controller.

## Design Rules

### 1. One Turn Owner

Introduce `TurnEngine` as the only high-level owner of one inbound turn.
`TurnEngine` must sequence `precheck -> normalize -> resolve -> plan -> execute -> record`.
`AgentBot` must stop owning turn sequencing.

### 2. Pure Planner

`DispatchPlanner` must become a pure policy stage.
It may inspect normalized input and resolved context.
It may return a `TurnPlan`.
It must not send messages.
It must not call `ResponseCoordinator`.
It must not update handled-turn state.

### 3. One Durable Turn Record

Replace the current split between `HandledTurnLedger` and persisted run-metadata fallback with one durable `TurnStore`.
`TurnStore` must be the single source of truth for source-event ownership, response linkage, history scope, conversation target, and coalesced prompt metadata.
Edit regeneration must read from `TurnStore` first and should not need to reconstruct ownership from current thread state.

### 4. Separate Conversation Scope From Delivery Placement

Replace the overloaded `MessageTarget` role with two concepts.
`ConversationTarget` should mean stable conversation identity used for locking, session scope, memory, and dedupe.
`DeliveryTarget` should mean Matrix delivery placement used for send and edit behavior.
If the old `MessageTarget` remains temporarily, new code should treat it as a migration shim rather than the target design.

### 5. Keep The Good Seams

Keep `InboundTurnNormalizer`.
Keep `ConversationResolver`.
Keep `DeliveryGateway`.
Those modules already map well to real boundaries in the system.

### 6. Do Not Add Classes That Do Not Delete Code

Do not introduce `TurnEngine`, `CommandExecutor`, `RouteExecutor`, or any other type unless it immediately removes duplicated control flow from existing modules.
A new object must replace an old owner, not sit beside it.

## Target Runtime Shape

```text
Matrix callback
    |
    v
AgentBot
    |
    v
TurnEngine
    |
    +--> InboundTurnNormalizer
    +--> ConversationResolver
    +--> DispatchPlanner
    +--> TurnExecutor
            |
            +--> ResponseCoordinator
            +--> DeliveryGateway
    |
    +--> TurnStore
```

`AgentBot` owns Matrix lifecycle and callback registration.
`TurnEngine` owns sequencing for one turn.
`DispatchPlanner` owns policy only.
`TurnExecutor` owns side effects for the chosen plan.
`ResponseCoordinator` owns response lifecycle mechanics only.
`TurnStore` owns durable turn outcome state.

## What Not To Do

Do not split `ResponseCoordinator` just to create smaller files.
Do not add a new abstraction layer if it still depends on the same large request object and the same collaborators.
Do not preserve both old and new turn-record systems at the end of the refactor.
Do not optimize for line counts.
Optimize for fewer control paths and fewer state representations.

## Refactor Order

### Phase 0

Agree on this plan and treat it as the source of truth for the refactor.
Do not extract more types before agreeing on the end state.

### Phase 1

Introduce `TurnEngine` with no behavior change.
Move the existing turn-sequencing logic out of `AgentBot` and into `TurnEngine`.
Keep `AgentBot` limited to Matrix callback handling, lifecycle, room membership, sync support, and reaction wiring.

#### Phase 1 Checklist

Add a new runtime module for `TurnEngine`.
Prefer a small file such as `src/mindroom/turn_engine.py`.
Do not move planner or response code into that file yet.
Move only sequencing and ingress control flow.

`AgentBot` should keep these responsibilities:

- callback registration in `start()`
- sync lifecycle and startup or shutdown helpers
- room joins, invites, presence, welcome messages, and router overdue-task draining
- redaction handling in `_on_redaction()`
- reaction handling in `_on_reaction()` and `_handle_reaction_inner()`

`AgentBot` should stop owning these turn-sequencing methods:

- `_enqueue_for_dispatch()` from `src/mindroom/bot.py`
- `_dispatch_coalesced_batch()` from `src/mindroom/bot.py`
- `_handle_message_inner()` from `src/mindroom/bot.py`
- `_dispatch_text_message()` from `src/mindroom/bot.py`
- `_handle_media_message_inner()` from `src/mindroom/bot.py`
- `_dispatch_special_media_as_text()` from `src/mindroom/bot.py`
- `_dispatch_file_sidecar_text_preview()` from `src/mindroom/bot.py`
- `_on_audio_media_message()` from `src/mindroom/bot.py`
- `_handle_command()` from `src/mindroom/bot.py`

Phase 1 should also move these small ingress helpers if they are only used by turn sequencing:

- `_precheck_event()`
- `_precheck_dispatch_event()`
- `_requester_user_id()`
- `_requester_user_id_for_event()`
- `_is_trusted_internal_relay_event()`
- `_should_bypass_coalescing_for_active_thread_follow_up()`
- `_has_newer_unresponded_in_thread()`
- `_should_skip_deep_synthetic_full_dispatch()`

Phase 1 should not move edit regeneration yet.
Keep `_handle_message_edit()` in `AgentBot` for now.
It is too entangled with the current dual persistence model and should be handled after `TurnStore` work.

The temporary `TurnEngine` constructor should receive already-extracted collaborators from `AgentBot`:

- `ConversationResolver`
- `InboundTurnNormalizer`
- `DispatchPlanner`
- `ResponseCoordinator`
- `DeliveryGateway`
- `ConversationStateWriter`
- `HandledTurnLedger`
- `ToolRuntimeSupport`
- `MatrixConversationAccess`
- logger, config, runtime paths, and agent name

The target call shape for Phase 1 is:

```text
_on_message() -> TurnEngine.handle_text_event()
_on_media_message() -> TurnEngine.handle_media_event()
CoalescingGate.flush() -> TurnEngine.handle_coalesced_batch()
```

The target internal flow for `TurnEngine.handle_text_event()` in Phase 1 is:

1. Append the live event to conversation access.
2. Skip streamed or invalid text events.
3. Run ingress precheck.
4. Branch edits to the existing `AgentBot._handle_message_edit()` callback.
5. Normalize text.
6. Apply deep synthetic relay gating and interactive text handling.
7. Decide whether to bypass coalescing.
8. Either enqueue into the gate or call the existing dispatch path.

The target internal flow for `TurnEngine.dispatch_text_message()` in Phase 1 is:

1. Normalize the raw or prechecked event into one prepared text event.
2. Call `DispatchPlanner.prepare_dispatch()`.
3. Handle command detection at the turn-engine layer, not in `AgentBot`.
4. Apply backlog suppression and deep synthetic relay suppression.
5. Call `DispatchPlanner.plan_dispatch()`.
6. Branch on the returned plan.
7. For now, call the existing side-effecting planner executor methods.
8. Record handled-turn outcomes exactly as today.

The explicit non-goal for Phase 1 is purity.
It is acceptable that `DispatchPlanner` still executes commands, router relays, and response actions during this phase.
The goal is only to make `AgentBot` stop being the turn controller.

### Phase 2

Make `DispatchPlanner` pure.
Remove `execute_command`, `execute_router_relay`, and `execute_response_action` from `DispatchPlanner`.
Return a `TurnPlan` that is rich enough for a separate executor to perform those actions.

### Phase 3

Introduce `TurnStore`.
Move durable turn recording, response linkage, visible echo linkage, source prompt maps, and edit-regeneration lookup behind that one store.
Delete fallback reconciliation between handled-turn records and persisted run metadata once `TurnStore` is sufficient.

### Phase 4

Split conversation identity from delivery placement.
Introduce `ConversationTarget` and `DeliveryTarget`, or make an equivalent simplification that removes the overloaded role of `MessageTarget`.
Update locking, session scope, memory scope, and delivery code to use the correct target type.

### Phase 5

Re-evaluate `ResponseCoordinator`.
Only split it further if that split deletes duplicated lifecycle paths or collapses the current request-shuffling between team and individual responses.
If no real deletion happens, leave it as one module.

### Phase 6

Delete stale paths, update docs and diagrams, and remove migration shims.
The refactor is not done until the old owners are gone.

## Review Checklist

When reviewing each phase, ask these questions.

Can one normal text turn be traced in one place.
Is there one owner for turn sequencing.
Is the planner side-effect free.
Is there one durable turn record.
Can edit regeneration use the same core execution path as a normal response.
Did the change delete an old owner, or only add a new one.

## Practical Note

A short plan document is the right first step.
The document must be brief, current, and tied to code deletion.
It must not become another architecture essay that the implementation no longer follows.
