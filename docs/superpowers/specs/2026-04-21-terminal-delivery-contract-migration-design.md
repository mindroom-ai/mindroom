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
- replay and regeneration continuity

It is also used for interactive follow-up registration, but only for states where the visible reply remains an active response artifact.
Preserved-stream error states may keep `response_identity_event_id`.
Cancellation-derived states do not.
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
- `state`
- `visible_response_event_id`
- `response_identity_event_id`
- `turn_completion_event_id`
- `should_mark_handled`
- `retryable`
- `has_visible_output`

This object is for outer callers.
It is not a second semantic state machine.
`state` is exactly `FinalDeliveryOutcome.state`, copied as convenience for callers.
Outer callers do not receive raw `FinalDeliveryOutcome`.
`TurnDeliveryResolution` is derived entirely from the same per-state policy row that drives `FinalDeliveryOutcome` accessors.

### Outer Repair Contract

Lifecycle may perform one best-effort outer repair only to land a terminal edit that was already semantically decided by the gateway.
Outer repair is transport-only.

Outer repair may use only:
- the canonical `FinalDeliveryOutcome`
- stream transport facts already frozen at the terminal boundary
- an already-known logical response event id that the gateway has already authorized as the repair target

Outer repair may not:
- perform a fresh standalone send
- emit hooks
- synthesize success
- create `response_identity_event_id`
- promote handledness
- infer any of the three canonical event-id meanings from `tracked_event_id`, repaired ids, or other partial facts

If outer repair succeeds, it lands the already-decided artifact.
If outer repair fails, it may preserve already-known `visible_response_event_id` and `turn_completion_event_id`.
It may not change hook-visible semantics or retroactively reinterpret the terminal state.

## Policy Table

The migration needs one explicit policy table adjacent to `FinalDeliveryOutcome`.
For each terminal state, the table must define the downstream contract.

Required policy dimensions:
- emit `after_response`
- emit `cancelled_response`
- derive `visible_response_event_id`
- derive `response_identity_event_id`
- derive `turn_completion_event_id`
- mark turn handled
- persist response linkage
- queue thread summary
- register interactive follow-up
- shield late post-effect failures once visible output exists
- treat as retryable

This table is the accountability mechanism.
No caller may invent behavior outside it.
`FinalDeliveryOutcome` accessors and `TurnDeliveryResolution` projection must both be derived from this same table.
There must not be parallel hand-coded conditionals for event-id accessors, handledness, retryability, or hook policy.

## Ownership Boundaries

### `delivery_gateway.py`

`delivery_gateway.py` becomes the sole owner of terminal delivery semantics and terminal hook emission.

It must:
- map non-streaming and streaming terminal cases into canonical `FinalDeliveryOutcome`
- emit terminal hooks exactly once
- own suppression cleanup outcomes
- preserve visible-stream survival semantics
- preserve interactive metadata whenever the visible response survives
- route ordinary non-streaming failed send/edit through the same canonical terminal hook path

Once terminal delivery coordination has started, suppression cleanup failure must resolve as canonical `suppression_cleanup_failed`.
It may not escape the typed terminal boundary as control flow.

Preserved-stream outcomes have a physical invariant.
If a state preserves `visible_response_event_id` or `response_identity_event_id` for an already-visible stream event, that event must remain physically visible.
Ordinary terminal update failure and hook-mutated re-edit failure may not redact or clean up that event.
The only legal redaction of an already-visible streamed reply is suppression cleanup under the suppression policy states.

It must not:
- return outcomes that require runner-side hook guesses
- hide visible-state loss behind fallback `DeliveryResult` reconstruction

### Transport Retry Contract

Terminal transport retry lives below the semantic boundary, but it is part of this contract.

A terminal first-send with no existing visible event id is non-retriable on ambiguous failure unless the transport boundary can prove invisibility or idempotency.
Automatic terminal retries are allowed only for operations that target an already-known event id or otherwise cannot create a second visible reply.
Cancellation and restart finalization must not wait behind retry backoff.

### `response_lifecycle.py`

`response_lifecycle.py` becomes a thin coordinator that:
- requests one optional outer repair when appropriate
- converts the canonical outcome into `TurnDeliveryResolution`
- invokes post-response effects using explicit policy rather than inferred success

It must not:
- return raw event ids
- reinterpret terminal semantics
- emit terminal hooks on its own

### Caller Boundaries

`bot.py`, `turn_controller.py`, `edit_regenerator.py`, skill-command entrypoints, command handlers, and every outward-facing wrapper around response generation must consume `TurnDeliveryResolution`.
This includes `ResponseRunner.generate_response()`, `send_skill_command_response()`, and any wrapper that currently returns `str | None`.

They must read:
- `should_mark_handled`
- `turn_completion_event_id`
- `response_identity_event_id`
- `retryable`

They must not use:
- `if event_id is not None`
- success-by-truthiness
- ad hoc "visible means success" rules

