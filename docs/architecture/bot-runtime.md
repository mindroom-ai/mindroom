# Bot Runtime Simplification

`src/mindroom/bot.py` currently owns too many jobs at once.
It is the Matrix lifecycle shell, the inbound event normalizer, the conversation resolver, the routing and team policy layer, the response lifecycle coordinator, and part of the persistence layer.
Today the file is roughly 6,800 lines long, which is a symptom rather than the root problem.
The root problem is that several core concepts are represented in multiple overlapping ways and are re-derived at different stages of the same turn.

This document defines the requirements for the bot runtime and proposes a simpler design.
The design goal is not to introduce more abstraction for its own sake.
The goal is to make one turn easy to trace from Matrix ingress to final delivery, with one owner for each concern.

## Current Pain Points

- Conversation identity is represented repeatedly as `EventInfo.thread_id`, `_MessageContext.thread_id`, `MessageTarget.thread_id`, `MessageTarget.resolved_thread_id`, and ad hoc local variables such as `effective_thread_id` and `delivery_thread_id`.
- Conversation history is loaded through several paths, including full history, lightweight snapshots, reply-chain reconstruction, per-turn caches, and the persistent event cache.
- Text, audio, sidecar-backed files, routed media, and edits all enter the same pipeline through different normalization paths.
- Response planning and response mechanics are interleaved, so policy decisions and delivery details are hard to separate.
- Placeholder messages, queued-message signals, streaming edits, cancellation, and duplicate tracking all mutate the same turn from different layers.
- Edit regeneration depends on partially reconstructing the original decision path instead of reading a single canonical record for the handled turn.

## Goals

- Make `AgentBot` a thin Matrix runtime shell.
- Introduce one canonical turn model for text, media, voice, synthetic hook events, and edits.
- Introduce one canonical conversation target model for routing, memory, and delivery.
- Move thread and reply-chain resolution behind one service boundary.
- Move response lifecycle state behind one service boundary.
- Preserve current user-visible behavior unless the current behavior is clearly a bug.
- Keep the number of runtime concepts small enough that a new contributor can trace a turn without reading the entire file.

## Non-Goals

- This design does not change the external Matrix behavior, the config model, or the tool runtime model.
- This design does not require a large class hierarchy.
- This design does not require splitting the code into many tiny files.
- This design does not redesign reactions or redactions beyond keeping their ownership clear.

## Functional Requirements

1. The bot runtime must accept text, media, voice, synthetic hook dispatches, edits, reactions, and redactions from Matrix.
2. The runtime must normalize inbound text-like turns exactly once before planning.
3. The runtime must preserve conversation continuity for native threads, plain replies from non-thread clients, and room-mode agents that intentionally avoid thread metadata.
4. The runtime must deduplicate already-handled source events before expensive work begins.
5. The runtime must enforce sender authorization and per-agent reply permissions before planning a response.
6. The runtime must support commands, router dispatch, direct agent replies, team replies, and explicit rejection responses.
7. The runtime must preserve attachment and media context across a conversation turn.
8. The runtime must support coalescing of closely related user turns without creating a second downstream execution model.
9. The runtime must support both streaming and non-streaming delivery with the same routing, hook, memory, and cancellation semantics.
10. The runtime must persist enough metadata to regenerate responses for edited source messages.
11. The runtime must keep the Matrix sync loop non-blocking by pushing expensive work into async tasks or background work.
12. The runtime must preserve backlog suppression semantics, where an older turn is skipped if a newer unhandled turn from the same requester already exists in the same conversation.
13. The runtime must preserve hook-ingress behavior for synthetic turns, including hook provenance, message-received depth, and the policy decisions that control reruns, plugin skipping, unmentioned-agent bypass, and deep-relay suppression.
14. The runtime must preserve the current built-in reaction paths: stop controls, interactive answers, and router config confirmations.
15. When built-in reaction handling declines a reaction, the runtime must still emit the post-built-in `reaction:received` observer hook.

## Structural Requirements

