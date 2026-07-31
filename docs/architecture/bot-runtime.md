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
Each exact Matrix principal and entity stores pending replay payloads and permanent compact tombstones in its own `tracking/dispatch_obligations-<identity-sha256>.sqlite3` file.
This file boundary prevents one entity's admission write from waiting on another entity's SQLite write transaction.
Its exact key combines the Matrix principal, entity, source event, and callback kind, while pending rows retain the original room and event source for replay.
Settled rows become permanent exact-key tombstones and atomically scrub that replay payload, keeping terminal truth compact without allowing an old callback to reappear.
Each entity database therefore grows by one compact row per exact callback over the lifetime of the instance.
Operators can inspect growth by running `SELECT state, COUNT(*) FROM dispatch_obligations GROUP BY state;` against each dispatch-obligation database.
Terminal rows must not be deleted unless duplicate callback execution after future Matrix redelivery is acceptable.
Successful and intentionally ignored callbacks settle explicitly, while failures and cancellations remain pending for direct startup recovery.
Callback failures receive at most five autonomous retries per process, and exhausted work stays pending for a later restart or operator investigation.
Recovery parses and invokes pending work without depending on a later Classic Sync token or Sliding Sync position.
Recovery logs and skips a corrupt pending row so other valid rows can continue, while retaining the corrupt row for repair.
To repair corruption, stop MindRoom, back up the affected database, and restore a known-good copy before restarting.
Deleting an unrecoverable pending row is a last resort that accepts losing that callback unless Matrix redelivers it.
Message and media obligations remain pending when coalescing or a pending `TurnStore` record defers them, then yield only to durably persisted terminal turn truth.
Raw sync-cache continuity remains owned separately by `SyncCacheTrust`, so a durable pending dispatch obligation is sufficient to preserve a certified checkpoint.
Classic Sync response-owned lifecycle hooks and their durable de-duplication markers complete before `SyncCacheTrust` certifies the response checkpoint.
Invite and response-owned lifecycle paths use the same runner directly because they are outside nio timeline fanout.
The matching ordinary nio event callbacks only load and execute already-persisted work after every admission callback succeeds, and may then continue in the background.

## Completed Simplifications

`TurnController` is now the only normal-turn owner.
It sequences `precheck -> normalize -> resolve -> decide -> execute -> record`.

`TurnPolicy` is now pure.
It no longer sends messages, runs AI, or writes persistence state.

`TurnStore` is now the main durable turn boundary for the extracted runtime flows.
`TurnController` and `EditRegenerator` read and write through `TurnStore` instead of owning their own persistence helpers.
Command handling now records terminal outcomes through `TurnStore` as well.

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
