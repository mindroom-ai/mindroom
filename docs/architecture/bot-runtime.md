# Bot Runtime Simplification Roadmap

## Purpose

This document is the source of truth for the next runtime simplification.
The goal is to make the remaining abstractions concrete, honest, and easy to trace.

## Good Boundaries To Keep

`AgentBot` is the Matrix runtime shell.
It should own lifecycle, callback registration, sync, room membership, presence, and startup or shutdown.

`InboundTurnNormalizer` owns raw input shaping.
It should turn text, voice, sidecars, and media into canonical turn inputs before policy or execution runs.

`ConversationResolver` owns conversation identity.
It should resolve explicit thread identity, history, mentions, and normalized ingress envelopes.

`DeliveryGateway` owns Matrix transport.
It should send, edit, redact, and finalize already-generated responses.

`EditRegenerator` owns the edited-message replay workflow.
It is still coupled to the current persistence split, but its workflow boundary is real.

`TurnStore` owns source-redaction tombstoning, and removes redacted persisted replay before the next response starts in the affected conversation.
The projection learns about a redaction through journal admission, so the Matrix redaction callback owes only that tombstone.

## Current Problems

`TurnController` is the real turn owner now, but it is still too large.
`TurnPolicy` is pure now, but `ResponseRunner` still owns too much execution detail.
`IngressHookRunner` is a thin hook adapter with a vague name.
`TurnStore` gives the runtime one durable turn boundary, but it still has to reconcile ledger state with persisted run metadata under the hood.
`MessageTarget` still combines conversation identity and delivery placement.

## Target Runtime Vocabulary

The target runtime should read like this:

```text
Matrix callback
  -> AgentBot
  -> TurnController
       -> InboundTurnNormalizer
       -> ConversationResolver
       -> TurnPolicy
       -> ResponseRunner
       -> TurnStore
       -> DeliveryGateway
```

`AgentBot` owns Matrix lifecycle only.
`TurnController` owns one inbound turn from ingress to recorded outcome.
`TurnPolicy` owns pure decision logic only.
`ResponseRunner` owns response execution and lifecycle only.
`TurnStore` owns durable turn truth.
`DeliveryGateway` owns Matrix transport only.

## Durable Dispatch Boundary

