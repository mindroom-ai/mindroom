# Turn Pipeline Lifecycle Refactor Plan

## Decision

Refactor the inbound turn pipeline by consolidating around the authorities that already exist.
Do not add a parallel lifecycle framework, universal settlement object, or replacement state machine.

The main new ingress concept is one canonical prepared event that survives coalescing without being normalized again.
The response and durability work should evolve `response_turn.py`, `FinalDeliveryOutcome`, `TurnStore`, and `TurnSettlementRetry` rather than replacing them.

The work should land in one pull request built from small ordered commits.
Every commit must preserve the focused behavior under change, and the completed pull request must remove all superseded paths, temporary adapters, and duplicate authorities before review.

## Problem

The inbound turn pipeline is the hardest part of MindRoom to understand because one logical turn crosses callback durability, trust validation, requester resolution, receipt-order lanes, conversation resolution, coalescing, policy, response locking, provider attempts, Matrix delivery, and durable settlement.
The local modules are generally well-factored, but the handoffs between them are difficult to reconstruct.

The highest-cost ambiguities are the following.

1. Receipt ordering, coalescing, and response serialization use different identities and provide different guarantees.
2. Ordinary text is normalized before coalescing and then normalized again during dispatch because the queue retains the raw event rather than the prepared result.
3. Per-source identity, callback settlement identity, trust, policy source, replay metadata, and merged prompt metadata travel through mutable parallel fields.
4. The outer agent/team and streaming/blocking settlement matrix repeats delivery and terminal handling around an already-shared inner response driver.
5. Blocking and streaming delivery use overlapping event-ID, visibility, cancellation, and replay-text rules.
6. Durable turn ownership and callback-obligation settlement are correct but difficult to understand because live claims, durable pending turns, deferred obligations, terminal records, and tombstones have different owners.

## Existing Authorities to Preserve

- `DispatchObligation` and `DispatchObligationKey` represent exact durable callback acceptance.
- `DispatchSemanticConsumer` selects one consumer only for multi-purpose approval and reaction callbacks.
- `PreparedTextEvent`, `PendingEvent`, `CoalescedBatch`, and `DispatchHandoff` are the current ingress handoff types.
- `CoalescingKey` is the existing requester-scoped batching identity.
- `MessageTarget` is the existing authoritative delivery target.
- `response_turn.py` already owns shared blocking and streaming attempt, retry, and continuation drivers.
- `StreamTransportOutcome` and `FinalDeliveryOutcome` already distinguish transport and final delivery facts.
- `TurnStore._record_terminal_turn` is already the terminal turn-record merge chokepoint.
- `TurnSettlementRetry` already bridges terminal ledger persistence back to deferred obligation settlement.

The refactor should make these authorities narrower and easier to connect.
It should not create similarly named replacements beside them.

## Scope and LOC Expectations

The scope is the path in `docs/architecture/bot-runtime.md` from exact callback acceptance through terminal turn persistence.
The current production boundary is approximately 22,000 to 24,000 physical source lines depending on whether adjacent recovery helpers are included.
Phase 0 will add one checked manifest so later measurements use an exact file list.

At reviewed commit `a88da5ae6`, the core pipeline manifest is the following 34 files and totals 22,644 physical source lines.

```text
src/mindroom/turn_controller.py
src/mindroom/response_runner.py
src/mindroom/response_turn.py
src/mindroom/response_lifecycle.py
src/mindroom/response_terminal.py
src/mindroom/response_attempt.py
src/mindroom/response_payload_preparation.py
src/mindroom/execution_preparation.py
src/mindroom/ingress_validation.py
src/mindroom/inbound_turn_normalizer.py
src/mindroom/conversation_resolver.py
src/mindroom/ingress_lanes.py
src/mindroom/coalescing.py
src/mindroom/coalescing_batch.py
src/mindroom/text_ingress_dispatch.py
src/mindroom/turn_policy.py
src/mindroom/dispatch_handoff.py
src/mindroom/dispatch_replay_guard.py
src/mindroom/command_turn_executor.py
src/mindroom/reaction_dispatch.py
src/mindroom/user_stop_reconciliation.py
src/mindroom/visible_response_reconciliation.py
src/mindroom/turn_store.py
src/mindroom/handled_turns.py
src/mindroom/redacted_turn_cleanup.py
src/mindroom/sync_restart_retry.py
src/mindroom/turn_settlement_retry.py
src/mindroom/delivery_gateway.py
src/mindroom/final_delivery.py
src/mindroom/streaming.py
src/mindroom/edit_regenerator.py
src/mindroom/message_target.py
src/mindroom/prompt_ingress_reservation.py
src/mindroom/dispatch_callback_outcome.py
```

