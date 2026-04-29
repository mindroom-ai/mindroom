# Dispatch Handoff Source And Policy Refactor Plan

## Goal

Introduce an explicit dispatch handoff seam between coalescing and dispatch preparation.

The handoff must carry the event payload, trusted source metadata, dispatch policy metadata, batch metadata, media metadata, and opaque cleanup metadata as separate fields.

This should stop `PreparedTextEvent.source_kind_override` from becoming the place where unrelated meanings are smuggled between modules.

The central invariant is that `source_kind` describes what arrived, while `dispatch_policy_source_kind` describes how this turn should be treated.

## Implementation Progress

- [x] Step 1 adds a neutral `dispatch_handoff.py` module with shared dispatch event and handoff types.
- [x] Step 2 builds `DispatchHandoff` values from `CoalescedBatch` outside `coalescing.py`.
- [x] Step 3 refreshes sidecar payload metadata and source-event prompts after raw text hydration.
- [x] Step 4 threads `DispatchHandoff` through `TurnController.handle_coalesced_batch()`.
- [x] Step 5 passes handoff ingress and payload metadata into context extraction and envelope creation.
- [x] Step 6 removes production reads of dispatch policy from `PreparedTextEvent`.
- [x] Step 7 updates newer-message guards to use dispatch source metadata for automation turns.
- [x] Step 8 routes text, sidecar text, voice text, and standalone media through the shared active-follow-up enqueue decision.
- [x] Step 9 makes scheduled, hook, hook-dispatch, and trusted internal relay source kinds FIFO bypass barriers.
- [x] Step 10 keeps resolver and lifecycle modules independent of the full coalescing handoff object.
- [x] Add the remaining final-envelope and model-payload regressions listed in the test plan.
- [ ] Complete the accountability searches and full validation commands.

## Current Problem

The current implementation keeps finding bugs because `source_kind` is used for multiple concepts.

It describes event modality such as `message`, `voice`, `image`, and `media`.

It describes automation provenance such as `scheduled`, `hook`, `hook_dispatch`, and `trusted_internal_relay`.

It also sometimes describes response policy such as `active_thread_follow_up`.

Those concepts have different consumers.

Hooks need actual modality and provenance.

Turn policy needs active-follow-up treatment.

Command handling needs to know whether the event is voice or media.

The coalescing gate needs to know whether an event is normal, command, or bypass.

The response lifecycle needs cleanup metadata for queued-human notices.

When all of that travels through one event override field, one fix tends to break a different consumer.

## Architectural Fit

The dispatch path has distinct stages.

Ingress receives Matrix text, voice, file, image, video, hook, scheduled, and relay events.

Normalization hydrates long text, prepares sidecar text previews, and transcribes voice.

Coalescing owns FIFO ordering, debounce, upload grace, command barriers, bypass barriers, and retargeting.

Dispatch preparation builds the `MessageEnvelope`, emits message-received hooks, and applies agent mention gates.

Turn policy decides whether to ignore, route, respond individually, or form a team.

Response lifecycle owns active-response locks and queued-human notice state.

The missing seam is the output from coalescing into dispatch preparation.

That seam should be a typed dispatch handoff rather than a mutated event payload.

## Proposed Interface

Add small dataclasses for the coalescing-to-dispatch seam.

Trusted ingress metadata should be explicit.

Payload metadata that is currently smuggled through synthetic Matrix event content should also be explicit.

```python
@dataclass(frozen=True)
class DispatchIngressMetadata:
    source_kind: str
    dispatch_policy_source_kind: str | None = None
    hook_source: str | None = None
    message_received_depth: int = 0


@dataclass(frozen=True)
class DispatchPayloadMetadata:
    attachment_ids: tuple[str, ...] | None = None
    original_sender: str | None = None
    raw_audio_fallback: bool | None = None
    mentioned_user_ids: tuple[str, ...] | None = None
    formatted_bodies: tuple[str, ...] | None = None
    skip_mentions: bool | None = None


@dataclass(frozen=True)
class DispatchHandoff:
    room: nio.MatrixRoom
    event: TextDispatchEvent
    requester_user_id: str
    ingress: DispatchIngressMetadata
    payload: DispatchPayloadMetadata = field(default_factory=DispatchPayloadMetadata)
    source_event_ids: tuple[str, ...] = ()
    source_event_prompts: Mapping[str, str] = field(default_factory=dict)
    media_events: tuple[MediaDispatchEvent, ...] = ()
    dispatch_metadata: tuple[PendingDispatchMetadata, ...] = ()
```

The exact field names may be adjusted during implementation, but the separation between text dispatch event, trusted ingress metadata, payload metadata, media, and cleanup metadata should remain.

`DispatchHandoff.event` should be a `TextDispatchEvent`.