1. Conversation resolution must happen in one place.
2. Matrix reply-chain traversal, snapshot loading, full-history loading, and event-cache usage must be hidden behind that one place.
3. Response delivery target resolution must happen in one place.
4. The same canonical conversation target must be used for routing, locks, memory session IDs, and duplicate suppression.
5. Matrix delivery and tool runtime anchoring must derive an explicit delivery target from that canonical conversation target instead of introducing a third ad hoc target shape.
6. Response lifecycle state such as locks, queued signals, placeholders, and cancellation must have one owner.
7. Marking a source event as handled must happen in one place after a terminal outcome is known.
8. Hooks must observe the same normalized envelope regardless of whether the turn started as text, voice, media, or an edit.
9. A stage must not partially redo work that an earlier stage already performed.
10. Hook-ingress policy must run before unmentioned-agent gating and before deep synthetic relays are allowed to continue into full dispatch.

## Proposed Shape

The target design keeps `AgentBot` as the Matrix-facing shell and moves turn handling into a small pipeline.
The pipeline should be logical first and physical second.
If some stages live in the same file during migration, that is fine as long as the boundaries are clear.

```text
Matrix callback
    |
    v
AgentBot
    |
    v
TurnController
    |
    +--> InboundTurnNormalizer
    |
    +--> ConversationResolver
    |
    +--> DispatchPlanner
    |
    +--> RouteExecutor
    |
    +--> CommandExecutor
    |
    +--> ResponseCoordinator
            |
            +--> DeliveryGateway
            +--> AI response engine
            +--> PostResponseEffects
    |
    +--> ConversationStateWriter
```

`AgentBot` should own Matrix client lifecycle, sync start and stop, room joins, room invites, presence, and callback registration.
It should also keep the callback-to-background-task wrapper that prevents Matrix sync from blocking on event handling.
`AgentBot` should not decide how to resolve one turn beyond handing the raw event to the pipeline.

`TurnController` should own the high-level state machine for one inbound event.
It should be the only place that decides whether a turn is ignored, routed, handled as a command, or completed with a response.
It should also be the only place that updates the handled-turn ledger.
It should own sequencing across ingress, planning, and execution phases, but it should not reimplement the logic of any single phase.
It should apply hook-ingress policy early enough that deep synthetic relays can stop before full dispatch and `hook_dispatch` can bypass the unmentioned-agent gate without leaking special cases into later stages.

`InboundTurnNormalizer` should convert raw Matrix events into one typed `InboundTurn`.
Voice transcription, file sidecar preview extraction, and coalesced batches should all produce the same normalized shape.
Commands should still be detectable from the normalized turn.
Synthetic hook turns must preserve the ingress facts needed to compute hook policy, at minimum hook provenance and message-received depth.

`ConversationResolver` should own thread and reply-chain resolution.
It should be the only component allowed to translate Matrix relations into a canonical conversation target.
It should also own the turn-scoped history cache, the reply-chain caches, and access to the persistent event cache.
That includes the per-turn cache scope that deduplicates repeated thread-history fetches inside one turn.
It should not own send-vs-edit placement rules for already-existing response events.

`DispatchPlanner` should be a mostly pure policy stage.
It should use the normalized turn and the resolved conversation snapshot to decide between `ignore`, `command`, `route`, `reject`, `respond_individual`, and `respond_team`.
It should not send messages, edit messages, or touch cancellation state.
It should operate in two steps when needed: a cheap `preplan(snapshot)` and a `finalize_plan(full_context_if_needed)` step for cases where the current behavior depends on upgraded history before the final action is chosen.
When the planner returns `command` or `route`, the actual execution should run through dedicated side-effecting executors instead of smuggling transport and persistence work back into the planner.

`RouteExecutor` should own router relay behavior.
That includes routed attachment handling, target selection for the chosen agent or team, visible router echoes, and the terminal bookkeeping for a routed turn.

`CommandExecutor` should own command workflows.
Commands are planner outcomes, but command execution still needs conversation context, response delivery, AI or tool execution, and memory persistence, so it should be explicit rather than treated as a special planner branch.

`ResponseCoordinator` should own the mechanics of a planned response.
It should acquire the per-conversation lock, manage queued-message state, manage placeholders, choose streaming versus non-streaming delivery, and wire cancellation and stop-button behavior.
It should collapse the duplicated agent-response and team-response lock and queued-signal paths into one response lifecycle mechanism.
Queued placeholders should be generation-scoped tokens with exactly three terminal operations: `adopt`, `complete_and_redact`, and `cancel_and_redact`.