Including all four Python files in `src/mindroom/dispatch_obligations/` adds 1,718 lines and produces the adjacent-recovery boundary of 24,362 lines.
The mixed-purpose composition and entity files `bot.py`, `ai.py`, and `teams.py` are excluded from this whole-file boundary because most of their contents are outside the turn pipeline, but every changed source line in them still counts toward churn and the final net `src/` delta.

Expected production-code churn is 5,000 to 8,000 lines.
Expected test churn is another 4,000 to 7,000 lines because the focused suites contain many direct dependency-object constructions and private response-runner calls.
Expected net production reduction is 1,000 to 2,500 lines, with a central estimate near 1,700 lines.
Absolute file lengths are tracking metrics rather than merge gates.

## Goals

- Carry one canonical prepared ingress representation through ordinary coalescing and policy.
- Preserve per-source identity and recovery evidence instead of flattening it into merged prompt metadata.
- Name the receipt-lane identity and replace synthetic follow-up requester strings with an explicit batching-owner variant.
- Centralize delivery failure classification and final event-ID precedence.
- Remove outer response settlement duplication without replacing the existing shared response drivers.
- Document the real durable transitions for ordinary turns and for multi-purpose control callbacks separately.
- Make corrupt callback obligations visible as quarantined operator state.
- Keep `turn_controller.py`, `response_runner.py`, and `bot.py` focused on coordination and dependency wiring.

## Non-Goals

- Changing user-visible batching, routing, cancellation, streaming, command, or recovery behavior.
- Rewriting Matrix sync, the event cache, model execution, team collaboration, or the tool system.
- Introducing a workflow engine, event-sourcing framework, generic pipeline abstraction, or new persistence service.
- Creating one mutable object that represents every transient state of a turn.
- Replacing `MessageTarget`, `FinalDeliveryOutcome`, `TurnStore`, or `TurnSettlementRetry` merely to improve naming.
- Forcing command journals, edit regeneration, redaction, user stop, and ordinary responses through one end-of-turn decision shape.
- Migrating durable storage unless a separately reviewed invariant requires it.

## Runtime Contracts

### Ordering identities

Introduce a frozen `ReceiptLaneKey(room_id, physical_sender_id)` in place of the private bare tuple.
Replace the requester string in `CoalescingKey` with a discriminated `CoalescingOwner` union containing `RequesterCoalescingOwner(requester_user_id)` and `ActiveFollowUpCoalescingOwner`.
Construct every coalescing key through one `derive_coalescing_key(room_id, thread_id, owner)` function so requester and active-follow-up owners cannot compare equal even when a requester ID resembles the old reserved prefix.
Continue using `MessageTarget` as the delivery identity and derive the response-lifecycle lock key through one method on that type or its owner.

These identities remain separate because they protect different invariants.
The refactor names and centralizes their derivation but does not merge their mechanisms.

### Prepared ingress

Rename and evolve the source-level `PreparedTextEvent` into one frozen `PreparedIngress` type that is the sole owner of the canonical prepared value.
`PreparedIngress` must retain the raw Matrix protocol reference where replies, edits, encryption, or cache operations require it.
It must also retain physical sender, effective requester, trusted original sender, source event ID, normalized prompt, trust evidence, callback settlement source kind, policy source kind, hook source, recovery flags, and opaque claim metadata as named nested values.

`PendingEvent` must reference one `PreparedIngress` plus queue-local mutable lifecycle state without copying canonical fields.
`CoalescedBatch` must retain a tuple of `PreparedIngress` values plus only derived batch presentation and ownership state.
`DispatchHandoff` must transport those canonical values and derived batch facts without duplicating normalized or attribution fields.
The merged prompt is a presentation derived from those sources, not a replacement for their identities.
Fail-closed requester attribution and persisted replay reconstruction must continue to use per-source evidence.