Standalone media and media batches should synthesize a text dispatch event before dispatch preparation.

The original media events should travel only in `media_events`.

`DispatchIngressMetadata.source_kind` is actual modality or provenance.

`DispatchIngressMetadata.dispatch_policy_source_kind` is optional dispatch behavior.

`active_thread_follow_up` belongs in `dispatch_policy_source_kind`.

`active_thread_follow_up` should not replace `message`, `voice`, `image`, or `media` as the event source kind.

`hook_source` and `message_received_depth` are trusted ingress metadata.

They should not require `ConversationResolver` to rediscover hook transport internals from the event after coalescing.

`attachment_ids`, `original_sender`, raw-audio fallback, mentions, formatted bodies, and skip-mention state should not require synthetic event source mutation to survive the handoff.

Payload metadata fields that may be unavailable before hydration should be tri-state.

`None` means unknown and must not be interpreted as authoritative absence.

Empty tuples or `False` mean the hydrated or trusted source was inspected and the metadata is authoritatively absent.

## Source Kind Contract

`source_kind` should preserve the real source consumed by hooks and command policy.

Valid real source kinds include `message`, `voice`, `image`, `media`, `scheduled`, `hook`, `hook_dispatch`, and `trusted_internal_relay`.

`dispatch_policy_source_kind` should carry behavior-only markers.

The first policy marker is `active_thread_follow_up`.

Future policy markers must not be added to `source_kind`.

`PreparedTextEvent.source_kind_override` should only describe the source of a prepared text payload.

`PreparedTextEvent.source_kind_override` should not carry active-follow-up policy.

`PreparedTextEvent.dispatch_policy_source_kind_override` must be removed once `DispatchHandoff` is threaded through dispatch.

The mergeable endpoint is that no production path or test fixture can express dispatch policy through `PreparedTextEvent`.

Direct callers should either create a `DispatchHandoff` or pass explicit source metadata into the wrapper that creates one.

## Handoff Construction

Keep `CoalescingGate` responsible for claiming FIFO batches and calling the dispatch callback.

Keep `CoalescedBatch` as the batch summary created from claimed `PendingEvent` values.

The seam starts at enqueue.

`PendingEvent` should carry trusted ingress metadata or the fields needed to build it.

`PendingEvent` should carry real `source_kind`.

`PendingEvent` should carry optional `dispatch_policy_source_kind`.

The gate classifier should use `dispatch_policy_source_kind` for bypass policy when it is present.

The gate classifier should use real `source_kind` for command and media behavior.

Automation source kinds with semantics that should not merge with user turns must be FIFO barriers.

This includes `scheduled`, `hook`, `hook_dispatch`, and `trusted_internal_relay` unless a future design defines per-event source metadata and deterministic mixed-batch semantics.

Do not coalesce scheduled, hook, hook-dispatch, or trusted-relay events with human `message`, `voice`, `image`, or `media` events in this refactor.

Bypass or solo dispatch is the simpler contract for these source kinds.

Add a mandatory dispatch-boundary module for handoff construction.

Use a name like `src/mindroom/dispatch_handoff.py` or `src/mindroom/dispatch_input.py`.

Move shared dispatch event types there, including `PreparedTextEvent`, `TextDispatchEvent`, `MediaDispatchEvent`, and `DispatchEvent`.

`ConversationResolver` and `InboundTurnNormalizer` should import shared dispatch event types from that neutral module instead of importing from `coalescing.py`.

Add `build_dispatch_handoff(batch)` in that dispatch-boundary module as the public conversion from `CoalescedBatch` to dispatch input.

Make `build_batch_dispatch_event(batch)` private or internal to the boundary module after the handoff builder exists.

The handoff builder should produce the dispatch event payload without losing trusted metadata.

For single prepared text batches, the handoff event may remain the original `PreparedTextEvent`.

For single raw text batches, the handoff event should remain raw so sidecar hydration still happens later.

For single raw text batches, pre-hydration payload metadata may be incomplete.

The dispatch flow must refresh or rebuild payload metadata after hydration and before context extraction.

For media batches, the handoff event should be a synthetic `PreparedTextEvent` prompt while `media_events` carries the original media.

For multi-event text batches, the handoff event may be a synthetic `PreparedTextEvent` prompt.

In all cases, source metadata and policy metadata should travel on `DispatchHandoff`, not only on the event payload.

The handoff builder should preserve attachment IDs explicitly on the handoff payload metadata.

The handoff builder should preserve original sender and raw-audio fallback metadata explicitly on the handoff payload metadata.

The handoff builder should preserve merged mentions, formatted bodies, and skip-mention state explicitly on payload metadata.

Context extraction must consume this payload metadata.

Source synthesis may remain only as an implementation detail for compatibility with existing Matrix event helpers.