Nio pre-fanout admission callbacks persist each correctness-critical Matrix timeline callback before any ordinary event callback can run.
Every principal shares one durable store at `tracking/event_journal.db`, or one PostgreSQL database, and each bot reads only its own principal-bound view of it.
Writes are serialized per store rather than per entity, so one principal's admission waits behind another's write transaction; the reader pool is separate, so reads do not.
A row is keyed `(principal_id, event_id)`, so one Matrix event is one row no matter how many features could have claimed it, and the callback kind is a column on that row rather than part of its identity.
Pending rows retain the original room and event source in `source_json` for replay.
`state` holds one of exactly two values, `pending` and `settled`, so a callback that has never run and a callback that ran and deferred its source to a turn are the same durable fact.
Telling those two apart is possible only in memory, from the parsed events the dispatcher is still holding and the live owners it can ask about, and a restart erases that -- which is why an interrupted turn replays its message instead of losing the answer.
Settling clears `source_json` and any claimed semantic consumer in place rather than deleting the row, so terminal truth stays compact while the row goes on proving that this event already produced its one turn.
Why the work ended, answered or deliberately not answered, is not recorded: that column existed for a while and was never read back.
`journal_events` grows by one row per admitted event, and a settled row is retained rather than deleted so a replayed Matrix event is recognised by ID instead of admitted twice.
Operators can inspect growth by running `SELECT state, COUNT(*) FROM journal_events GROUP BY state;` against that store.
Terminal rows must not be deleted unless duplicate callback execution after future Matrix redelivery is acceptable.
Classic Sync tokens are opaque and may be invalidated, forcing a no-`since` sync whose limited timeline backfill can redeliver an older event, so there is no checkpoint-relative pruning frontier that preserves exact de-duplication.
Successful and intentionally ignored callbacks settle explicitly, while failures and cancellations remain pending for direct startup recovery.
Callback failures remain autonomously retry-owned with capped exponential backoff until they settle or deterministic corruption parks them for operator repair.
Visible response paths persist `TurnStore` truth, while pure policy ignores, unmentioned managed senders, blocked deep synthetic relays, and commands owned by another entity settle their journal events directly instead of recording a turn.
This keeps ignored high-volume traffic out of the handled-turn ledger without weakening exact callback de-duplication.
An in-memory claim loser waits for the competing owner, then yields to durable terminal truth or retries ingress when that owner exits without a terminal outcome.
Ingress-lane readiness and delivery failures return the exact source to the existing durable retry owner after the lane releases it.
A successful empty readiness result explicitly settles the exact source as intentionally ignored instead of repeating download or transcription work forever.
Router delivery failure raises back into that same retry path instead of completing without terminal truth.
Recovery parses and invokes pending work without depending on a later Classic Sync token or Sliding Sync position.
Recovery callbacks may rely on the room ID, while cached membership and state are best-effort because recovery does not wait for a new sync.
Recovery logs and skips a corrupt pending row so other valid rows can continue, while retaining the corrupt row for repair.
To repair corruption, stop MindRoom, back up the affected database, and restore a known-good copy before restarting.
Deleting an unrecoverable pending row is a last resort that accepts losing that callback unless Matrix redelivers it.
Message and media obligations remain unsettled only while their callback, gate, competing turn claim, retry, or a pending `TurnStore` response owns them, then yield only to an explicit settlement.
Recovery intent travels with queued ingress so pre-existing lane and coalescing workers cannot turn a temporarily unavailable recovered router target into a terminal fallback response.
The sync callback admits each relevant event to the journal, in the same transaction that updates the projection, before any background execution.
The pinned nio recovery contract publishes a recovered-room outcome only after every non-live callback succeeds and republishes every open gap as unrecovered on each response.
Sync continuity is owned separately by `SyncCheckpointTrust`, so a pending journal event is sufficient to preserve a certified checkpoint.
Classic clients disable nio token and recovery persistence, so the store-generation-validated MindRoom checkpoint is the only durable Classic cursor.
nio parses one Classic response and stages its room, recovery, and completion state in memory.
MindRoom advances the checkpoint only after journal admission and response-owned lifecycle effects complete, and after any skipped gap has been recorded as a durable room history-recovery obligation.
The same cancellation-drained publication step then acknowledges the exact staged token in nio, so nio's volatile dirty bit can only force replay and never authorizes checkpoint progress.
nio exposes that acknowledgeable token only after all internal response processing succeeds with no retained recovery callback or failure, so failed or still-running staging stays dirty even if its mutable cursor is old or partially advanced.
An ordinary same-token response that nio suppresses as a clean no-op has no dirty state, so MindRoom publishes continuity without calling the acknowledgement API.
Any failed, cancelled, or nio-unrecovered response discards nio's transient world and replays from the retained MindRoom checkpoint with full state.
The nio reset waits for non-sync membership cleanup plus active and queued room-state operations before clearing that world, and its one-shot rebuild marker applies the first full-state response even when Matrix returns the same opaque token.
When a reset ends nio's current Classic receive loop, `AgentBot` re-enters the first rebuild immediately in-process without supervisor failure classification.
Consecutive rejected rebuilds use capped exponential backoff so a persistent recovery failure cannot create a tight full-state sync loop.
Transient sync errors retain the initial cursor, filter, and full-state request until a successful response completes the rebuild.
Classic startup clears legacy nio cursor, recovery, and Sliding window rows so a previous transport mode cannot later resurrect them.
Sliding Sync retains its own persisted recovery lane but does not become a Classic cursor authority.
Already-admitted events remain recoverable from the journal, and Matrix replay is idempotent because admission is keyed by event ID.
`SyncCheckpointTrust` certifies only locally complete responses and requests a transient nio reset for every rejected Classic response.
Rewinding cannot shrink a gap measured from a fixed checkpoint to an advancing live position, so a room that stays unrecovered across repeated attempts from one unchanging checkpoint would otherwise never converge.
`SyncRecoveryStallTracker` counts those failures per room against the checkpoint they were measured from, and a checkpoint that advances between attempts is forward progress that restarts the count.
After three failures from one unchanging checkpoint, that room's gap is skipped: the response certifies its own `next_batch` and logs `matrix_sync_recovery_gap_skipped_after_stalled_rebuild` with the room and the token range it moved past.
Three failures is a policy threshold rather than proof that recovery is impossible, so skipping does not accept the loss; it defers it.
Before the skipping checkpoint is persisted, and inside the same lock, a `room_history_recovery` row records the only fact Classic sync can prove: this room has an unknown missing interval.
The obligation exists even when the projection is empty, and recording it retracts completeness for every room and thread marker.
A repairable room reads as unhydrated for every conversation in it, so the next read walks `/messages` past the prompt window until readable server exhaustion or a configured cost ceiling.
Only readable server exhaustion clears the obligation; malformed or unreadable events fail the read and leave it repairable, while a cost ceiling retains a truncated obligation and bounded context without claiming completeness.
Every later signal resets the obligation to repairable and increments its revision, and settlement compares that exact revision so an older walk cannot clear a newer gap.
A departure drops the old membership's obligation, a signal received while departure remains fenced is ignored, and a late unknown signal after a confirmed rejoin may conservatively over-repair the new membership.
A skipping checkpoint also resets the client, because nio may still hold recovery state for the room the checkpoint moved past and would refuse to acknowledge the response in place.
A positioned limited room absent from both typed outcome sets has no real nio recovery gap and may certify, including membership-reset windows.
A complete tokenless initial snapshot may establish the first MindRoom checkpoint even when its timeline is limited.
An event that never crossed MindRoom admission and later falls outside Matrix replay is the explicit pre-admission loss boundary.
Classic receive-loop exit resets only when nio reports unacknowledged staged state, source admission failed, or the live cursor differs from MindRoom's checkpoint.
An acknowledged clean transport restart retains nio's room cache, so in-flight encrypted delivery is not interrupted by an unnecessary rebuild.
Every outbound send uses nio's bounded transport-recovery retry, including notices, hooks, tool output, and media events that begin while the Classic room cache is rebuilding.
The resolved encryption state is frozen before large-message or media upload, so a concurrent cache reset cannot downgrade sidecar or attachment encryption.
Application first-sync readiness remains separate from Classic transport rebuild state, so a reset requests full state without repeating the once-only `bot:ready` lifecycle.
The pinned mindroom-nio contract supplies durable `LIVE` or `HISTORY` provenance with every timeline-event admission.
Admission projects every historical event into `visible_messages` before applying the cold-history fence, so `/messages` recovery cannot complete without its projected rows and redaction effects. Events carrying nio's `HISTORY` provenance are admitted `CONTEXT_ONLY`, so cold history is readable and starts no turn.
`matrix/journal_ingress.py` classifies by nio provenance rather than by a separate fence: `HISTORY` is admitted `CONTEXT_ONLY`, so cold history is readable and owes no turn, while live and recovered events are admitted `ACTIONABLE`.
The same event-scoped provenance gates auxiliary room callbacks, so one live event cannot license unrelated historical call-state mutations.
Checkpoint mutations serialize their epoch check, durable transform, and runtime publication, while continuity revisions prevent older completed tasks from overwriting newer join-fence state.
Malformed or future continuity records are durably repaired to an empty cold record before startup room lifecycle restoration.
Continuity reads and writes run off the event loop, and retry decisions use the checkpoint already loaded or applied by `SyncCheckpointTrust`.
Classic Sync response-owned lifecycle hooks and their durable de-duplication markers complete before `SyncCheckpointTrust` certifies the response checkpoint.
The tokenless room-member baseline remains pending across rejected response attempts and records membership from both the state block and the timeline, while a restored-token timeline remains a catch-up stream that may emit missed joins.
After a live reset from a certified checkpoint, unseen state-block joins also enter the exact durable dispatch path so a join omitted from the replay timeline is not lost.
Live `room-member-joined` hooks are at-least-once because hook emission happens before the durable seen marker, so a marker write failure replays the hook instead of losing it.
Response-owned lifecycle paths run outside nio's timeline fanout, so they admit their own events through `admit_and_run` and get the same durable dispatch, retry, and de-duplication a timeline event gets.
Invites take neither path and are not journalled at all: an invite carries no Matrix event ID to key a durable row on, and it does not need one, because an invite the bot has not acted on reappears in every sync response until it does.
The homeserver is therefore already providing the redelivery a journal row would have, so invite handling is a plain background task.
The matching ordinary nio event callbacks only load and execute already-persisted work after every admission callback succeeds, and may then continue in the background.
Auxiliary call-manager membership and unknown-event callbacks remain best-effort reconciliation wakeups because their standalone event payloads cannot replay the current room call state; the manager reconciles joined rooms after sync and retries transient state fetches directly.
To-device call inputs and desktop pairing receivers also remain best-effort because they do not share a stable replayable timeline-event identity, so failures in these auxiliary paths are logged without journal ownership.