### Response execution and delivery

Keep `run_blocking_response_turn` and `stream_response_turn` as the shared inner drivers.
Shrink the current callback-heavy `BlockingTurnAdapter` and `StreamingTurnAdapter` surfaces only where an existing typed request or outcome can replace several callbacks.
Do not create another lifecycle driver above them.

Evolve `StreamTransportOutcome` and `FinalDeliveryOutcome` in place.
The delivery contract must continue to distinguish physical event existence, placeholder-only visibility, substantive visibility, terminal visibility, canonical provider text, rendered Matrix text, replayable assistant text, and final response event ID.

### Durable ordinary-turn sequence

The ordinary message and media path follows this sequence.

```text
dispatch obligation pending
  -> in-process callback execution claim
  -> pending physical-source turn claim
  -> normalization and ingress admission under that claim
  -> callback obligation deferred while downstream owns work
  -> durable pending TurnStore record when response ownership begins
  -> durable terminal TurnStore record notifies TurnSettlementRetry
  -> TurnSettlementRetry queues source IDs and calls obligation-storage settlement
  -> obligation storage verifies terminal truth and writes the tombstone
```

The pending claim must be acquired before normalization and released on every non-admission or failure path.
The exact crash guarantee is durable callback acceptance with retryable callback execution, followed by durable turn ownership and terminal deduplication once the turn record exists.

### Multi-purpose callback sequence

Approval replies and reactions can have several semantic consumers.
For those callback families only, `DispatchSemanticConsumer` is the durable authority that prevents a second consumer from claiming the same callback.
It is not the ordinary message-turn ownership token.

### Settlement topology

Keep `TurnStore._record_terminal_turn` as the terminal record chokepoint.
Inventory all terminal writers before changing its input surface, including commands, edit regeneration, normal dispatch, user stop, redaction, and reaction paths.
Extract shared `TurnRecord` construction helpers only where two or more writers create the same semantic outcome.
Do not require command journal milestones or other mid-turn durable records to fit one terminal-only value.

## Phase 0: Freeze Missing Invariants and Measurements

### Changes

- Add the missing glossary and corrected ordinary-turn and multi-purpose-callback diagrams to `docs/architecture/bot-runtime.md` rather than creating a second source of truth.
- Materialize the 34-file core boundary and four-file adjacent-recovery boundary above as the exact checked pipeline manifest, and add a checked LOC-report command.
- Create a terminal-write call-site inventory covering `turn_controller.py`, `text_ingress_dispatch.py`, `command_turn_executor.py`, `edit_regenerator.py`, reaction handling, user stop, redaction, and recovery.
- Create a method-ownership ledger for code expected to leave `turn_controller.py` and `response_runner.py`.
- Reuse the existing restart harnesses and injectable coalescing controls instead of adding a production seam-hook framework.
- Mark existing crash and ordering tests so the phase adds only missing coverage.

### New invariant tests

- A pending turn claim is acquired before text or media normalization and released on every non-admission path.
- Persisted replay text remains byte-stable relative to the live model-facing prompt where the contract requires equivalence.
- Dynamic-tool continuation does not call final delivery at the gateway seam between attempts.
- Cancellation-note markers remain compatible with `stale_stream_cleanup.py` recovery matching.
- `settle_pending_from_turn_store` does not compact a pending callback whose callback body has not run.
- Lifecycle-lock table eviction cannot evict a target with a live queued signal or active lock.
- User-stop visible reconciliation and durable terminal reconciliation converge in one cross-module test.

### Existing behavior to cite rather than duplicate

- Obligation persistence followed by crash and restart.
- Downstream ownership followed by crash and restart.
- Terminal turn persistence before obligation tombstoning.
- Voice readiness followed by same-sender text ordering.
- Distinct requester batching with shared response serialization.
- Command bypass of ordinary coalescing.

### Exit gate