Source synthesis must not be the only carrier of source, policy, hook, mention, attachment, or skip-mention metadata.

Do not keep `build_dispatch_handoff()` in `coalescing.py`.

Keeping it in `coalescing.py` would leave the gate module coupled to Matrix payload shape, hook metadata, attachment metadata, mention extraction, and dispatch-envelope concerns.

## Dispatch Flow

`TurnController.handle_coalesced_batch()` should build a `DispatchHandoff`.

Retargeting should use the handoff event and batch metadata as needed.

`HandledTurnState` should still use `source_event_ids` and `source_event_prompts` from the handoff.

Before context extraction, `_dispatch_handoff()` should normalize or hydrate `handoff.event` when it is raw text.

After hydration, `_dispatch_handoff()` should merge payload metadata from the resolved `PreparedTextEvent.source` back into the handoff.

Hydrated metadata should fill any `None` payload fields.

Hydrated metadata should not overwrite trusted queue metadata unless that field was explicitly unknown.

After hydration, `_dispatch_handoff()` should also refresh `source_event_prompts` for the hydrated raw sidecar event.

`HandledTurnState` should not keep preview text when the hydrated body is available.

This post-hydration merge is required before context extraction and final envelope creation.

`_dispatch_text_message()` should accept a handoff or delegate to a new `_dispatch_handoff()` helper.

Direct tests and legacy direct callers may keep using `_dispatch_text_message()` through a thin compatibility wrapper.

`_dispatch_handoff()` should not accept a parallel `queued_notice_reservation` argument.

Cleanup ownership should come from `handoff.dispatch_metadata`.

The core dispatch path should call `_prepare_dispatch()` with trusted `source_kind` and `dispatch_policy_source_kind` from the handoff.

The core dispatch path should also call `_prepare_dispatch()` with trusted `hook_source` and `message_received_depth` from the handoff.

The trusted-router-relay context decision happens before final envelope creation.

That decision must use handoff ingress metadata rather than inspecting only the event payload.

Hook, hook-dispatch, and scheduled events with `ORIGINAL_SENDER_KEY` must not be treated as trusted internal relays before envelope creation.

`ConversationResolver.build_message_envelope()` should receive explicit source metadata for coalesced dispatch.

It may still infer source metadata for direct non-coalesced callers.

Context extraction should receive enough handoff metadata to decide whether to use trusted-router-relay context or normal context.

Normal context extraction should also consume handoff payload metadata.

Do not pass the full `DispatchHandoff` into `ConversationResolver`.

The resolver should not see media events, source-event prompts, queued-human cleanup metadata, or coalescing batch internals.

Add a smaller neutral request type for context extraction and envelope construction.

That request should contain only the hydrated text event, ingress metadata, payload metadata, requester ID, and target/thread inputs needed by the resolver.

Mention detection should use `handoff.payload.mentioned_user_ids` when present.

Formatted-body handling should use `handoff.payload.formatted_bodies` when present.

Skip-mention handling should use `handoff.payload.skip_mentions`.

Attachment handling should use `handoff.payload.attachment_ids`.

This should happen before `MessageEnvelope` exists.

Do not let context extraction depend only on `handoff.event.source` for coalesced or synthesized handoffs.

`ConversationResolver` should not become coalescing-aware.

It should accept neutral dispatch-boundary metadata from its caller instead.

If a resolver API is added, it should take a small neutral context-extraction request, not `DispatchHandoff` or `CoalescedBatch`.

## Queued Human Notice Metadata

The existing queued-human reservation refactor remains valid.

Response lifecycle should own queued-human notice state.

Coalescing should only carry generic `PendingDispatchMetadata`.

Coalescing may close generic metadata when claimed work cannot reach dispatch ownership.

After a handoff is built, `TurnController` should unwrap queued-human reservation metadata from `DispatchHandoff.dispatch_metadata`.

The handoff should be the only post-claim owner of opaque dispatch metadata.

`TurnController` should cancel metadata-owned reservations on terminal dispatch exits before lifecycle consumption.

The handoff refactor should not reintroduce event-id side tables.

## Use Cases That Must Work

Normal text should hydrate long-text sidecars and dispatch as `source_kind="message"`.

Scheduled text should hydrate long-text sidecars and dispatch as `source_kind="scheduled"`.

Scheduled text with `ORIGINAL_SENDER_KEY` should still dispatch as `source_kind="scheduled"`.

Hook text should hydrate long-text sidecars and dispatch as `source_kind="hook"` or `source_kind="hook_dispatch"`.

Hook text with `ORIGINAL_SENDER_KEY` should not be relabeled as `trusted_internal_relay`.

Hook text should preserve `hook_source` and `message_received_depth`.

Trusted internal relay text without `com.mindroom.source_kind` should dispatch as `source_kind="trusted_internal_relay"`.