## Completed Simplifications

`TurnController` is now the only normal-turn owner.
It sequences `precheck -> normalize -> resolve -> coalesce -> decide -> execute -> record`.

`TurnPolicy` is now pure.
It no longer sends messages, runs AI, or writes persistence state.

`TurnStore` is now the main durable turn boundary for the extracted runtime flows.
`TurnController` and `EditRegenerator` read and write through `TurnStore` instead of owning their own persistence helpers.
Command handling now records terminal outcomes through `TurnStore` as well.
Potentially mutating chat commands use an at-most-once execution-attempt journal: `TurnStore` records that execution is about to begin before the handler runs, then records the exact result before visible delivery.
Recovery re-delivers a recorded result, while an interrupted execution attempt without a result is not rerun and instead produces an explicit uncertain-outcome response that requires the requester to inspect state before retrying.
Startup loads turn truth without pruning, repairs ledger records for answers the outbox proves were delivered, replays turn-backed journal events, then applies age and count cleanup while retaining pending redaction work, replayable incomplete turns, and every group referenced by an unsettled journal row.
Recovery and its post-recovery ledger cleanup run under one background retry owner, independently of bot startup and Matrix sync lifecycle progress.
Multi-purpose callbacks durably claim one application consumer before that consumer's side effects, and recovery routes only to the claimed consumer instead of rediscovering intent from mutable runtime state.
Consumer-owned side effects remain responsible for their own replay semantics; for example, generic reaction hooks are at-least-once.
`!config set` uses a separate Matrix-backed `preview -> decision -> execution -> result` journal because its mutation begins only after the requester reacts to the preview.