`TeamBot` should use the same inbound pipeline as `AgentBot`.
The team-specific difference should live at response execution time, not in a second ingress or planning flow.

`DeliveryGateway` should be the only layer that turns a canonical target into Matrix event content.
It should own send, edit, redact, latest-thread-event compatibility behavior, and response-hook transport around final visible delivery.

`PostResponseEffects` should own memory persistence, interactive-question registration, compaction notices, and thread-summary scheduling.
Those are downstream consequences of a terminal response, not part of response lifecycle state itself.

`ConversationStateWriter` should own the write side of conversation state.
That includes sync-time cache seeding, thread-event append behavior, and redaction-driven invalidation for the advisory `EventCache`.

## Canonical Runtime Types

The current code already has useful building blocks such as `MessageEnvelope` and `MessageTarget`.
The simplification should keep that idea, but reduce the number of adjacent representations.

### `InboundTurn`

`InboundTurn` is the normalized trigger for one turn.
It should include the source event ID, sender ID, requester ID, normalized body, source kind, attachment references, edit metadata, and the raw relation facts needed by the resolver.
For synthetic hook turns it must also preserve the ingress facts needed to reproduce current hook behavior, at minimum `hook_source` and `message_received_depth`.
Raw Matrix relation facts such as the incoming `thread_id` belong here and in the derived conversation context, not in the canonical conversation identity.

### `ConversationTarget`

`ConversationTarget` is the single source of truth for canonical conversation scope.
It should identify the stable scope used for routing, locks, session IDs, memory, and duplicate suppression.
For the internal pipeline, the important stable fields are `room_id`, `mode`, `root_event_id`, and `session_id`.
That list is intentionally about stable conversation identity, not a complete send target.

### `DeliveryTarget`

`DeliveryTarget` is derived from `ConversationTarget` when the runtime actually sends or edits Matrix events.
It should carry delivery-only fields such as the requested thread shape, `delivery_thread_root_id`, `reply_to_event_id`, and `latest_thread_event_id`.
This keeps placeholder adoption, existing-event edits, and room-mode delivery quirks out of the canonical conversation object.
`ConversationTarget` plus `DeliveryTarget` should fully replace today's `MessageTarget` contract.
The pair must preserve enough thread and reply-anchor information to rebuild tool runtime context without lossy translation.

### `ConversationContext`

`ConversationContext` should contain the canonical target, the conversation history, mention and participant facts, and a history detail level.
The resolver should return explicit `history_completeness` and `content_hydration` flags because a snapshot fetch may already be full history and cached reply-chain messages may still need sidecar hydration.
`snapshot` is a request, not a guarantee.
Only the resolver should be allowed to upgrade a context.

### `HandledTurnRecord`

`HandledTurnRecord` should be the durable record for one terminally handled turn.
It should persist `owner_entity`, exact `history_scope`, `conversation_target`, `session_id`, `reply_anchor_event_id`, `source_event_ids`, `source_event_prompts` for coalesced turns, `response_event_id`, and any `visible_echo_event_id`.
It should also persist the terminal outcome state, at minimum distinguishing `visible_echo_sent`, `terminally_handled_without_response`, and `response_completed`.
The exact `history_scope` matters for team runs because the persisted history lane may be a configured team name or an ad hoc sorted-member scope.
Edit regeneration should read this record directly instead of re-running router choice or mention-based responder selection.

### `DispatchPlan`

`DispatchPlan` should be the planner output.
It should say what to do, who owns the response, whether a full history upgrade is required, and what prompt inputs are needed.
It should not contain transient delivery state.

### `ResponseJob`

`ResponseJob` should be the executor input.
It should include the final prompt, target, existing response event ID if one exists, hook envelope, and any execution metadata that must be persisted with the run.

## Conversation Resolution

The most important simplification is to make one service own all conversation identity logic.
Today that logic is spread across `_extract_message_context_impl`, `_derive_conversation_context`, `_derive_conversation_target`, `_fetch_thread_history`, `_resolve_response_thread_root`, and several send and edit paths.

The proposed resolver API is:

```text
resolve(turn, detail="snapshot") -> ConversationContext
ensure_full(context) -> ConversationContext
```

