# Terminal Delivery Contract Closure Design

**Goal:** Remove the remaining legacy bridges and finish the terminal-delivery migration so the response lifecycle has one semantic source of truth and no fallback path reconstructs meaning from partial facts.

**Status:** Proposed closure design for the post-migration cleanup pass.

## Why This Exists

The first migration pass introduced the right model:
- `FinalDeliveryOutcome` for canonical terminal semantics
- `TurnDeliveryResolution` for caller-facing decisions
- gateway-owned terminal hook emission

That got the main lifecycle onto the new contract.
It did not yet guarantee that every old bridge was gone.

The remaining risk is not "many random bugs."
The remaining risk is that some fallback, exception, or adapter path can still bypass the canonical model or reconstruct semantics from partial facts.

That is unacceptable for this codebase.
There are no compatibility constraints that justify keeping those bridges alive.
They were migration scaffolding.
Now they should be deleted.

## Design Decision

We will not preserve legacy recovery bridges.

The codebase should not keep:
- semantic coercion from raw `DeliveryResult`
- caller-facing `str | None` response APIs
- exception paths that bypass `FinalDeliveryOutcome`
- fallback success/failure inference from event-id truthiness
- hand-built `TurnDeliveryResolution` values in runtime code outside the canonical projection helper

If a path cannot produce a typed canonical result, that path is incomplete and must be refactored until it can.

## Closure Principles

1. Deletion beats containment.
If a bridge exists only because the migration was not finished yet, remove it instead of fencing it off.
2. Passing tests are necessary but insufficient.
The closure pass is complete only when tests, grep audits, and file-by-file boundary inspection all agree.
3. Transport-specific projection is allowed only at transport boundaries.
That exception applies to adapters such as OpenAI-compatible SSE.
It does not justify multiple semantic contracts inside the response lifecycle.
4. Review comments do not become work items automatically.
Each claimed bug must be checked against current `HEAD`.
If it is stale, we do not reopen the old path just to satisfy a stale review artifact.

## Target End State

At the end of this closure pass:

1. `FinalDeliveryOutcome` is the only semantic terminal state model.
2. `TurnDeliveryResolution` is the only caller-facing response result.
3. `delivery_gateway.py` is the only terminal-hook owner.
4. No delivery-stage exception bypasses the typed contract.
5. No caller persists `turn_completion_event_id` where `response_identity_event_id` is required.
6. No preserved visible stream loses interactive follow-up metadata.
7. No streamed re-edit failure can physically delete a visible reply that the outcome claims to preserve.
8. Recorder persistence, replay persistence, Matrix delivery, and OpenAI-compatible SSE all follow one explicit final-text authority rule, with any adapter-specific projection limitations documented and tested.

## Audit Scope

The closure pass must explicitly inspect every file that can still leak terminal semantics:

- `src/mindroom/final_delivery.py`
- `src/mindroom/delivery_gateway.py`
- `src/mindroom/response_lifecycle.py`
- `src/mindroom/response_runner.py`
- `src/mindroom/post_response_effects.py`
- `src/mindroom/streaming.py`
- `src/mindroom/ai.py`
- `src/mindroom/api/openai_compat.py`
- `src/mindroom/bot.py`
- `src/mindroom/turn_controller.py`
- `src/mindroom/edit_regenerator.py`
- `src/mindroom/commands/handler.py`
- `src/mindroom/teams.py`

The closure pass is not done if any of those files still contain a semantic bridge, even if tests happen to pass.

## Non-Goals

This closure pass is not for:
- unrelated refactoring outside response delivery
- general tool/runtime cleanup
- changing user-visible behavior that is not necessary to close the contract
- preserving old behavior solely because tests used to encode it

## Canonical Ownership Rules

### `final_delivery.py`

Owns:
- the terminal state set
- per-state policy
- `visible_response_event_id`
- `response_identity_event_id`
- `turn_completion_event_id`
- handledness
- retryability
- post-effect eligibility
- interactive eligibility

Must not depend on caller-specific branching.

### `delivery_gateway.py`

Owns:
- mapping transport facts into `FinalDeliveryOutcome`
- terminal hook emission
- cleanup outcomes
- suppression cleanup semantics
- non-streaming final send/edit failure semantics
- preserved visible stream semantics

Must not:
- return a canonical non-success outcome without running terminal hook emission when hook emission is required
- emit terminal hooks before the cleanup result is known
- delete a visible streamed reply in a path that returns a preserved-stream outcome

### `response_lifecycle.py`

Owns:
- post-effect application against canonical outcomes
- projection to `TurnDeliveryResolution`

Must not:
- return `str | None`
- reinterpret terminal semantics
- select a different event identity than the canonical policy table

### Callers

Includes:
- `response_runner.py`
- `bot.py`
- `turn_controller.py`
- `edit_regenerator.py`
- command/skill entrypoints

May use only:
- `TurnDeliveryResolution`
- `should_mark_handled`
- `response_identity_event_id`
- `visible_response_event_id`
- `turn_completion_event_id`
- `retryable`

Must not:
- infer success from event-id truthiness
- persist `turn_completion_event_id` as the durable response link
- rebuild semantic meaning from partial transport facts
- construct `TurnDeliveryResolution(...)` directly in runtime code

## Explicit Closure Targets

### 1. Delete Legacy Semantic Bridges

The following patterns must be removed from migrated delivery paths:
- `_coerce_final_delivery_outcome()` for runtime semantics
- `resolve_response_event_id()` or any equivalent single-event-id projector used for control flow
- `DeliveryResult`-driven reconstruction of terminal meaning