`TurnRecord` is the single immutable schema for turn identity, outcome, and regeneration facts.
One codec projects that schema into the versioned handled-turn ledger and recoverable Agno run metadata.
Interactive-selection discovery aliases remain separate from canonical source identity, so recovery can index every triggering event without making one message look coalesced.
Coalesced router relays persist each human discovery alias on its physical source metadata so later edits and redactions update the owned prompt.
Per-source Matrix revision tuples keep durable edit facts newest-wins across retries and restarts.
`EditRegenerator` groups edits by room, response anchor, and requester in a bounded per-response mailbox.
One draining owner folds each source's newest Matrix revision into a complete response request and loops when newer edits arrive.
Physical source IDs are exclusive turn claims, while discovery aliases are advisory settlement keys observed by `wait_for_turn_settled`.
A committed service-restart or generic terminal interruption note records its exact source room in `InterruptedTurnRooms`.
Replacement recovery uses the registered room directly, while next-startup cleanup can rediscover the durable note and an interrupted edit revision remains uncommitted for re-drive.
The two physical stores remain intentionally redundant so run metadata can repair a ledger write lost during a crash.
`TurnStore` applies deterministic field precedence: a present ledger record owns canonical source identity and anchor, while a newer delivered run can repair mutable response and regeneration facts after a crash.
Recovery never replaces a ledger record that changed while run metadata was loading.
Older or incomplete run metadata only backfills absent optional facts, and conflicting discovery aliases are pruned instead of claiming another completed turn.
Run metadata supplies a complete record when the ledger row is absent and otherwise participates only through that precedence rule.
`TurnStore` immediately writes a recovered or enriched record back to the ledger, so callers never own backfill or repair decisions.
One runtime process owns each ledger's semantic ordering, and nothing defines cross-process turn precedence.
Terminal records live in the journal database rather than a per-agent JSON file, so the advisory file lock that used to make the file update atomic is gone; the database serializes the write itself.
Neither substrate ever merged two processes' views of one record, so one process must own one agent's records — an unenforced contract, and a second runtime against the same storage path will still start.
Unversioned pre-user ledger and run-metadata turn schemas are rejected instead of carrying migration scaffolding.