The current implementation passes every characterization test.
The LOC manifest, terminal-writer inventory, and method-ownership ledger are reviewed before numeric reduction goals are used.
`uv run tach check --dependencies --interfaces` passes.

## Phase 1: Re-ground the Ingress Types and Ordering Keys

### Changes

- Replace the private lane tuple with `ReceiptLaneKey`.
- Replace the active-follow-up requester prefix with the discriminated `CoalescingOwner` union and the sole `derive_coalescing_key` constructor.
- Rename and evolve `PreparedTextEvent` into the sole canonical `PreparedIngress` value.
- Reduce `PendingEvent`, `CoalescedBatch`, and `DispatchHandoff` to wrappers that carry `PreparedIngress` values plus only their own queue, batch, or handoff state.
- Preserve mutable claim-closing and busy-rerouting behavior until explicit functional replacements exist.
- Map each changed type to its predecessor and identify the later commit in the same pull request that deletes any temporary predecessor.
- Preserve `CoalescingKey`, `MessageTarget`, `DispatchObligation`, `FinalDeliveryOutcome`, and `TurnRecord` as existing authorities.

### Primary files

- `src/mindroom/dispatch_handoff.py`
- `src/mindroom/coalescing_batch.py`
- `src/mindroom/ingress_lanes.py`
- `src/mindroom/message_target.py`
- `src/mindroom/prompt_ingress_reservation.py`
- `tach.toml`

### Exit gate

No parallel old/new type family survives without a dated deletion step.
Per-source sender and callback-settlement evidence remain available after coalescing.
The import-graph tests and Tach checks pass.

### Expected net production delta

Between 100 lines added and 200 lines removed.

## Phase 2: Normalize Ordinary Ingress Once

### Changes

- Carry the prepared event created during lane-delivered admission into coalescing instead of enqueueing only the raw event.
- Build preparation under an already-held pending turn claim for both text and media.
- Release the claim on every ignored, failed, abandoned, superseded, or non-admitted path.
- Preserve enqueue-time trusted-relay source promotion as an explicit transformation.
- Preserve callback settlement source kind for voice and media fallback handling.
- Preserve per-source attribution, recovery flags, and opaque metadata close ownership through the batch.
- Remove the second ordinary normalization call from `text_ingress_dispatch.py`.
- Include synthetic interactive-selection and edit-regeneration inputs in the preparation inventory.

### Primary files

- `src/mindroom/turn_controller.py`
- `src/mindroom/inbound_turn_normalizer.py`
- `src/mindroom/conversation_resolver.py`
- `src/mindroom/coalescing.py`
- `src/mindroom/coalescing_batch.py`
- `src/mindroom/text_ingress_dispatch.py`
- `src/mindroom/edit_regenerator.py`
- `src/mindroom/prompt_ingress_reservation.py`

### Tests

- The ordinary text normalizer is called exactly once per live turn.
- Text, media, voice, edit, reply, room, thread, relay, hook, and recovery inputs retain current behavior.
- Gate debounce can still inspect undelivered lane state without violating receipt order.
- Busy-conversation rerouting preserves policy-source promotion.
- Every coalesced source remains individually attributable to the effective requester.
- Replay can construct the prepared input without requiring a live `nio` event.

### Exit gate

Ordinary dispatch does not carry two independently authoritative normalized representations beyond coalescing.
No raw protocol reference remains unless a named downstream protocol operation uses it.
Tach and focused ingress tests pass.

### Expected net production delta

Remove 300 to 700 lines.

## Phase 3: Centralize Ordering-Key Derivation

### Changes

- Make lane, coalescing, busy-follow-up, lifecycle-lock, and queued-notice APIs accept their existing or newly named key types.
- Derive each key once at the earliest authoritative evidence boundary.
- Remove duplicate `CoalescingKey` and room/thread tuple construction from `turn_controller.py` and response lifecycle code.
- Preserve the gate-to-lane readiness query because it is a current flush invariant.

### Tests

- Same sender across two rooms uses two receipt lanes.
- Two requesters in one conversation batch independently and serialize delivery through one target.
- Room-level and threaded targets derive the same lifecycle identity used by cancellation and queued notices.
- `ActiveFollowUpCoalescingOwner` cannot equal `RequesterCoalescingOwner`, including when the requester ID contains the old reserved prefix.