Voice text should dispatch as `source_kind="voice"` and should not be parsed as a command.

Standalone image events should dispatch as `source_kind="image"` with no active-follow-up policy.

Standalone file and video events should dispatch as `source_kind="media"` with no active-follow-up policy.

Voice active-thread follow-up should dispatch as `source_kind="voice"` and `dispatch_policy_source_kind="active_thread_follow_up"`.

Image active-thread follow-up should dispatch as `source_kind="image"` and `dispatch_policy_source_kind="active_thread_follow_up"`.

File or video active-thread follow-up should dispatch as `source_kind="media"` and `dispatch_policy_source_kind="active_thread_follow_up"`.

Sidecar text preview active-thread follow-up should dispatch with its text source kind and `dispatch_policy_source_kind="active_thread_follow_up"`.

Sidecar-backed mentions should survive hydration, context extraction, and final envelope creation.

Sidecar-backed formatted bodies should survive hydration, context extraction, and final envelope creation.

Sidecar-backed skip-mention metadata should survive hydration, context extraction, and final envelope creation.

Coalesced media batches should preserve original media in `media_events`.

Coalesced batches should preserve attachment IDs in the final `MessageEnvelope`.

Coalesced batches should preserve non-primary mentions for context extraction and final envelope construction.

Coalesced batches should preserve formatted bodies for context extraction and final envelope construction.

Coalesced batches should preserve skip-mention state.

Coalesced batches should preserve original sender and raw-audio fallback metadata when those values are present.

Coalesced batches should preserve FIFO source-event order.

Queued-human notice reservations should be consumed when response lifecycle ownership begins.

Queued-human notice reservations should be canceled when dispatch exits before response lifecycle ownership.

## Pre-Handoff Preservation

The handoff cannot recover metadata from events that were dropped before enqueue.

Ingress prechecks must preserve trusted source metadata before normalization and coalescing.

The early router skip path must also be covered.

`_should_skip_router_before_shared_ingress_work()` currently runs before hydration, enqueue, and handoff construction.

That decision must either move after trusted preview metadata extraction, or receive a pre-handoff metadata request that includes trusted source kind, hook metadata, mentions, formatted bodies, and skip-mention state.

It must not depend only on preview `event.body` and raw `event.source` when the full content is sidecar-backed.

Oversized hook and hook-dispatch previews must carry enough trusted metadata for `_precheck_event()` to avoid dropping self-authored hook output.

Oversized trusted-internal-relay previews must carry enough trusted metadata to preserve original sender and relay classification before the handoff exists.

Large-message preview metadata should preserve `com.mindroom.source_kind`, hook source, message-received depth, attachment IDs, skip-mention state, original sender, and other trusted payload metadata when those values are trusted internal metadata.

If the sidecar preview format does not currently carry those values, update the preview writer and reader before relying on the handoff.

Self-authored events with trusted `hook_dispatch` metadata should still pass precheck even when the visible preview event is oversized.

Oversized scheduled, hook, hook-dispatch, and trusted-internal-relay content should be tested from the actual sidecar writer through the actual preview reader.

This is a blocker because a coalescing-to-dispatch handoff starts too late to recover an event that precheck already ignored.

## Implementation Steps

### Step 1: Add The Handoff Type

Add a neutral dispatch-boundary module.

Move `PreparedTextEvent`, `TextDispatchEvent`, `MediaDispatchEvent`, and `DispatchEvent` out of `coalescing.py` into that module.

Add `DispatchIngressMetadata`, `DispatchPayloadMetadata`, and `DispatchHandoff` to that module.

Keep them as dataclasses with typed fields.

Do not add methods unless they remove real duplication.

Make `DispatchHandoff.event` a `TextDispatchEvent`.

Do not make dispatch preparation accept media events directly through the handoff event field.

### Step 2: Build Handoffs From Batches

Add `build_dispatch_handoff(batch)`.

Place `build_dispatch_handoff(batch)` in the dispatch-boundary module.

Move event synthesis decisions behind this function.

Preserve raw text for single raw text batches.

Preserve prepared text for single prepared text batches.

Create synthetic prepared text for multi-event text batches and media batches.

Carry trusted ingress metadata, payload metadata, `media_events`, `source_event_ids`, `source_event_prompts`, and `dispatch_metadata` on the handoff.

Carry attachment IDs explicitly on the handoff.

Carry hook source and message-received depth explicitly on the handoff.

Carry mentions, formatted bodies, and skip-mention state explicitly on the handoff.

Carry original sender and raw-audio fallback explicitly on the handoff.

Do not make synthetic event source the only carrier for these values.

### Step 3: Refresh Metadata After Hydration

When `_dispatch_handoff()` receives a handoff whose event is raw text, it should call the normalizer before context extraction.