By the end of the migration, internal response APIs do not return `str | None`.
If a leaf compatibility wrapper still returns an event id, it must be a thin projection of one explicitly named field from `TurnDeliveryResolution`.
That wrapper may not be used for control flow.

### `post_response_effects.py`

`post_response_effects.py` must execute effects from explicit policy, not from raw delivery-result presence.

It must use:
- `response_identity_event_id` for persistence and summary effects
- `turn_completion_event_id` for shielding late failures after visible output
- `visible_response_event_id` where visible targeting is needed

Interactive follow-up uses `response_identity_event_id`.
It is allowed for `final_visible_delivery`, `kept_prior_visible_stream_after_completed_terminal_failure`, and `kept_prior_visible_stream_after_error` when interactive metadata survives.
It is not allowed for cancellation-derived states unless the policy table explicitly promotes that state into response identity.
`suppression_cleanup_failed` remains non-persistable and non-response-identity, but it still participates in failure shielding according to its policy row if visible output leaked.

### Final Text Consumers

`ai.py`, team response producers, interrupted replay persistence, and `api/openai_compat.py` must share one authoritative rule for final text.

The authoritative source is a shared canonicalization step that consumes accumulated assistant text, visible tool-transcript state, and optional terminal completion content.
If `RunCompletedEvent.content` is not `None`, including the empty string, it replaces earlier accumulated assistant text as the authoritative final assistant text.
If `RunCompletedEvent.content` is `None`, earlier accumulated assistant text remains authoritative.
Visible tool markers are preserved only through this same canonicalization step.
No consumer may append, merge, or reconstruct visible tool markers independently.

Matrix delivery uses the canonical rendered visible body derived from that shared canonicalization step.
Recorder persistence, replay, team paths, and SSE use the same authoritative final assistant text and the same canonicalization rules.

No consumer may keep earlier partial text when canonical final content differs.
If canonicalization produces no visible body, completing as visible `Thinking...` is illegal.

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
Do not expose raw `FinalDeliveryOutcome` to outer callers.

### Phase 3: Migrate Callers

Update:
- `response_lifecycle.py`
- `response_runner.py`
- `bot.py`
- `turn_controller.py`
- `edit_regenerator.py`
- skill-command paths in `response_runner.py`, `turn_controller.py`, and `commands/handler.py`
- any outward-facing wrapper that still projects a raw response event id

The goal of this phase is to eliminate caller-side success inference.

### Phase 4: Move All Terminal Hook Emission Behind the Gateway

Non-streaming and streaming final failures must emit hooks through one gateway-owned path.
Remove runner-side fallback hook emission.
Ordinary non-streaming failed send/edit must emit exactly one terminal failure hook through that same path.

### Phase 5: Unify Final Text Authority

Update:
- `ai.py`
- `teams.py`
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
2. Gateway tests for hook emission ownership, ordinary non-streaming failed send/edit hook emission, suppression cleanup, preserved-stream outcomes, and the absence of cleanup/redaction when preserved-stream states keep a visible reply
3. Lifecycle tests for `TurnDeliveryResolution`, including the outer-repair non-semantic invariant
4. Caller tests for handled vs retryable behavior across normal message paths, skill-command paths, and outward-facing wrappers
5. Final-text authority tests covering partial chunks plus corrective `RunCompletedEvent`, including agent and team paths
6. Interactive-registration tests for surviving streamed replies after terminal failure and explicit non-registration for cancellation-derived states
7. Failure-shielding tests for `suppression_cleanup_failed`, including visible-output shielding without persistence or response identity
8. Late streamed-finalization cancellation and exception tests that preserve `visible_response_event_id` and `turn_completion_event_id` without promoting `response_identity_event_id`

Targeted regression tests are still useful, but they must anchor back to the policy table.

## Accountability Rules

These are the non-negotiable checks for this migration.

1. No new delivery state without a policy-table row and tests.
2. No caller may decide success from `event_id is not None`.
3. No terminal hook emission outside `delivery_gateway.py`.
4. No new fallback that reconstructs semantics from partial facts.
5. No exception may bypass the typed terminal boundary after delivery has started.
6. No automatic retry of a first visible terminal send on ambiguous failure.
7. Each phase must remove an old inference path, not merely add a wrapper around it.
8. Each phase must end with targeted tests and `pre-commit`.
9. Review findings are not fixed one by one until they are mapped to the canonical policy table first.

## Definition Of Done

The migration is done when all of the following are true:

1. Lifecycle returns `TurnDeliveryResolution`, not `str | None`.
2. Immediate callers and outward-facing wrappers no longer use raw event-id truthiness as success.
3. Terminal hook emission is gateway-owned only.
4. Post-response effects consume explicit policy, not raw delivery presence.
5. Final text authority is shared across Matrix delivery, recorder persistence, replay, SSE, and team response paths.
6. Interactive follow-up survives preserved-stream terminal failures.
7. Suppression cleanup failure is a canonical typed outcome, not an exception escape hatch.
8. Legacy semantic reconstruction paths are removed or reduced to transport-only helpers.
9. The remaining delivery bugs are ordinary implementation defects, not contract leaks.
