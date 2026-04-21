# Terminal Delivery Contract Migration Design

**Goal:** Finish the terminal-delivery migration so delivery semantics are defined once, consumed consistently, and no caller infers success from a truthy event id.

**Status:** Proposed design approved for specification.

## Problem

The current delivery refactor has a stronger internal model than the old code, but the migration is incomplete.
`FinalDeliveryOutcome` exists, yet multiple callers still interpret delivery success differently.
That leaves the system straddling two contracts at once.

The old contract is "return `str | None` and infer success from whether an event id exists."
The new contract is "return a canonical delivery state with explicit semantics."
Both are still live in the codebase.

That is why repeated review rounds keep finding real bugs in different places.
Each layer is still making slightly different assumptions about visible output, delivered response identity, turn completion, retryability, and hook emission.

## Current Failure Pattern

The same semantic decision is still made in multiple layers.
Those layers do not share one authoritative policy table.

The active contract leaks are:

1. Lifecycle finalization still collapses rich delivery meaning into `str | None`.
2. Immediate callers still treat non-`None` as "successful response."
3. Post-response effects still gate behavior from raw delivery facts rather than one canonical policy.
4. Streaming, recorder persistence, and OpenAI-compatible SSE do not share one authoritative final-text rule.
5. The gateway owns some terminal hook semantics, but runners and fallback paths still own others.

The result is a fragile migration.
Any state that crosses one of those boundaries can be reinterpreted incorrectly.

## Design Principles

1. There must be one canonical semantic model for terminal delivery.
2. Only one layer may own terminal hook emission semantics.
3. Callers must not infer success or completion from event-id truthiness.
4. Visible output, response identity, and turn completion are related but distinct concepts.
5. Every terminal state must have an explicit downstream policy.
6. Recorder persistence, replay, and API streaming must use the same final-text authority.
7. Migration must remove old inference paths, not merely patch around them.

## Canonical Concepts

The migration will use three event-id concepts, each with one meaning.

### `visible_response_event_id`

This answers: "What response event is currently visible in the room?"

It is used for:
- hook payloads that need to target visible output
- visible cleanup and redaction behavior
- shielding logic for failures after visible output already leaked

It is not used to decide whether a turn produced a durable delivered response.

### `response_identity_event_id`

This answers: "What event should count as the response for persistence and future regeneration?"

It is used for:
- response linkage persistence
- thread summary eligibility
- interactive follow-up registration for a surviving visible response
- replay and regeneration continuity

It is not used for cancellation-note bookkeeping unless that state is intentionally promoted into response identity.

### `turn_completion_event_id`

This answers: "What visible event, if any, means the turn produced a handled terminal artifact?"

It is narrower than `visible_response_event_id` and broader than `response_identity_event_id`.
It exists to prevent the outer API from forcing unrelated meanings into `response_identity_event_id`.

It is used for:
- whether the turn should count as handled
- whether late post-effect failures should be downgraded once visible output exists

It is not used for response linkage persistence or success-style downstream effects.

## Canonical Types

### `FinalDeliveryOutcome`

`FinalDeliveryOutcome` remains the canonical semantic terminal object.
It will continue to represent the terminal state itself.
It will also expose explicit accessors or computed properties for the three event-id meanings above.

The state model remains in `src/mindroom/final_delivery.py`.
The key change is that the state model must own more downstream policy directly instead of forcing callers to derive policy from individual fields.

### `TurnDeliveryResolution`

Lifecycle finalization will no longer return `str | None`.
It will return a typed outer result, tentatively named `TurnDeliveryResolution`.

It will include:
- `terminal_state`
- `visible_response_event_id`
- `response_identity_event_id`
- `turn_completion_event_id`
- `should_mark_handled`
- `retryable`
- `has_visible_output`
- `delivery_outcome`

This object is for outer callers.
It is not a second semantic state machine.
It is an explicit projection of `FinalDeliveryOutcome` plus lifecycle-level repair facts.

## Policy Table

The migration needs one explicit policy table adjacent to `FinalDeliveryOutcome`.
For each terminal state, the table must define the downstream contract.

Required policy dimensions:
- emit `after_response`
- emit `cancelled_response`
- preserve `visible_response_event_id`
- preserve `response_identity_event_id`
- preserve `turn_completion_event_id`
- mark turn handled
- persist response linkage
- queue thread summary
- register interactive follow-up
- shield late post-effect failures once visible output exists
- treat as retryable

This table is the accountability mechanism.
No caller may invent behavior outside it.

## Ownership Boundaries

### `delivery_gateway.py`

`delivery_gateway.py` becomes the sole owner of terminal delivery semantics and terminal hook emission.

It must:
- map non-streaming and streaming terminal cases into canonical `FinalDeliveryOutcome`
- emit terminal hooks exactly once
- own suppression cleanup outcomes
- preserve visible-stream survival semantics
- preserve interactive metadata whenever the visible response survives