After normalization, rebuild or update the handoff with the hydrated `PreparedTextEvent`.

Merge metadata extracted from the hydrated source into `DispatchPayloadMetadata`.

Only fill metadata fields that were `None`.

Do not treat pre-hydration empty tuples as authoritative for sidecar-backed events unless the preview format proves the full content was inspected.

This step is required for sidecar-backed mentions, formatted-body pills, skip-mentions, attachment IDs, and other payload metadata that may not exist on the preview event.

Refresh `source_event_prompts` from the hydrated text for raw sidecar handoffs.

Do not let edit or regeneration metadata keep the preview body once full sidecar content has been loaded.

### Step 4: Thread The Handoff Through Turn Controller

Change `handle_coalesced_batch()` to pass the handoff into dispatch.

Prefer adding `_dispatch_handoff(handoff, handled_turn)` and letting `_dispatch_text_message()` be a direct-call wrapper.

`_dispatch_handoff()` should own metadata cleanup by reading `handoff.dispatch_metadata`.

Avoid growing `_dispatch_text_message()` with several optional metadata parameters.

### Step 5: Prepare Context And Envelopes From Handoff Metadata

Change `_prepare_dispatch()` or its call site so `build_message_envelope()` receives handoff `source_kind`.

Pass handoff `dispatch_policy_source_kind` into `build_message_envelope()`.

Pass handoff `hook_source` and `message_received_depth` into envelope creation or into an explicit metadata parameter used by envelope creation.

Pass `handoff.payload.attachment_ids` into envelope creation.

Pass handoff payload metadata into context extraction before envelope creation.

Pass a small context extraction request to `ConversationResolver`, not the full handoff.

Use handoff attachment metadata for both `MessageEnvelope.attachment_ids` and dispatch payload construction.

Use handoff mention metadata for mention detection.

Use handoff skip-mention metadata for skip-mention behavior.

Use handoff source metadata for the trusted-router-relay context decision that happens before envelope creation.

Do not rely on `PreparedTextEvent.source_kind_override` to recover queue-owned metadata.

Keep trusted Matrix content fallback only for direct non-coalesced paths.

### Step 6: Remove Policy From Event Overrides

Stop assigning `active_thread_follow_up` to `PreparedTextEvent.source_kind_override`.

Stop using `PreparedTextEvent.dispatch_policy_source_kind_override` on the coalesced path.

Remove `PreparedTextEvent.dispatch_policy_source_kind_override` from the shared event type and all callers.

Wrap direct callers into `DispatchHandoff` or pass explicit neutral ingress metadata.

The mergeable endpoint is that no production path or test fixture can express dispatch policy through `PreparedTextEvent`.

### Step 7: Update Replay And Newer-Message Guards

Update replay and newer-message guards that currently inspect `PreparedTextEvent.source_kind_override`.

They should use handoff ingress metadata or the final `MessageEnvelope`.

Scheduled, hook, and hook-dispatch events should not be incorrectly suppressed because their source metadata moved off the event payload.

### Step 8: Consolidate Active-Follow-Up Enqueue

Keep one shared enqueue path for prepared text, voice-prepared text, sidecar-prepared text, and standalone media.

That helper should decide whether the event is an active-thread follow-up.

It should reserve the queued-human notice when needed.

It should enqueue with real `source_kind` plus optional `dispatch_policy_source_kind="active_thread_follow_up"`.

It should not enqueue with real `source_kind="active_thread_follow_up"` for human message modalities.

It should enqueue hook source and message-received depth when the event carries trusted hook metadata.

It should enqueue attachment and relay payload metadata when that metadata is already known.

### Step 9: Define Mixed-Source Batch Semantics

Make `scheduled`, `hook`, `hook_dispatch`, and `trusted_internal_relay` FIFO bypass or solo source kinds in this refactor.

They should not coalesce with human messages.

If a future product need requires mixed-source coalescing, add per-event source metadata to the handoff and define deterministic final-envelope semantics in a separate change.

Add regressions proving scheduled, hook, hook-dispatch, and trusted-relay plus human FIFO ingress dispatch as separate turns.

Each regression should assert the final envelope source kind for the automation or relay turn is preserved.

### Step 10: Clean Up Old Coupling

Remove tests and code that assert policy source kinds appear as real source kinds.

Update docs that describe hook transport objects only if the implementation changes the public contract.

Do not expand `ConversationResolver` into a coalescing-aware module.

Do not make `response_lifecycle.py` know about coalescing.

## Test Plan

Add or update focused tests for final envelope behavior.

Test a raw trusted internal relay through the gate into `_prepare_dispatch()` and assert the final `MessageEnvelope.source_kind` is `trusted_internal_relay`.

Test a trusted internal relay handoff and assert the trusted-router-relay context extraction path is used.