`resolve` should return the canonical target plus the cheapest history that is valid for planning.
`ensure_full` should upgrade that context only when the planner or executor actually needs complete history.
No other stage should fetch history directly.

This lets the pipeline express a simple rule.
Planning starts with a snapshot.
Some turns may then require a full-history promotion before the final plan is chosen.
Actual response generation should only start after that promotion decision is settled.
Reply-only chains from non-thread clients are still treated as one conversation and get a stable canonical root reused for should-respond decisions, session scope, and duplicate suppression.

## Caching And State Ownership

The simplification depends on explicit ownership of caches and mutable state.

### ConversationResolver-owned state

- Reply-chain traversal caches.
- The turn-scoped thread history cache.
- Read access to the persistent `EventCache`.
- Snapshot versus full-history upgrade logic.

`EventCache` is advisory and partial, not authoritative.
Resolver code must treat cache misses, stale rows, and invalidation as normal fallback cases rather than as correctness failures.
If a fetch path wants to refresh, repopulate, or invalidate persistent cache entries, the resolver should request that through a writer-owned cache maintenance API instead of mutating `EventCache` directly.

### TurnController-owned state

- The handled-turn ledger, which replaces the current partial `ResponseTracker` role.
- Source-event to response-event linkage.
- Source-event to routed-echo linkage.
- The rule that a source event is only marked handled after a terminal outcome is known.
- Backlog suppression semantics for older turns in the same conversation.

### ResponseCoordinator-owned state

- Per-conversation lifecycle locks.
- Queued-message signals.
- Placeholder lifecycle.
- Stop-button and cancellation lifecycle.
- In-flight response counters.

### ConversationStateWriter-owned state

- Sync-time cache seeding from live Matrix events.
- Cached thread append behavior for threads that are already materialized.
- Redaction-driven invalidation and cache cleanup.
- Fetch-path cache invalidation, refresh, and repopulation requested by the resolver after thread-history reads.
- Exclusive write access to `EventCache`.

Coalescing should be treated as an ingress concern.
It may emit a combined `InboundTurn`, but it should not create a second response execution model.
It should only batch events that already passed ingress prechecks and share `(room_id, canonical conversation scope, requester_user_id)`.
If the canonical conversation root is discovered after the gate opens, retargeting must also transfer queued-signal and placeholder ownership for the same generation.
If coalescing wants a visible queued state, it should request that through the response coordinator instead of open-coding Matrix sends and redactions from the gate itself.

## Edit Regeneration

Edit handling should become a normal pipeline case instead of a special side path with partial reconstruction.
The missing abstraction is a handled-turn record that says which agent or team owned the original response, which source events fed it, and which response event was produced.

The requirements for edit regeneration are:

1. Editing a previously handled user message must find the original `HandledTurnRecord`.
2. Regeneration must reuse the persisted owner, history scope, and target directly.
3. Regeneration must not re-run router choice or mention-based responder selection.
4. Coalesced turns must persist the source prompt map needed to rebuild the coalesced prompt.
5. Regeneration must edit the existing response event in place when possible.
6. Before storing the replacement run, the runtime must remove or supersede prior persisted runs for every source event in the handled-turn record.

This removes the current special-case limitation where a routed agent may fail to regenerate after the user edits the original message.

## Runtime Invariants

- Native threads use the thread root as canonical conversation scope.
- Plain reply chains from non-thread clients still collapse to one stable canonical root.
- Room-mode agents collapse all delivery and session identity to room scope.
- Conversation scope and delivery scope are related but not identical.
- History has two dimensions: completeness and hydration.
- Snapshot resolution must stay cheap and must not fetch full history until promotion is required.
- Once promoted, full history should be fetched at most once per dispatch.
- `EventCache` is advisory and partial, so cache misses and stale rows are fallback cases, not correctness failures.
- Coalescing may only batch events that already passed self-message, duplicate, authorization, and reply-permission checks.
- If the coalescing gate opens before the canonical root is known, retargeting must transfer queued-signal and placeholder ownership for the same generation.
- A queued placeholder must end in exactly one terminal state: adopted, completed and redacted, or cancelled and redacted.
- Duplicate suppression is not only "already handled source event IDs"; it also includes backlog suppression when a newer unhandled turn from the same requester already exists in the same conversation.
- Hook-ingress policy must evaluate before unmentioned-agent gating and before deep synthetic relays continue into planner and executor stages.
- Router config-confirmation reactions remain a built-in reaction path and must not be lost while separating stop controls from interactive-answer reactions.
- If built-in reaction handling declines a reaction, the runtime still emits `reaction:received` observer hooks.
- `HandledTurnRecord` must persist exact history-scope identity for team runs instead of inferring it later from `owner_entity`.
- If the runtime does not introduce a durable pre-send reservation or idempotent delivery key, crash-boundary duplicate suppression is best-effort rather than exactly-once.