### Exit gate

No synthetic requester string represents follow-up ownership.
Plain lane and lifecycle tuples do not cross subsystem module boundaries.

### Expected net production delta

Remove 100 to 300 lines.

## Phase 4: Type Matrix Delivery Failures

### Changes

- Replace the internal `None` collapse in `matrix/client_delivery.py` with typed failure reasons for encryption guards, sync prerequisites, unknown encryption state, send exceptions, and unexpected response types.
- Translate those failures once at `delivery_gateway.py` into the existing final-delivery vocabulary.
- Keep the public Matrix-client surface stable unless a broader caller inventory proves a safe direct conversion.

### Tests

- Every current `None` failure site maps to a distinct internal reason.
- Existing user-visible failure text and retryability remain unchanged.
- Successful encrypted and unencrypted sends retain current event-ID handling.

### Exit gate

No caller infers failure class from `None` at the gateway boundary.

### Expected net production delta

Between 100 lines added and 100 lines removed.

## Phase 5: Consolidate Final Delivery Before Response Settlement

### Changes

- Make the gateway the sole normal constructor of `FinalDeliveryOutcome`.
- Relocate the five current non-gateway constructions in `response_runner.py`: no-terminal-event pre-delivery finalization, missing-outcome settlement after delivery starts, late-cancellation finalization, team blocking cancellation before an event exists, and agent blocking cancellation before an event exists.
- Share one final event-ID precedence function across streaming, blocking, cancellation, placeholder adoption, and pre-delivery terminal paths.
- Preserve `StreamTransportOutcome` as transport evidence and keep durable handledness outside `streaming.py`.
- Centralize canonical provider text, rendered Matrix text, visible-body state, replay text, and empty-terminal reconciliation in the existing delivery types.
- Preserve streaming's single-delivery-owner task and committed-state rollback invariants.
- Pin terminal note text consumed by `stale_stream_cleanup.py`.

### Primary files

- `src/mindroom/delivery_gateway.py`
- `src/mindroom/final_delivery.py`
- `src/mindroom/response_terminal.py`
- `src/mindroom/streaming.py`
- `src/mindroom/matrix/stale_stream_cleanup.py`
- `src/mindroom/response_runner.py`

### Tests

- Streaming and blocking event-ID precedence are equivalent.
- Placeholder-only, substantive, finalized, adopted, failed, and cancelled states remain distinct.
- Replay text remains stable when rendered Matrix text differs from provider output.
- Cancellation before placeholder, during edit, after placeholder, and after final delivery converges correctly.
- Stale-stream recovery recognizes every terminal and interrupted marker.

### Exit gate

Code outside the gateway does not construct ordinary `FinalDeliveryOutcome` values or infer substantive handledness from event-ID truthiness.

### Expected net production delta

Remove 250 to 400 lines.

## Phase 6: Deduplicate Outer Response Settlement

### Changes

- Keep the existing blocking and streaming response-turn drivers.
- Extract duplicated outer helpers for stream-delivery failure, cancellation-note settlement, streamed finalization, timing marks, interrupted persistence, post-response outcome construction, and session-watch setup.
- Keep agent and team materialization, session, memory, knowledge, and model-selection differences in their current envelopes.
- Consolidate repeated adapter construction only where it reduces the current callback surface.
- Do not replace the current adapters with a larger lowest-common-denominator callback bag.

### Primary files

- `src/mindroom/response_runner.py`
- `src/mindroom/response_turn.py`
- `src/mindroom/response_lifecycle.py`
- `src/mindroom/ai.py`
- `src/mindroom/teams.py`
- `tests/test_response_runner_focused.py`

### Tests

- Agent and team outer settlement produce equivalent final outcomes for equivalent execution facts.
- Streaming and blocking modes preserve completed, empty, failed, interrupted, continued, and cancelled semantics.
- Under-lock payload preparation remains exactly once.
- Dynamic continuation and empty-run retry limits remain independent.
- Structural coverage pins `BlockingTurnAdapter` to at most 10 callback fields and `StreamingTurnAdapter` to at most 11 callback fields, unless the phase reduces those reviewed baselines.