Matrix source redactions are durably tombstoned in the same transaction that withholds the redacted body, and every projection install path consults that tombstone table.
A tombstone becomes a retained cleanup intent once the entity has recorded the affected conversation context, while unrelated redactions remain bounded ledger barriers without storage probes.
Pending normal and interactive responses durably record their exact target and history scope off the event loop before generation, and every source-backed response checks tombstones again under the lifecycle lock.
Before a response starts, `TurnStore` removes the matching run and its causal suffix from every history scope recorded for the conversation, clears summary-backed replay state, preserves compaction run tombstones, and sanitizes coalesced prompt metadata used by later edit regeneration.
Redacted replay may remain in local session storage until that conversation's next response, but no model receives it.
Semantic memory backends such as Mem0 have a separate lifecycle and are not altered by persisted replay cleanup.

## Tool Dispatch Contracts

There are now four active runtime contracts for tool and scheduling dispatch.
`ToolRuntimeContext` is the live Matrix runtime object with client, conversation reader, relation lookup, hook bindings, and attachment scope.
`LiveToolDispatchContext` is the strict live contract that pairs one `ToolRuntimeContext` with a matching `ToolExecutionIdentity`.
`ToolDispatchContext` is the detached contract for cases that only have a serializable execution identity and no live Matrix runtime.
`SchedulingRuntime` is the explicit live scheduling contract consumed by command and tool scheduling entrypoints.
Hook bridges and response execution now consume these contracts directly instead of rebuilding identity from partial nullable fields.

`AgentBot` is closer to a runtime shell again.
It still needs more cleanup, but normal turn control, edit regeneration, and interactive selection execution no longer live in the bot class itself.

Interactive reactions and numeric text selections now share the same controller-owned execution path.
That path sends the acknowledgment, runs response generation, and records the handled turn once.

`ResponseAttemptRunner` now owns visible response attempts.
It registers stop tracking, runs the cancellable response task, logs cancellation provenance, and clears stop tracking.
`ResponseRunner` keeps the existing attempt entry point, but delegates attempt mechanics through this deeper module.

It deliberately does not send the turn's placeholder.
The durable `INITIAL` outbox row that `ResponseRunner` writes when it takes the lifecycle lock is the only thing allowed to put a placeholder in the room, so a send whose outcome Matrix never confirmed is resolved by resending that row under its own transaction ID rather than by a second, unowned send.
When the durable placeholder cannot be delivered, the turn runs without one and the `FINAL` row becomes its first visible message.

The ingress-to-execution seam is now one-way.
Ingress (`TurnController` and `text_ingress_dispatch`) builds an immutable `ResponsePayloadPreparation` value and hands it to the runner inside `ResponseRequest`.
The runner acquires the lifecycle lock, refreshes thread history, then calls `ResponsePayloadPreparer.prepare` as a first-class execution step to assemble the final payload, run enrichment hooks, and log startup latency.
The old `prepare_after_lock` callback that ran payload building back inside `TurnController` is deleted; data crosses the seam as values, not closures.

## Next Simplification Work

Shrink `ResponseRunner` further.
It keeps locking, streaming, AI or team execution, and post-response effects.
The under-lock payload-assembly side path now lives in `ResponsePayloadPreparer`; the remaining follow-up is to fold `execution_preparation.py` into the execution side and move any other side paths that belong to ingress or delivery out of `ResponseRunner`.

Revisit `IngressHookRunner`.
It may stay as a helper, but it should not grow into another top-level orchestration object.

Only after those steps should we revisit `MessageTarget`.
That follow-up can split conversation identity from delivery placement if the runtime still needs it.

## Review Questions

When reviewing either PR, ask these questions.

Does each abstraction own a concrete thing rather than a vague place in the pipeline.
Did the change delete an old owner instead of adding a second one.
Can one inbound turn be traced without jumping between multiple coordinators.
Is the durable turn truth singular.