## Simpler End-To-End Flow

The target steady-state flow for text, voice, media, and edits should be:

1. `AgentBot` receives a Matrix callback and hands it to `TurnController`.
2. `InboundTurnNormalizer` converts the raw event into `InboundTurn`.
3. `TurnController` performs cheap prechecks such as self-message skipping, deduplication, authorization, and hook-ingress gating for synthetic relays.
4. `ConversationResolver.resolve(..., detail="snapshot")` returns the canonical target and planning context.
5. `DispatchPlanner` returns a `DispatchPlan`, and may request a full-history promotion before the final plan is locked.
6. If needed, `ConversationResolver.ensure_full(...)` upgrades the context and the planner finalizes the plan.
7. `TurnController` routes the final plan to `RouteExecutor`, `CommandExecutor`, or `ResponseCoordinator`.
8. `ResponseCoordinator` executes the response job and delegates send or edit operations to `DeliveryGateway`.
9. `PostResponseEffects` runs terminal side effects.
10. `TurnController` records the terminal outcome in the handled-turn ledger.

Control reactions such as stop buttons should remain separate from the turn pipeline.
Router config-confirmation reactions should also remain a separate built-in reaction path.
Interactive-answer reactions should be fed back into `TurnController` as synthetic `InboundTurn`s.
If built-in reaction handling declines the reaction, the runtime should still emit `reaction:received` hooks for plugin observers.
Redactions remain outside the turn pipeline, but they may notify `ConversationStateWriter` to mutate persistent cache state while resolver-owned turn-scoped caches stay read-side only.

## Migration Plan

The migration should be incremental and behavior-preserving.
The order matters because the riskiest coupling today lives in terminal outcome recording, response lifecycle state, and edit regeneration metadata.

### Phase 1

Centralize terminal outcome recording first.
Keep the existing control flow, but make one helper own source-event to response-event linkage, visible-echo linkage, and the durable handled-turn record.
No behavior changes should happen in this phase.

### Phase 2

Extract `ResponseCoordinator`, `DeliveryGateway`, and `PostResponseEffects`.
Move locks, queued signals, placeholders, cancellation, send and edit operations, and visible delivery behind one response boundary.
Move memory persistence, interactive registration, compaction notices, and thread-summary side effects into post-response effects.
Because Phase 2 lands before conversation resolution is fully extracted, it may need a temporary shim for thread-root resolution that Phase 3 later removes.

### Phase 3

Extract `InboundTurnNormalizer`, `ConversationResolver`, and `ConversationStateWriter` together.
Preserve snapshot and full-history upgrades, merged plain-reply context, sidecar hydration, hook provenance, coalescing thread-key resolution, sync-time cache seeding, and redaction invalidation.

### Phase 4

Extract `DispatchPlanner`.
Represent command handling and router relay delivery as explicit executor paths instead of planner-owned side effects.

### Phase 5

Move edit regeneration onto the same pipeline using the handled-turn record.
Remove special-case code paths that only exist because earlier stages were not explicit enough.

## Reliability Note

Duplicate suppression should be described honestly.
Unless the runtime introduces a durable pre-send reservation or an idempotent delivery key, duplicate suppression across crash boundaries is best-effort rather than exactly-once.

## Result

After the refactor, `bot.py` should read like a runtime shell.
The file should answer questions such as when the bot starts, what callbacks are registered, and which pipeline handles each event type.
It should not be the place where a contributor has to understand every detail of thread resolution, routing, placeholder state, streaming, and edit regeneration all at once.

The key simplification is not smaller methods.
The key simplification is one canonical turn, one canonical conversation target, one conversation resolver, and one response coordinator.