If a fallback path still exists after this pass, it must preserve only raw transport facts and immediately convert them at the gateway boundary.
It may not project business semantics on its own.

### 2. Make Caller Semantics Uniform

The caller-facing contract is complete only when:
- `ResponseLifecycle.finalize()` returns `TurnDeliveryResolution`
- `ResponseRunner.generate_response()` returns `TurnDeliveryResolution`
- bot/controller/regenerator/command callers consume only `TurnDeliveryResolution`
- no wrapper returns `str | None` for response lifecycle control flow

Compatibility wrappers are not acceptable here.

### 3. Close Suppression Cleanup Semantics

`suppression_cleanup_failed` must remain inside `FinalDeliveryOutcome`.

That means:
- no `SuppressedPlaceholderCleanupError` after delivery coordination has started
- no terminal cancellation hook emission before cleanup success/failure is known
- retryability comes from the canonical policy row, not from exception control flow

### 4. Preserve Visible Streams Physically And Semantically

If an outcome returns any preserved-stream state, then:
- the visible event must still exist
- `option_map` and `options_list` must survive if the visible response is interactive
- post-effects must register interactive follow-up when policy allows it

No path may redact a visible stream event and then return `kept_prior_visible_stream_after_*`.

### 5. Canonical Final-Text Authority

The canonical assistant text rule is:
- accumulated streamed assistant text is provisional
- if `RunCompletedEvent.content is not None`, including `""`, it replaces prior assistant text as the canonical final assistant text
- if `RunCompletedEvent.content is None`, accumulated assistant text remains canonical

This rule must drive:
- recorder persistence
- interrupted replay persistence
- Matrix final delivery text
- team-response final text persistence

OpenAI-compatible SSE cannot retroactively rewrite already-emitted deltas.
That is an adapter constraint, not a reason to keep multiple semantic sources of truth.

Therefore the SSE adapter must define and test one explicit projection rule:
- stream immediately when possible
- optionally buffer only the minimum tail needed for safe correction
- never claim to provide stronger rewrite semantics than the transport can support

## Forbidden Remaining Patterns

The closure pass is incomplete if runtime code still contains any of these patterns:

- a helper that collapses a canonical outcome back into one raw event id for control flow
- a helper that reconstructs canonical semantics from `DeliveryResult`, placeholder ids, tracked ids, or raw transport facts
- terminal hook emission in any file other than `delivery_gateway.py`
- `TurnDeliveryResolution(...)` constructed directly outside the canonical projection helper
- caller code that marks turns handled without consulting `should_mark_handled`
- caller code that persists `turn_completion_event_id` where `response_identity_event_id` is required
- exception-based suppression cleanup semantics after delivery coordination begins
- preserved-stream outcomes that lose interactive metadata or physically delete the preserved event

## Proof Obligations

We consider the migration closed only if we can prove all of the following on current `HEAD`:

1. Source-of-truth proof.
Every terminal semantic decision can be traced back to `FinalDeliveryOutcome` policy rows.
2. Boundary proof.
Every outward-facing response lifecycle API used for control flow returns `TurnDeliveryResolution`.
3. Hook proof.
Every terminal cancellation or failure hook is emitted exactly once by `delivery_gateway.py`.
4. Physical-state proof.
Any outcome that claims a visible response survives leaves that response physically present and still interactive when policy allows it.
5. Persistence proof.
No persisted response linkage points at `turn_completion_event_id` when the canonical durable identity is different.
6. Text-authority proof.
Recorder persistence, replay, Matrix final delivery, and adapter projections all consume the same canonical final assistant text rule.

## Execution Strategy

### Approach Options

#### Option A: Continue patching review items individually

Do not do this.
It keeps drift alive and makes it impossible to know when the migration is truly finished.

#### Option B: One big refactor without ratchets

Too risky.
It invites regressions and makes review harder because everything moves at once.

#### Option C: Closure pass with deletion-based phases and hard audit gates

This is the right approach.

Each phase must:
- remove one class of legacy bridge
- add the contract tests that make that bridge impossible to reintroduce silently
- end with grep-based and test-based closure checks

## Definition Of Done

The migration is complete only when all of the following are true:

1. No caller-facing response lifecycle API returns `str | None`.
2. No runtime path uses `_coerce_final_delivery_outcome()` or `resolve_response_event_id()` for delivery semantics.
3. No terminal hook emission remains outside `delivery_gateway.py`.
4. No delivery-stage exception bypasses `FinalDeliveryOutcome`.
5. Preserved-stream failure paths preserve visible event state and interactive metadata.
6. Non-streaming final send/edit failures emit canonical terminal hooks exactly once.
7. `response_identity_event_id` is the only persisted durable response link.
8. Recorder persistence and visible final delivery share the same canonical final assistant text rule.
9. The post-migration audit checklist passes with no waivers.

## Merge Gate

This branch is not mergeable until:

- the closure plan checklist is fully checked
- the forbidden-pattern grep audits are clean
- the full suite passes on current `HEAD`
- every current review finding is either reproduced and fixed with a test, or documented as stale against current `HEAD`

There are no waivers for "mostly migrated" or "good enough for now."

## Accountability Rule

If any closure checklist item fails, the migration is not complete.

No "mostly migrated."
No "good enough for now."
No keeping a bridge because it is convenient.

If a bridge still exists, the code still has two contracts.
