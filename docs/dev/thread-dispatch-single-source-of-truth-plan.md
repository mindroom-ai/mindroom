# Thread Dispatch Single Source Of Truth Plan

Last updated: 2026-04-02
Owner: MindRoom backend
Status: In progress
Applies to: PR #447 and the follow-up cleanup work that lands with it

## Objective

Make thread reconstruction, dispatch decisions, and response delivery use one source of truth per concern.
Stop fixing the same bug class in four different call paths.
Preserve the startup-latency goal without letting preview data leak into AI-facing or routing-facing decisions.

## Why This Keeps Regressing

The current code has three duplicated seams.
Each seam has multiple helpers that must agree on subtle behavior.
Each merge or optimization changes one path and leaves another path behind.

### Seam 1: Thread View Construction

The code currently rebuilds visible thread state in multiple places.
`fetch_thread_snapshot()` and `fetch_thread_history()` each have their own behavior constraints.
The room-scan fallback is a third reconstruction path.
`_latest_thread_event_id()` is effectively a fourth thread-visibility path.
These paths all need to agree on root detection, replacement handling, visible event IDs, and sidecar behavior.

### Seam 2: Dispatch Phase Boundaries

`_prepare_dispatch()` builds one mutable context object.
That context is consumed before hydration in `_resolve_dispatch_action()`.
The same object is later upgraded by `_hydrate_dispatch_context()`.
This makes it too easy for router, team formation, and `should_agent_respond()` to read preview data as if it were canonical data.

### Seam 3: Response Outcome Semantics

Streaming, non-streaming, team, and individual delivery paths each define “final response” slightly differently.
Suppressed responses, redacted placeholders, cleanup failures, and streamed-first-chunk suppression do not share one resolver.
`_resolve_response_event_id()` is not the only place that decides what the final response event ID means.

## Current Branch Status

Phase 1 is partially landed already.
The preview snapshot path now preserves latest visible edits without sidecar downloads.
That is the right visible-state boundary for preview mode.

Phase 4 is also partially landed already.
`resolved_thread_id` already flows through locks, session IDs, tool runtime context, and most delivery paths.
The remaining work there is cleanup and removal of the last ad hoc recomputation points.

Phase 2 is partially landed in the current worktree.
Dispatch now hydrates canonical thread history before router, team, and individual-response decisions consume thread bodies.
The remaining work there is cleanup and simplification of the phase boundary rather than proving the boundary itself.

Phase 3 also has partial progress.
Streaming suppression now uses the shared response result path more consistently, but the broader outcome cleanup still remains.

## Non-Negotiable Invariants

1. There is one shared thread-view builder.
2. Preview mode and full mode share ordering, root detection, edit selection, and visible-event semantics.
3. Preview mode may skip sidecar downloads, but it may not show stale pre-edit visible state.
4. Full mode is the only mode allowed to hydrate sidecar-backed canonical content.
5. Router, team selection, and `should_agent_respond()` must consume hydrated canonical thread context only.
6. Preview context is allowed only for cheap deterministic gates that do not depend on canonical thread content.
7. `DecisionContext` must not carry preview `thread_history`.
8. One inbound event must not pay for two independent visible-thread reconstructions over the same thread unless the second pass reuses results from the first pass.
9. The existing shared response result type must become the single source of truth for delivery outcomes rather than being paralleled by a second near-duplicate type.
10. A suppressed response must never preserve a placeholder event ID as the final delivered response.
11. A source event is marked responded only when a real terminal outcome exists.
12. Thread identity for locking, delivery, sessions, and tool runtime must come from one canonical resolved thread target.

## Non-Goals

Do not rewrite all of `bot.py`.
Do not add a large abstraction tree or framework.
Do not preserve old tests by adding weak production fallbacks.
Do not optimize micro-latency at the cost of semantic correctness.

## Target Architecture

## 1. One Shared Thread View Builder

Introduce one internal builder for thread views.
The builder takes a mode flag with exactly two values: `preview` and `full`.
Both modes use the same event ordering and visible-state reconstruction rules.
Both modes apply bundled replacements and replacement relations to compute the latest visible event.
Only `full` mode is allowed to hydrate sidecar-backed canonical content with `client.download()`.
`preview` mode resolves edits and visible mentions from relation payloads when present, but never downloads sidecars.
One inbound event must not trigger two unrelated reconstructions of the same thread.
If a later full pass is required after an earlier preview pass, it must reuse relation results or equivalent intermediate thread-view state from the preview pass.

The builder should produce one list of `ResolvedVisibleMessage`.
The builder should be used by:
- `fetch_thread_snapshot()`
- `fetch_thread_history()`
- the room-scan fallback adapter
- `_latest_thread_event_id()` either directly or through a tiny shared helper that reads the same visible-state result

The main rule is simple.
There is one implementation of “latest visible thread state”.
Mode only changes how canonical content is hydrated.

## 2. Explicit Dispatch Phases

Replace the current mutable “maybe preview, maybe hydrated” context flow with explicit phases.

Phase A is `DecisionContext`.
This phase contains only cheap deterministic data.
It may include:
- current event metadata
- sender/requester identity
- current-event mention resolution
- thread target identity
- whether a preview thread exists

It must not include preview `thread_history`.
That is intentional.
If thread bodies are present in this phase, someone will eventually route on them again.

Phase B is `HydratedContext`.
This phase contains full canonical thread history and any derived values that depend on it.
It is the only context that may be consumed by:
- router dispatch
- team formation
- `should_agent_respond()`
- prompt assembly
- payload building

The placeholder should be sent only after the action is known to be “this bot or team will answer”.
That means the safe dispatch order becomes:
1. cheap deterministic gates
2. hydrate canonical thread context
3. router and team and should-respond decision
4. send placeholder
5. build payload and generate response