Test hook and scheduled handoffs with `ORIGINAL_SENDER_KEY` and assert the normal context extraction path is used.

Test hook and hook-dispatch messages with `ORIGINAL_SENDER_KEY` and assert they remain `hook` and `hook_dispatch`.

Test hook messages through the gate and assert final `MessageEnvelope.hook_source` and `MessageEnvelope.message_received_depth` are preserved.

Test oversized hook content generated by the actual large-message sidecar writer and read through the actual sidecar preview reader.

Assert that oversized hook content passes precheck and the final envelope preserves `source_kind`, `hook_source`, and `message_received_depth`.

Test oversized hook-dispatch content generated by the actual large-message sidecar writer and read through the actual sidecar preview reader.

Assert that oversized hook-dispatch content passes precheck and the final envelope preserves `source_kind`, `hook_source`, and `message_received_depth`.

Test oversized trusted-internal-relay content generated by the actual large-message sidecar writer and read through the actual sidecar preview reader.

Assert that oversized trusted-internal-relay content passes precheck, preserves original sender, and reaches a final envelope with `source_kind="trusted_internal_relay"`.

Test plain scheduled messages and assert final `MessageEnvelope.source_kind` is `scheduled`.

Test scheduled messages with `ORIGINAL_SENDER_KEY` and assert final `MessageEnvelope.source_kind` is still `scheduled`.

Test oversized scheduled content generated by the actual large-message sidecar writer and assert precheck passes and final `MessageEnvelope.source_kind` is `scheduled`.

Test a normal sidecar-backed raw text event and assert the hydrated body reaches dispatch and final `MessageEnvelope.source_kind` is `message`.

Test a scheduled sidecar-backed raw text event and assert the hydrated body reaches dispatch and final `MessageEnvelope.source_kind` is `scheduled`.

Test a hook sidecar-backed raw text event and assert the hydrated body reaches dispatch and final `MessageEnvelope.source_kind` is `hook`.

Test the same hook sidecar-backed raw text event and assert final `MessageEnvelope.hook_source` and `MessageEnvelope.message_received_depth` are preserved.

Test a hook-dispatch sidecar-backed raw text event and assert the hydrated body reaches dispatch and final `MessageEnvelope.source_kind` is `hook_dispatch`.

Test the same hook-dispatch sidecar-backed raw text event and assert final `MessageEnvelope.hook_source` and `MessageEnvelope.message_received_depth` are preserved.

Test sidecar-backed attachment IDs and assert final `MessageEnvelope.attachment_ids` preserves them.

Test sidecar-backed attachment IDs and assert the model dispatch payload preserves them.

Test sidecar-backed mentions and assert context extraction preserves `mentioned_agents` and `am_i_mentioned`.

Test sidecar-backed formatted-body pills and assert context extraction preserves mention semantics.

Test sidecar-backed skip-mention metadata and assert context extraction honors it.

Test untrusted user-authored raw events spoofing `com.mindroom.source_kind`, hook source, message-received depth, and active-follow-up policy.

Assert the final envelope remains normal user ingress.

Test untrusted user-authored raw events spoofing `com.mindroom.attachment_ids`, `com.mindroom.original_sender`, raw-audio fallback, mentions, formatted bodies, and skip-mention metadata.

Assert spoofed internal payload keys do not become trusted final-envelope or model-payload metadata.

Test untrusted user-authored sidecar-backed events spoofing `com.mindroom.source_kind`, hook source, message-received depth, and active-follow-up policy.

Assert the final envelope remains normal user ingress after hydration.

Test untrusted user-authored sidecar-backed events spoofing `com.mindroom.attachment_ids`, `com.mindroom.original_sender`, raw-audio fallback, mentions, formatted bodies, and skip-mention metadata.

Assert spoofed internal payload keys do not become trusted final-envelope or model-payload metadata after hydration.

Test a normal image event through the gate and assert final `MessageEnvelope.source_kind` is `image`, final `MessageEnvelope.dispatch_policy_source_kind` is `None`, and original `media_events` are preserved.

Test a normal file or video event through the gate and assert final `MessageEnvelope.source_kind` is `media`, final `MessageEnvelope.dispatch_policy_source_kind` is `None`, and original `media_events` are preserved.

Test an active image follow-up through the gate and assert final `MessageEnvelope.source_kind` is `image`.

Test the same active image follow-up and assert final `MessageEnvelope.dispatch_policy_source_kind` is `active_thread_follow_up`.

Test an active file or video follow-up through the gate and assert final `MessageEnvelope.source_kind` is `media`.

Test the same active file or video follow-up and assert final `MessageEnvelope.dispatch_policy_source_kind` is `active_thread_follow_up`.

Test an active voice follow-up through the gate and assert final `MessageEnvelope.source_kind` is `voice`.

