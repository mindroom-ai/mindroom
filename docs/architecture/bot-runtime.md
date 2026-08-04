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

`RedactedTurnCleanup` owns durable source-redaction tombstoning and advisory cache sanitization.
`TurnStore` removes redacted persisted replay before the next response starts in the affected conversation.
`AgentBot` only delegates the Matrix redaction callback to that collaborator.

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
Each exact Matrix principal and entity stores pending replay payloads and permanent compact tombstones in its own `tracking/dispatch_obligations-<entity>-<principal-sha256-prefix>.sqlite3` file.
This file boundary prevents one entity's admission write from waiting on another entity's SQLite write transaction.
Its exact key combines the Matrix principal, entity, source event, and callback kind, while unsettled rows retain the original room and event source for replay.
Unsettled rows distinguish callbacks that still need execution from callbacks that completed and deferred their source to downstream turn work.
Settled rows become permanent exact-key tombstones and atomically scrub that replay payload, keeping terminal truth compact without allowing an old callback to reappear.
Each entity database therefore grows by one compact row per settled callback except successful invites, whose synthetic obligations are deleted so later re-invites can run.
Operators can inspect growth by running `SELECT state, COUNT(*) FROM dispatch_obligations GROUP BY state;` against each dispatch-obligation database.
Terminal rows must not be deleted unless duplicate callback execution after future Matrix redelivery is acceptable.
Classic Sync tokens are opaque and may be invalidated, forcing a no-`since` sync whose limited timeline backfill can redeliver an older event, so there is no checkpoint-relative pruning frontier that preserves exact de-duplication.
Successful and intentionally ignored callbacks settle explicitly, while failures and cancellations remain pending for direct startup recovery.
Callback failures remain autonomously retry-owned with capped exponential backoff until they settle or deterministic corruption parks them for operator repair.
Visible response paths persist `TurnStore` truth, while pure policy ignores, unmentioned managed senders, blocked deep synthetic relays, and commands owned by another entity compact their exact dispatch-obligation tombstones directly.
This keeps ignored high-volume traffic out of the handled-turn JSON ledger without weakening exact callback de-duplication.
An in-memory claim loser waits for the competing owner, then yields to durable terminal truth or retries ingress when that owner exits without a terminal outcome.
Ingress-lane readiness and delivery failures return the exact source to the existing durable retry owner after the lane releases it.
A successful empty readiness result explicitly settles the exact source as intentionally ignored instead of repeating download or transcription work forever.
Router delivery failure raises back into that same retry path instead of completing without terminal truth.
Recovery parses and invokes pending work without depending on a later Classic Sync token or Sliding Sync position.
Recovery callbacks may rely on the room ID, while cached membership and state are best-effort because recovery does not wait for a new sync.
Recovery logs and skips a corrupt pending row so other valid rows can continue, while retaining the corrupt row for repair.
To repair corruption, stop MindRoom, back up the affected database, and restore a known-good copy before restarting.
Deleting an unrecoverable pending row is a last resort that accepts losing that callback unless Matrix redelivers it.
Message and media obligations remain unsettled only while their callback, gate, competing turn claim, retry, or a pending `TurnStore` response owns them, then yield only to an explicit compact outcome.
Recovery intent travels with queued ingress so pre-existing lane and coalescing workers cannot turn a temporarily unavailable recovered router target into a terminal fallback response.
The registered `DispatchObligationRunner` source callback durably accepts each relevant event before background execution.
The pinned nio recovery contract publishes a recovered-room outcome only after every non-live callback succeeds and republishes every open gap as unrecovered on each response.
Raw sync-cache continuity remains owned separately by `SyncCacheTrust`, so a durable pending dispatch obligation is sufficient to preserve a certified checkpoint.
`SyncCacheTrust` certifies a complete recovered response, rewinds every locally incomplete, failed, or nio-unrecovered response to the retained pre-gap checkpoint, and relies on nio's persisted aggregate gap state instead of duplicating it.
A positioned limited room absent from both typed outcome sets has no real nio recovery gap and may certify, including membership-reset windows.
When no generation-safe checkpoint exists, `SyncCacheTrust` lets one locally complete and error-free limited response advance without a token reset so nio can position itself and classify that gap.
Classic receive-loop exit also reconciles nio's live cursor with the last certified checkpoint, covering cancellation after nio applies a response but before its response callback starts.
The pinned mindroom-nio contract supplies durable `LIVE` or `HISTORY` provenance with every timeline-event admission.
The aggregate admission owner durably caches every historical event through the room-ordered sync mutation path before applying the cold-history dispatch fence, so `/messages` recovery cannot complete without its point rows and redaction effects.
`ColdHistoryFence` admits live events immediately and admits historical events only when the exact event and callback kind are already durably pending.
The same event-scoped provenance gates auxiliary room callbacks, so one live event cannot license unrelated historical call-state mutations.
Checkpoint mutations serialize their epoch check, durable transform, and runtime publication, while continuity revisions prevent older completed tasks from overwriting newer join-fence state.
Malformed or future continuity records are durably repaired to an empty cold record before startup room lifecycle restoration.
Continuity reads and writes run off the event loop, and retry decisions use the checkpoint already loaded or applied by `SyncCacheTrust`.
Classic Sync response-owned lifecycle hooks and their durable de-duplication markers complete before `SyncCacheTrust` certifies the response checkpoint.
The tokenless room-member baseline remains pending across rejected response attempts and records membership from both the state block and the timeline, while a restored-token timeline remains a catch-up stream that may emit missed joins.
Live `room-member-joined` hooks are at-least-once because hook emission happens before the durable seen marker, so a marker write failure replays the hook instead of losing it.
Invite and response-owned lifecycle paths use the same runner directly because they are outside nio timeline fanout.
Current invite callbacks bypass cold-history admission because they represent live membership work, while their callback runner still provides exact durable retry.
The matching ordinary nio event callbacks only load and execute already-persisted work after every admission callback succeeds, and may then continue in the background.
Auxiliary call-manager membership and unknown-event callbacks remain best-effort reconciliation wakeups because their standalone event payloads cannot replay the current room call state; the manager reconciles joined rooms after sync and retries transient state fetches directly.
To-device call inputs and desktop pairing receivers also remain best-effort because they do not share a stable replayable timeline-event identity, so failures in these auxiliary paths are logged without dispatch-obligation ownership.