It must not:
- return outcomes that require runner-side hook guesses
- hide visible-state loss behind fallback `DeliveryResult` reconstruction

### `response_lifecycle.py`

`response_lifecycle.py` becomes a thin coordinator that:
- requests one optional outer repair when appropriate
- converts the canonical outcome into `TurnDeliveryResolution`
- invokes post-response effects using explicit policy rather than inferred success

It must not:
- return raw event ids
- reinterpret terminal semantics
- emit terminal hooks on its own

### Immediate Callers

`bot.py`, `turn_controller.py`, and `edit_regenerator.py` must consume `TurnDeliveryResolution`.

They must read:
- `should_mark_handled`
- `turn_completion_event_id`
- `response_identity_event_id`
- `retryable`

They must not use:
- `if event_id is not None`
- success-by-truthiness
- ad hoc "visible means success" rules

### `post_response_effects.py`

`post_response_effects.py` must execute effects from explicit policy, not from raw delivery-result presence.

It must use:
- `response_identity_event_id` for persistence and summary effects
- `turn_completion_event_id` for shielding late failures after visible output
- `visible_response_event_id` where visible targeting is needed

Interactive follow-up must be allowed for surviving visible responses even when terminal delivery finished in a preserved-stream failure state.

### Final Text Consumers

`ai.py`, interrupted replay persistence, and `api/openai_compat.py` must share one authoritative rule for final text.

If `RunCompletedEvent.content` is authoritative, then:
- Matrix delivery uses it
- recorder persistence uses it
- replay uses it
- OpenAI-compatible SSE uses it

No consumer may keep earlier partial text when canonical final content differs.

## Migration Strategy

The migration should happen in bounded phases.
Each phase must begin with failing tests and end with removal of an old inference path.

### Phase 1: Freeze the Policy

Write a parameterized policy-table test for every `FinalDeliveryOutcome.state`.
The test must assert every downstream policy dimension.

This creates one visible contract before more code moves.

### Phase 2: Introduce `TurnDeliveryResolution`

Add the typed lifecycle result without deleting the old paths immediately.
Create adapters only long enough to keep the tree passing.

Do not expose new semantics through `str | None`.

### Phase 3: Migrate Callers

Update:
- `response_lifecycle.py`
- `response_runner.py`
- `bot.py`
- `turn_controller.py`
- `edit_regenerator.py`

The goal of this phase is to eliminate caller-side success inference.

### Phase 4: Move All Terminal Hook Emission Behind the Gateway

Non-streaming and streaming final failures must emit hooks through one gateway-owned path.
Remove runner-side fallback hook emission.

### Phase 5: Unify Final Text Authority

Update:
- `ai.py`
- recorder persistence
- replay state
- `api/openai_compat.py`

All must consume the same authoritative terminal text semantics.

### Phase 6: Delete Legacy Reconstruction

Remove or sharply constrain helpers that reconstruct meaning from:
- `DeliveryResult`
- `tracked_event_id`
- raw repaired event ids
- truthy response ids

If a helper survives, it must be transport-only, never semantic.

## Testing Strategy

Testing must move from ad hoc regressions to contract coverage.

Required test layers:

1. Canonical policy-table tests in `tests/test_final_delivery.py`
2. Gateway tests for hook emission ownership, suppression cleanup, and preserved-stream outcomes
3. Lifecycle tests for `TurnDeliveryResolution`
4. Caller tests for handled vs retryable behavior
5. Final-text authority tests covering partial chunks plus corrective `RunCompletedEvent`
6. Interactive-registration tests for surviving streamed replies after terminal failure

Targeted regression tests are still useful, but they must anchor back to the policy table.

## Accountability Rules

These are the non-negotiable checks for this migration.

1. No new delivery state without a policy-table row and tests.
2. No caller may decide success from `event_id is not None`.
3. No terminal hook emission outside `delivery_gateway.py`.
4. No new fallback that reconstructs semantics from partial facts.
5. Each phase must remove an old inference path, not merely add a wrapper around it.
6. Each phase must end with targeted tests and `pre-commit`.
7. Review findings are not fixed one by one until they are mapped to the canonical policy table first.

## Definition Of Done

The migration is done when all of the following are true:

1. Lifecycle returns `TurnDeliveryResolution`, not `str | None`.
2. Immediate callers no longer use raw event-id truthiness as success.
3. Terminal hook emission is gateway-owned only.
4. Post-response effects consume explicit policy, not raw delivery presence.
5. Final text authority is shared across Matrix delivery, recorder persistence, replay, and SSE.
6. Interactive follow-up survives preserved-stream terminal failures.
7. Legacy semantic reconstruction paths are removed or reduced to transport-only helpers.
8. The remaining delivery bugs are ordinary implementation defects, not contract leaks.
