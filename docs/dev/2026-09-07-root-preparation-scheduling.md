# Independent root preparation

This record covers the September 2026 decision to let independent new conversations in a threaded room prepare concurrently, up to eight preparations at a time per bot; the bound covers preparation, not the number of running model replies.
It also keeps the 200-conversation capacity controls and the startup-throughput investigation that measured what the change did and did not fix.

## Why

The serial coalescing gate waited for one new root's context preparation, planning, durable handoff, and response-lifecycle lock before preparing the next new root from the same requester.
An unrelated slow root therefore blocked every following root.
The recorded 200-conversation controls spread first visible replies over about 20 seconds.
Concurrent preparation was a measured candidate for reducing that delay, not a promised latency bound.

## Decisions

- Only completed new root batches in a threaded room are independent conversations, so only they prepare concurrently.
- Explicit threads, active follow-up queues, and rooms configured as one conversation keep serial dispatch, because their batches are not independent.
- The bound stays at eight: a sixteen-slot trial did not improve the workload and nearly doubled summed preparation time (see [Durable batch controls](#durable-batch-controls)).
- The change is kept for independently blocked roots and bounded ownership even though it produced no large application speedup.

## Rejected alternatives

- Keep serial preparation: smallest change, but an unrelated slow root keeps blocking following roots.
- Move all preparation into the response runner: reuses its task registry, but hands off work before the runner's durable adoption point and needs new metadata and retry ownership across components.
- Keep preparation in the coalescing gate: selected, because its claimed-batch cleanup and dispatch-failure callback already own these responsibilities.
- Key each root by its root event ID instead of the room-level key (room, no thread, requester): rejected, because that also changes context lookup, reply targeting, and room-level media promotion.

## Invariants

- A detached preparation owns its claimed source events until dispatch hands them to the response lifecycle, and source-ownership checks must include it so the durable journal worker cannot redispatch a still-owned source.
  For the same reason, no await may separate registering the task from transferring source ownership.
- A slot is awaited before claiming events, and the eligible front batch is recomputed after that wait, because ingress or media promotion may have changed it.
- A drain is complete only when no owned preparation remains, not only those in its first snapshot, because a queue task can start new root preparations after that snapshot.
- Cancelling a queue's wait for a slot does not cancel independent preparations; shutdown owns them.
  A bounded drain reports incomplete work instead of certifying a clean checkpoint.

## Limitations

- Bypass commands handled in the gate wait for older detached preparations with the same coalescing key (same room and requester), but commands routed outside the gate are not ordered against them.
- Bounded shutdown cancels preparations cooperatively and returns at its budget without waiting for callbacks that resist cancellation.
- Concurrent preparation alone did not shorten startup for the 200-root workload; its critical serialization point was the handled-turn ledger lock (see [Resolution](#resolution)).
- None of these controls qualifies 1,000 concurrent replies.

## Evidence

The 200-conversation controls use three syncing principals, and their reply times include about 60 seconds of synthetic generation, so they are not LLM latency predictions.
Every control keeps the same acceptance predicates: 200 exact replies, a 180-second shared reply deadline, a 45-second minimum full visible overlap, a two-second health timeout, the post-terminal fence, drain, and clean shutdown.
Installed source hashes and loaded module paths are checked against the named commits before and after each control.
When testing against a locally built Nio wheel, disable automatic dependency resync for the suite and its subprocesses: in one run a test subprocess restored the old Git pin mid-suite.

### Controlled scheduler benchmark

Three alternating local runs of 40 roots with controlled 10-millisecond asynchronous preparation measured median completion of 409.05 milliseconds with one slot and 56.39 milliseconds with eight.
Preparation start spread fell from 396.48 to 43.38 milliseconds, and peak active preparation was exactly one and eight respectively.
The probe also preserved configured room-mode ordering, explicit-thread ordering, and same-key bypass barriers.
This measures scheduler overlap under controlled asynchronous work, not application reply latency.

### Serial versus eight preparations

One sequential frozen-source Tuwunel pair used Nio `09d5a25` and MindRoom `b45966053`, with the candidate differing only in `coalescing/gate.py`.

| Measure | Serial | Eight preparations |
| --- | ---: | ---: |
| Exact completed replies | 200 | 200 |
| Preparation start spread | 21.076 s | 19.845 s |
| Initial visible reply spread | 21.147 s | 20.571 s |
| Input-to-completion median | 74.579 s | 74.677 s |
| Input-to-completion p95 | 86.211 s | 85.139 s |
| First input to last completion | 86.842 s | 85.708 s |
| Full visible overlap | 42.491 s | 43.247 s |

Both runs settled the post-terminal reaction for all three principals, retained no journal or outbox debt, kept syncing, and shut down cleanly.
Both failed only the 45-second overlap requirement.
This single pair shows no large application speedup: the median was unchanged and initial visibility spread improved by 0.576 seconds.

An earlier serial-dispatch diagnostic attributed 20.337 of 21.314 seconds to `record_pending_turn`: 8.297 seconds awaiting the per-agent ledger lock and 11.836 seconds awaiting the database call.
Only 0.607 seconds occurred inside its database transactions; 10.259 seconds preceded execution and 0.969 seconds followed it before the caller resumed.
These overlapping-stage timings explain why more preparations do not remove the shared persistence bottleneck.
That diagnostic used Nio `5e9c79d`, sampled the process at 25 Hz, and is not comparable with uninstrumented latency results.

### Integrated capacity controls

Nio `398dae4` and MindRoom `55c37f9f3`, with unchanged production source.
All three runs completed 200 exact replies with no application errors, event-loop stalls, or degraded reads.

| Control | Reply median | Reply p95 | Full overlap | Fence principals settled | Application pending / outbox |
| --- | ---: | ---: | ---: | ---: | ---: |
| Tuwunel | 74.607 s | 83.888 s | 44.250 s | 3/3 | 0 / 0 |
| Synapse 5,000-event cap | 80.235 s | 86.141 s | 47.714 s | 2/3 | 1 / 0 |
| Same-source Synapse repeat | 80.058 s | 87.582 s | 48.262 s | 1/3 | 0 / 0 |

Tuwunel passed the fence, drain, later-sync, and clean-shutdown checks and failed only the overlap requirement.
Both Synapse runs failed fence qualification.
The repeat's traceback shows the shared 180-second deadline expiring during fence observation, not the two-second health timeout.
The responder kept admitting older streaming edits through the deadline and cleanup, which is catch-up capacity exhaustion with no evidence of a frozen owner.
The first failed Synapse run carried 7.7% more observer journal traffic than an earlier passing control, so these runs do not establish a per-event processing regression.
Zero application queue counts after cleanup are not a Nio drain certificate: the repeat still retained Nio's authenticated Frames and Work.

### Durable batch controls

These controls followed the replacement of Nio's Frames/Work engine with a coordinated durable batch adapter; the scheduler still used eight slots.

A diagnostic sixteen-slot trial at Nio `34699bf` and MindRoom `72732c22b` changed Tuwunel's median from 71.754 to 71.648 seconds and p95 from 81.323 to 81.196 seconds.
Full overlap stayed below 45 seconds (41.207 versus 42.406), and summed overlapping preparation durations grew from 159.4 to 296.7 seconds.
This justifies neither a larger cap nor another writer.

A FULL build at Nio `ed316ed` completed only 73 of 200 Tuwunel roots before the deadline despite zero remaining queue debt.
An exclusive pagination end prevented proof of retained-tail overlap; Nio `aac2e32` corrects that producer pagination without relaxing scheduling or durability, and Nio's own design keeps the failed control.

Final runtime: Nio `aac2e32` and MindRoom `ed4a209d6`, with FULL durability.

| Control | Reply median | Reply p95 | Full overlap | Acceptance |
| --- | ---: | ---: | ---: | --- |
| Tuwunel | 73.927 s | 86.501 s | 37.567 s | Overlap below 45 s |
| Tuwunel repeat | 74.830 s | 86.533 s | 37.470 s | Overlap below 45 s |
| Synapse 5,000-event cap | 76.987 s | 82.719 s | 47.352 s | PASS |

Every run completed exactly 200 replies and all three post-terminal fence principals, with no remaining input or batch rows, journal work, or delivery-outbox debt after clean shutdown, and no stalls, degraded reads, or incomplete-drain warnings.
Synapse passed every predicate; both Tuwunel runs failed only the overlap requirement.
Against the integrated controls, Tuwunel's median was roughly unchanged and its p95 about 2.6 seconds slower, while Synapse improved from roughly 80.1/87.6 to 77.0/82.7 seconds and its fence and retained-input failures were absent.
In the final Tuwunel runs, preparation starts spanned 23.5 to 23.9 seconds.
These few runs are workload evidence, not a statistical guarantee of speedup, and the much lower engine cost did not remove the application's shared startup persistence cost.
Detailed results are `controls-20260906T093405Z` and `controls-20260906T094006Z` in the retained capacity workspace.

### Startup throughput investigation

A follow-up at MindRoom `556cacacb` and Nio `04c7f72` compared four small prototypes outside production.
The fresh uninstrumented Tuwunel baseline completed all 200 replies with a 73.392-second median and 84.028-second p95.
The baseline and every prototype run settled the fence for all three principals and drained producer, journal, and delivery work.

| Experiment | Initial visibility spread | Full overlap |
| --- | ---: | ---: |
| Unchanged baseline | 22.539 s | 39.233 s |
| Batch worker handoffs, retain separate FULL transactions | 21.788 s | 40.156 s |
| Group queued writes into a FULL commit | 22.820 s | 39.248 s |
| Repeat grouped commits | 22.520 s | 39.606 s |
| Exclude already-owned deferrals from ordinary pending payload reads | 22.294 s | 39.499 s |
| Combine producer capture and initial preparation commits | 21.931 s | 40.064 s |

None of the prototypes showed a startup improvement worth its extra production behavior, so no writer batching, transaction grouping, pending-query exclusion, or scheduler expansion was adopted.
The producer trial would also need consolidated failure ownership before shipping: nesting the existing transactions tries to close a poisoned store before the outer transaction unwinds, and its single-run 0.608-second gain does not justify changing that boundary.
Reconsider these candidates only with new measurements showing a worthwhile gain and tests covering their changed failure behavior.
The group-repeat operation histogram confirms that writes actually grouped, but its interrupted final child hash report limits that run's source proof to the parent before/after hashes.

Findings from the timing traces:

- Each root makes two pending-turn writes, durable intent before generation and binding of the first visible response event; they record distinct facts and are not redundant.
- Ledger waits overlap across concurrent preparations, so their sum is neither end-to-end latency nor time spent inside SQLite.
- During one diagnostic startup interval, the 392 turn-record transactions used about 1.09 seconds of worker wall time, while other admission and settlement work also occupied the shared writer.
- The earlier shared-persistence attribution is therefore a waiting location, not proof that disk or the writer design causes the remaining limit.
- A separate 100 Hz sampled diagnostic put 8.66 seconds of main-thread sample weight in Nio SQLite commits across the run, spread across capture, preparation, acknowledgement, and sync completion.
  It measures sampled execution and blocking locations, not CPU-only time or an exact critical path, and its 28.576-second initial visibility spread is perturbed by instrumentation.
  It motivated the producer transaction trial; neither result points to the Matrix server as the cause.

The retained `startup-throughput` capacity evidence holds the diagnostic wrappers, exact-cohort analysis, and rejected trials.

Decisions from this investigation:

- Keep the producer/application ownership split and FULL durability.
- Any next optimization needs a measured critical path and an unchanged-workload comparison before it becomes production code.
- Do not add a second writer, persistent queue, recovery protocol, serializer dependency, or weaker durability merely to pursue the scale goal.

### Resolution

A later exact-startup trace identified the agent-wide handled-turn ledger lock, held across unrelated admission and settlement work in the shared writer queue, as the critical serialization point; Nio commit stalls overlap those waits and do not add to them.
The [ledger ownership design](2026-09-07-ledger-write-ownership.md) records that trace, the fix in MindRoom `5502b1177`, and its controls, which pass every acceptance predicate on Tuwunel and Synapse with eight preparation slots unchanged.
That result does not qualify 1,000 concurrent replies or justify another scheduler, writer, or durability change.

## Scale direction

The long-term capacity goal is more than 1,000 concurrent replies; this is a direction, not a guarantee the 200-root controls establish.
Measure burst startup, sustained streaming, recovery catch-up, memory, and health responsiveness separately.
Qualify capacity by increasing the same workload through 200, 500, 1,000, and beyond with explicit hardware and model timing, rather than extrapolating a local database benchmark.
Keep the exact-reply, durable-admission, recovery, ordering, cancellation, and shutdown checks at each size.