### Exit gate

The number of adapter callback fields is below the current 10-field blocking and 11-field streaming baselines, or the phase is limited to helper extraction without increasing either baseline.
`response_runner.py` contains less duplicated settlement logic without absorbing entity-specific domain behavior.

### Expected net production delta

Remove 300 to 550 lines.

## Phase 7: Clarify Durable Settlement Without Rebuilding It

### Changes

- Add a terminology table for the two deferred result enums and the persisted deferred obligation state.
- Document in-process execution claim, pending turn claim, durable pending turn, terminal turn, semantic consumer, and obligation tombstone as distinct concepts.
- Add an operator-visible quarantine diagnostic for corrupt obligation rows while retaining those rows for cleanup safety.
- Preserve the asymmetry between live deferred-row settlement and replay-time unconditional tombstoning.
- Preserve permanent message and reaction tombstones required for receipt ordering.
- Preserve the thread-safe callback from ledger persistence into `TurnSettlementRetry`.
- Extract shared `TurnRecord` construction only after the Phase 0 writer inventory proves real duplication.
- Do not add a universal settlement service.

### Primary files

- `src/mindroom/dispatch_callback_outcome.py`
- `src/mindroom/dispatch_obligations/events.py`
- `src/mindroom/dispatch_obligations/runner.py`
- `src/mindroom/dispatch_obligations/storage.py`
- `src/mindroom/turn_store.py`
- `src/mindroom/turn_settlement_retry.py`
- `src/mindroom/bot.py`
- `docs/architecture/bot-runtime.md`

### Tests

- Ordinary callback retry and turn dedup follow the documented sequence.
- Multi-purpose semantic-consumer claims remain scoped to approval and reaction callbacks.
- A pending row is not compacted by the live terminal-settlement path before callback execution defers it.
- Replay settlement can tombstone from existing terminal truth.
- Corrupt rows are retained, reported as quarantined, and continue protecting cleanup ownership.
- The persist-worker notification remains safe across threads and event-loop shutdown.

### Exit gate

The durable diagram maps one-to-one to current code and tests.
No schema migration or new settlement service is introduced in this phase.

### Expected net production delta

Between 100 lines added and 150 lines removed.

## Phase 8: Remove Adapters and Finish the Architecture Boundary

### Changes

- Delete temporary adapters whose replacement is now authoritative.
- Delete duplicate key helpers, duplicate normalization paths, dead callback plumbing proven dead by call-site analysis, and obsolete delivery constructors.
- Move remaining domain decisions out of composition roots only where the Phase 0 ownership ledger identifies a focused existing or new owner.
- Update `docs/architecture/bot-runtime.md` to match the final code.
- Run a duplication audit limited to the touched pipeline.

### Tracking targets

- `turn_controller.py` should become materially smaller, with 1,800 to 2,000 lines as a planning range and 1,500 only as a stretch metric justified by the ownership ledger.
- `response_runner.py` should lose the outer settlement matrix without moving the same complexity into a callback-heavy adapter.
- The exact Phase 0 production boundary should finish 1,000 to 2,500 lines smaller.
- New focused modules should stay below 1,200 lines unless their single responsibility is documented.

### Exit gate

Focused and full tests pass.
`uv run tach check --dependencies --interfaces` passes.
The import-graph test passes without broadening heavy dependency allowlists merely for the refactor.
`uv run pre-commit run --all-files` passes in a fully synchronized environment.
The architecture document describes the code that exists.

## Single Pull Request Execution Sequence

Implement the work on one feature branch and open one non-draft pull request only after every stage is complete and verified.
Keep the commit order reviewable so each architectural transition can be inspected independently inside the pull request.

1. Add missing characterization tests, the glossary, LOC manifest, writer inventory, and ownership ledger.
2. Add the prepared-ingress shape, lane key, and explicit follow-up owner.
3. Carry single-normalization ingress through coalescing while preserving claim and replay semantics.
4. Centralize ordering-key derivation.
5. Type Matrix delivery failures.
6. Make the gateway own final delivery and event-ID precedence.
7. Extract duplicated outer response-settlement helpers.
8. Add deferred terminology and corrupt-row quarantine.
9. Delete temporary adapters and old paths, shrink composition roots, and finish documentation.