Test the same active voice follow-up and assert final `MessageEnvelope.dispatch_policy_source_kind` is `active_thread_follow_up`.

Test a sidecar-text active follow-up and assert final `MessageEnvelope.source_kind` is its real text source kind.

Test the same sidecar-text active follow-up and assert final `MessageEnvelope.dispatch_policy_source_kind` is `active_thread_follow_up`.

Test active voice text starting with `!help` and assert it is not parsed as a command.

Test non-active voice text starting with `!help` and assert it is not parsed as a command.

Test an active text follow-up and assert source kind remains `message` while policy source kind is `active_thread_follow_up`.

Test a coalesced batch with attachments and assert final `MessageEnvelope.attachment_ids` contains all batch attachment IDs.

Test a coalesced batch with attachments from multiple source events and assert the model dispatch payload preserves all attachment IDs.

Test a raw sidecar-backed single event and assert hydrated `source_event_prompts` replace preview text in `HandledTurnState`.

Test a coalesced batch where only a non-primary event mentions the agent and assert context extraction preserves `mentioned_agents` and `am_i_mentioned`.

Test a coalesced batch with formatted bodies from multiple events and assert context extraction and final envelope preserve formatted mention semantics.

Test a coalesced batch with skip-mention metadata and assert context extraction honors it.

Test a coalesced relay or voice batch and assert original sender and raw-audio fallback metadata survive where applicable.

Test scheduled and hook events with newer thread history and assert replay or newer-message guards do not suppress them because source metadata moved off `PreparedTextEvent`.

Test oversized scheduled, hook, hook-dispatch, and trusted-relay sidecar previews through the early router skip path.

Assert those events remain reachable for final envelope creation with correct mention, skip, source, and hook metadata.

Test a normal user sidecar-backed formatted-body pill or mention through the early router skip path.

Assert the router skip decision does not drop the event before hydration and final context extraction.

Test scheduled plus human FIFO ingress and assert scheduled dispatches solo rather than coalescing into a mixed-source batch.

Test hook plus human FIFO ingress and assert hook dispatches solo with final `MessageEnvelope.source_kind="hook"`.

Test hook-dispatch plus human FIFO ingress and assert hook-dispatch dispatches solo with final `MessageEnvelope.source_kind="hook_dispatch"`.

Test trusted-relay plus human FIFO ingress and assert trusted relay dispatches solo with final `MessageEnvelope.source_kind="trusted_internal_relay"`.

Test queued-human reservation cancellation when handoff construction or retargeting fails after claim.

Test queued-human reservation cancellation when dispatch exits before response lifecycle ownership.

Test queued-human reservation consumption when response lifecycle ownership begins.

Keep the FIFO tests that assert coalesced source event order follows queue order, not Matrix server timestamp order.

## Commit Strategy

Do the handoff refactor and tests in one commit if the diff stays focused.

Use a second cleanup commit if removing transitional event override fields or old tests makes the first diff hard to review.

Do not commit failing tests.

Do not hide the design change inside unrelated Kubernetes, docs, or release changes.

## Definition Of Done

The coalesced dispatch path has a typed `DispatchHandoff`.

Shared dispatch event and handoff types live in a neutral dispatch-boundary module, not in `coalescing.py`.

`ConversationResolver` and `InboundTurnNormalizer` do not import dispatch event types from `coalescing.py`.

`DispatchHandoff.event` is a `TextDispatchEvent`.

Original media travels through `DispatchHandoff.media_events`.

The final envelope for coalesced dispatch receives source metadata from the handoff.

The final envelope for coalesced dispatch receives hook metadata from the handoff.

The final envelope for coalesced dispatch receives dispatch policy metadata from the handoff.

Pre-envelope context extraction uses handoff source metadata for trusted-router-relay decisions.

Pre-envelope context extraction uses handoff payload metadata for mentions, formatted bodies, skip-mention behavior, and attachments.

Sidecar-backed handoffs refresh payload metadata after hydration and before context extraction.

Unknown payload metadata is represented distinctly from authoritative absence.

The early router skip path is covered by trusted preview metadata or moved after metadata extraction.

`active_thread_follow_up` is not used as real source kind for message, voice, image, or media follow-ups.

Single raw text batches can stay raw without losing trusted queue-owned source metadata.

Media batches can synthesize a text prompt without losing media modality or active-follow-up policy.

Voice batches preserve voice command behavior and active-follow-up policy separately.

Hook and hook-dispatch source kinds survive `ORIGINAL_SENDER_KEY`.

Hook source and message-received depth survive coalescing.

Scheduled source kind survives `ORIGINAL_SENDER_KEY`.

Coalesced attachment IDs survive into the final `MessageEnvelope`.

Coalesced attachment IDs survive into model payload construction.

Coalesced non-primary mentions and formatted bodies survive context extraction.