This gives up the most aggressive early-placeholder behavior.
It keeps the meaningful win of moving the placeholder before payload building and model work.
It removes the repeated “preview text drove routing” bug class.

## 3. One Shared Response Outcome Type

Evolve `_ResponseDispatchResult` into the single shared response outcome type for all delivery paths.
That object should carry:
- `event_id`
- `response_text`
- `delivery_kind`
- `suppressed`
- `option_map`
- `options_list`

Then add one resolver for final response identity.
Only that resolver decides:
- which event ID counts as the real final response
- whether a placeholder was terminal or merely provisional
- whether the source event should be marked responded

Streaming, non-streaming, team, and individual delivery must all return this same outcome shape.
If a before-response hook suppresses after streaming has already created a placeholder, the outcome must represent “no final response exists”.
That rule must be identical in both team and individual streaming paths.

## 4. One Canonical Thread Target

Keep the resolved thread root as a first-class value through the response lifecycle.
Locks, session IDs, delivery, and tool runtime context must all receive the same resolved thread target.
Do not recompute raw `thread_id` versus `resolved_thread_id` ad hoc downstream.

This does not require a broad type system redesign.
A small shared helper or a minimal carried target object is enough.
The important part is one source of truth, not a bigger abstraction surface.

## Implementation Plan

## Phase 1: Thread View Unification

Create the shared thread-view builder with `preview` and `full` mode.
Move all relation-based visible-state logic into it.
Move the room-scan fallback to a mode-aware wrapper that reuses the same visible-state application helpers.
Make `_latest_thread_event_id()` consume the same visible-state rules.
Keep preview mode sidecar-free.
Keep preview mode latest-edit aware.

Status on current branch: partially landed.
The remaining work is to finish consolidation so snapshot/history/latest-thread all use one shared visible-state policy rather than multiple subtly different helpers.

### Phase 1 tests

Add or tighten tests for:
- preview mode applies latest visible reply edits
- preview mode never downloads sidecars
- full mode hydrates sidecar-backed root and edit content
- root-only relations fall back cleanly
- latest-thread-event fallback matches the last visible event from the shared builder

## Phase 2: Dispatch Phase Split

This phase is the main architectural seam in this series.
It is a deliberate latency-versus-correctness decision, not just neutral cleanup.
The goal is to move the placeholder later than the old preview-driven branch behavior did, but still earlier than payload building and model execution.

Introduce explicit `DecisionContext` and `HydratedContext` or the smallest equivalent that enforces the same separation.
Remove router, team, and `should_agent_respond()` from the preview phase.
Make `_prepare_dispatch()` stop building AI-facing decisions from preview thread history.
Hydrate canonical thread history before routing and team decision logic runs.
Send the placeholder only after a real reply action is selected.

### Phase 2 tests

Add regression tests for:
- sidecar-backed thread history does not affect routing until hydration
- edited mentions in thread history affect routing and team selection correctly
- skip paths do not send placeholders
- routed-away paths do not send placeholders
- real reply paths still send placeholders before payload building and model generation

## Phase 3: Response Outcome Unification

Start by fixing any remaining streaming-suppression edge cases.
Then evolve `_ResponseDispatchResult` into the single source of truth instead of introducing a second parallel outcome type.
Change `_deliver_generated_response()`, `_process_and_respond_streaming()`, team streaming delivery, and the final response-id resolution code to use that one shared result shape.
Remove duplicate placeholder and suppression semantics from individual branches.
Make `mark_responded` depend on a shared predicate instead of local branch behavior.

### Phase 3 tests

Add regression tests for:
- non-streaming suppressed placeholder cleanup success
- non-streaming suppressed placeholder cleanup failure
- streaming suppression after first send
- team streaming suppression after first send
- placeholder redaction failure leaves the source event retryable

## Phase 4: Thread Target Cleanup

Make one helper compute the resolved thread target once.
Use that value for:
- response lifecycle locks
- session IDs
- tool runtime context
- response delivery
- prompt metadata

Keep this change local to the response lifecycle code.
Do not spread a new type across unrelated modules unless it removes real duplication immediately.

Status on current branch: partially landed.
The work here is to finish consolidation and remove the last ad hoc recomputation points rather than to build resolved-thread propagation from scratch.

### Phase 4 tests

Add or keep coverage for:
- first-turn replies canonicalized onto a thread root
- streaming and non-streaming session parity
- team and individual delivery parity
- room-mode scoping remains room-scoped

## Commit Strategy

1. Finish the dispatch-phase split on top of the landed thread-view groundwork.
2. Finish shared response-outcome semantics on `_ResponseDispatchResult`.
3. Complete canonical thread-target cleanup.
4. Dead-code removal, test-harness cleanup, and docs updates.

Each commit must leave the touched suites green.
Each commit must remove duplication rather than layering more branches on top of it.

## Acceptance Criteria

The refactor is done when all of the following are true.

- There is one shared visible-thread state policy with thin relations and room-scan adapters.
- Preview and full thread fetches differ only in content hydration mode.
- `_latest_thread_event_id()` matches the last visible event from the shared thread-view logic.
- Router, team formation, and `should_agent_respond()` never consume preview-only thread bodies.
- No AI-based routing or team-selection call runs before hydration.
- Streaming and non-streaming suppression semantics match.
- Team and individual suppression semantics match.
- Placeholder event IDs are never preserved as final response IDs after suppression.
- The touched test surface is warning-clean except for acknowledged third-party warnings outside this code.

## Immediate Next Step

Finish Phase 2 immediately on top of the already-landed Phase 1 groundwork.
Do not continue stacking local fixes on the old split paths.
Once the dispatch boundary is clean, finish Phase 3 and Phase 4 on the same shared foundations.