## Completed Simplifications

`TurnController` is now the only normal-turn owner.
It sequences `precheck -> normalize -> resolve -> decide -> execute -> record`.

`TurnPolicy` is now pure.
It no longer sends messages, runs AI, or writes persistence state.

`TurnStore` is now the main durable turn boundary for the extracted runtime flows.
`TurnController` and `EditRegenerator` read and write through `TurnStore` instead of owning their own persistence helpers.
Command handling now records terminal outcomes through `TurnStore` as well.
Potentially mutating chat commands use an at-most-once execution-attempt journal: `TurnStore` records that execution is about to begin before the handler runs, then records the exact result before visible delivery.
Recovery re-delivers a recorded result, while an interrupted execution attempt without a result is not rerun and instead produces an explicit uncertain-outcome response that requires the requester to inspect state before retrying.
Startup loads turn truth without pruning, recovers turn-backed dispatch obligations, then applies age and count cleanup while retaining pending redaction work, replayable incomplete turns, and every group referenced by a raw unsettled callback row.
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
One runtime process owns each ledger's semantic ordering, while the advisory file lock protects exact durable writes without defining cross-process turn precedence.
Unversioned pre-user ledger and run-metadata turn schemas are rejected instead of carrying migration scaffolding.

Matrix source redactions are durably tombstoned before the advisory conversation cache is mutated.
A tombstone becomes a retained cleanup intent once the entity has recorded the affected conversation context, while unrelated redactions remain bounded ledger barriers without storage probes.
Pending normal and interactive responses durably record their exact target and history scope off the event loop before generation, and every source-backed response checks tombstones again under the lifecycle lock.
Before a response starts, `TurnStore` removes the matching run and its causal suffix from every history scope recorded for the conversation, clears summary-backed replay state, preserves compaction run tombstones, and sanitizes coalesced prompt metadata used by later edit regeneration.
Redacted replay may remain in local session storage until that conversation's next response, but no model receives it.
Semantic memory backends such as Mem0 have a separate lifecycle and are not altered by persisted replay cleanup.

## Tool Dispatch Contracts

There are now four active runtime contracts for tool and scheduling dispatch.
`ToolRuntimeContext` is the live Matrix runtime object with client, caches, hook bindings, and attachment scope.
`LiveToolDispatchContext` is the strict live contract that pairs one `ToolRuntimeContext` with a matching `ToolExecutionIdentity`.
`ToolDispatchContext` is the detached contract for cases that only have a serializable execution identity and no live Matrix runtime.
`SchedulingRuntime` is the explicit live scheduling contract consumed by command and tool scheduling entrypoints.
Hook bridges and response execution now consume these contracts directly instead of rebuilding identity from partial nullable fields.

`AgentBot` is closer to a runtime shell again.
It still needs more cleanup, but normal turn control, edit regeneration, and interactive selection execution no longer live in the bot class itself.

Interactive reactions and numeric text selections now share the same controller-owned execution path.
That path sends the acknowledgment, runs response generation, and records the handled turn once.

`ResponseAttemptRunner` now owns visible response attempts.
It sends thinking placeholders, registers stop tracking, runs the cancellable response task, logs cancellation provenance, and clears stop tracking.
`ResponseRunner` keeps the existing attempt entry point, but delegates attempt mechanics through this deeper module.

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