Skip-mention metadata survives context extraction.

Oversized scheduled, hook, hook-dispatch, and trusted-internal-relay previews preserve trusted metadata before precheck.

Untrusted raw and sidecar-backed spoofed metadata does not become trusted handoff, envelope, or model-payload metadata.

Scheduled, hook, hook-dispatch, and trusted internal relay events dispatch solo instead of joining human-message batches.

Queued-human notice reservations remain lifecycle-owned and exception-safe.

`PreparedTextEvent.source_kind_override` no longer carries dispatch policy.

No production path reads dispatch policy from `PreparedTextEvent`.

Hydrated sidecar source-event prompts replace preview source-event prompts before `HandledTurnState` is used.

No review fix should require asking whether `source_kind` means modality or policy.

## Accountability Checks

Search for `source_kind=COALESCING_BYPASS_ACTIVE_THREAD_FOLLOW_UP`.

No production path or test fixture should use active-follow-up policy as `source_kind`.

Search for `dispatch_policy_source_kind_override`.

It must be gone from production code and tests.

Search for `source_kind_override=COALESCING_BYPASS_ACTIVE_THREAD_FOLLOW_UP`.

There should be no production uses.

Inspect `_prepare_dispatch()`.

It should receive source metadata from the handoff for coalesced dispatch.

It should use source metadata from the handoff for trusted-router-relay context decisions.

Inspect context extraction.

It should consume handoff payload metadata for mentions, formatted bodies, skip-mention behavior, and attachments.

It should receive a small neutral request, not full `DispatchHandoff`.

Inspect `build_message_envelope()` calls from coalesced dispatch.

They should pass explicit `source_kind`, `dispatch_policy_source_kind`, hook source, message-received depth, and attachment IDs.

Inspect active media, voice, sidecar text, and normal text enqueue paths.

They should share the same active-follow-up reservation decision.

Inspect final-envelope tests.

They should cover normal sidecar text, scheduled sidecar text, hook sidecar text, hook-dispatch sidecar text, trusted internal relay, active voice, non-active voice command suppression, active image, active file or video, normal image, normal file or video, hook depth, coalesced attachment IDs, coalesced model payload attachment IDs, source-event prompt hydration, sidecar-backed mentions, sidecar-backed formatted bodies, sidecar-backed skip-mentions, coalesced non-primary mentions, formatted bodies, skip-mentions, spoofed untrusted source metadata, spoofed untrusted payload metadata, early router skip, solo scheduled dispatch, solo hook dispatch, solo hook-dispatch dispatch, solo trusted-relay dispatch, oversized hook sidecar metadata, and oversized trusted-relay sidecar metadata.

Run focused tests for hooks, queued notices, live message coalescing, and multi-agent active follow-ups.

Run full pytest with `-n auto`.

Run `ruff`, `git diff --check`, `tach check`, and pre-commit before pushing.

## Suggested Reviewer Prompt

Review the dispatch handoff source and policy refactor plan in `docs/dev/dispatch-handoff-source-policy-refactor-plan.md`.

Focus on whether the proposed `DispatchHandoff` seam fits the existing MindRoom architecture.

Check whether it covers raw text sidecar hydration, post-hydration metadata refresh, source-event prompt hydration, scheduled and hook messages, trusted internal relays, hook source and depth, voice command suppression, standalone media follow-ups, active-thread follow-up policy, queued-human notice cleanup, coalesced attachment IDs, model payload attachment IDs, coalesced mentions, sidecar mentions, formatted bodies, skip-mentions, early router skip behavior, spoofed untrusted source metadata, spoofed untrusted payload metadata, hooks, turn policy, and FIFO coalescing.

Call out any use case where trusted `source_kind`, hook metadata, payload metadata, or `dispatch_policy_source_kind` would still be lost before `MessageEnvelope` creation.

Call out any use case where precheck or sidecar preview generation can drop trusted metadata before the handoff exists.

Call out any pre-envelope consumer that still depends only on event source instead of handoff metadata.

Call out any post-hydration metadata or `source_event_prompts` value that is unavailable when the handoff is first built and not refreshed before context extraction.

Call out any place where the plan still makes `PreparedTextEvent` carry policy instead of payload.

Call out any place where pre-envelope context extraction still inspects the event instead of handoff metadata.

Call out if the plan still leaves `build_dispatch_handoff()` or shared dispatch event types in `coalescing.py`.

Call out if `ConversationResolver` still receives full `DispatchHandoff` instead of a smaller neutral request.

Call out if scheduled, hook, hook-dispatch, or trusted relay events can still coalesce with human messages without per-event final source semantics.

Call out any module that would still need to know too much about another module after this refactor.

Treat missing final-envelope tests as blockers.

Do not focus on style unless it affects the seam or reviewability.