Each stage should be one atomic commit or a small contiguous commit group.
Every commit must pass its focused test matrix, and the branch must pass the complete verification matrix after stages 3, 6, and 9.
Temporary adapters may exist between commits on the feature branch, but none may remain merely for compatibility when the pull request is opened.
Avoid feature flags and parallel old/new execution paths beyond the shortest commit interval required to move a boundary safely.
If a stage exposes a semantic disagreement, stop and amend the plan and characterization tests on the same branch before continuing.

## Verification Matrix

The ingress matrix includes `test_ingress_validation.py`, `test_ingress_lanes.py`, `test_coalescing.py`, `test_live_message_coalescing.py`, `test_turn_controller_focused.py`, `test_turn_controller.py`, `test_bot_media_dispatch.py`, and `test_voice_command_processing.py`.
The response matrix includes every `test_response_runner_*` file, `test_response_turn.py`, and the streaming test family.
The delivery matrix includes gateway, finalization, streaming edit, stale-stream cleanup, and interrupted replay tests.
The durability matrix includes `test_dispatch_obligations.py`, `test_turn_dispatch_pipeline.py`, `test_turn_store.py`, `test_handled_turns.py`, and `test_sync_restart_retry.py`.

Use explicit test paths because the repository has no focused-suite marker.
Use injected clocks, debounce controls, and deterministic fakes rather than adding sleep-based transition tests.
Run the full suite before merging changes to durable recovery, cancellation, or Matrix terminal delivery.

## Risk Controls

- Treat existing behavior and Phase 0 characterization traces as the specification.
- Preserve the media and text claim-before-normalization rule introduced to prevent duplicate dispatch.
- Preserve per-source identity in coalesced and replayed turns.
- Preserve mutable close ownership until a functional replacement is proven.
- Keep delivery transport facts separate from durable handledness.
- Do not add a storage migration or intentional behavior change to this architectural pull request.
- Update `tach.toml` and run Tach checks in every commit group that moves a boundary.
- Review the final pull request commit-by-commit as well as as one combined diff.
- Keep the final combined diff self-contained, with no dependency on a follow-up cleanup pull request.
- Stop after three review rounds if reviewers still find new major bug classes and reconsider the design.
- Reject abstractions that serve only hypothetical transports, entities, or persistence backends.

## Success Criteria

A maintainer should be able to answer these questions from `bot-runtime.md`, the ingress handoff types, the delivery outcomes, and the durable stores.

- Which identity is the physical sender, effective requester, batching owner, and delivery target?
- Which mechanism preserves receipt order, which batches messages, and which serializes visible responses?
- Is an ordinary callback merely pending, executing in-process, downstream-owned, durably pending as a turn, or terminal?
- Is a semantic-consumer claim relevant to this callback family?
- Did the provider complete, did Matrix show a placeholder or substantive response, and what text is replayable?
- What can repeat after a crash, and which durable record prevents a second visible response?
- Where do cancellation, redaction, command journaling, and restart recovery write durable truth?

The refactor is complete only when these answers are represented by existing or evolved types and transition tests, not only by prose.

## Independent Review Outcome

Fable 5 and Opus 5 independently reviewed the checksum-pinned first draft in isolated worktrees.
Both agreed with the ingress diagnosis and rejected the draft's greenfield framing of response and settlement work.
Both found that the existing shared response drivers, final delivery types, terminal `TurnStore` chokepoint, and settlement retry bridge should be preserved.
Both required lower LOC expectations, a corrected file inventory, delivery work before outer response deduplication, and stronger claim, replay, cancellation, and recovery invariants.

The reviewers disagreed on `DispatchSemanticConsumer`.
Direct source verification shows that it selects consumers for approval and reaction callbacks only, so this revised plan follows Fable's narrower interpretation and documents ordinary turn ownership separately.

The resulting design adds no universal lifecycle framework.
Its main structural change is authoritative prepared ingress, followed by consolidation into existing delivery, response, and durable-settlement owners.
