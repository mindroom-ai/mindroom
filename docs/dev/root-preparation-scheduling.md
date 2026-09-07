# Independent root preparation

## Decision and ownership

Completed new conversation batches in a threaded room may prepare concurrently, with at most eight such preparations per `CoalescingGate` instance.
The gate still owns receipt ordering, asynchronous readiness, attachment/caption grouping, debounce, and the exact claimed source events until dispatch hands them to the existing response lifecycle.
Only the preparation of independent, completed root batches changes; the original `CoalescingKey(room, None, requester)` remains the semantic context and reply-target input.
Explicit threads, active follow-up queues, and rooms configured as one conversation retain their existing serial dispatch.
The eight-slot bound covers tasks from registration through dispatch completion, including time waiting for durable handoff and the response lifecycle lock; it is not a limit on running model responses.

The existing gate waits for context preparation, planning, durable handoff, and lifecycle acquisition before preparing the next new root from the same requester.
The recorded 200-conversation controls spread first visible replies over approximately 20 seconds.
Concurrent preparation is a measured candidate for reducing that delay, not a promised latency bound.

## Alternatives

Keep serial preparation: smallest implementation, but an unrelated slow root continues to block following roots.
Move all preparation into the response runner: reuses its task registry, but transfers work before its existing durable adoption point and requires new metadata and retry ownership across components.
Retain preparation in the coalescing gate: selected because its claimed-batch cleanup and dispatch-failure callback already own these responsibilities.
Changing the semantic thread key to a root event ID is rejected because that also changes context lookup, reply targeting, and room-level media promotion.

## Lifecycle

Each detached preparation uses an existing `_GateEntry` holding its exact claimed admissions and a tracked task; queued events stay on the original gate.
Capacity is awaited before claiming events or creating another task, and the eligible front batch is recomputed after that await because ingress or media promotion may have changed it.
Task registration and source ownership transfer have no intervening await.
Source-ownership checks include detached entries so the durable journal worker cannot redispatch a still-owned source.
The existing `_dispatch_claim` owns dispatch, failure notification, and metadata cleanup.
Task completion removes the tracked entry and retrieves unexpected failures; cancellation before the task's first step must also release metadata.
An in-gate bypass barrier waits for older detached preparations with the same semantic key; commands routed outside the gate keep their existing behavior.

Graceful drains await both queued gates and detached preparations.
Bounded drains apply the existing shutdown budget and cancellation provenance to both, close metadata once, and report incomplete work instead of certifying a clean checkpoint.
Cancelling the queue's wait for capacity does not cancel independent preparations; shutdown remains their owner.
Detached tasks expose no independent cancellation API; bounded shutdown also delivers cooperative cancellation before returning without waiting indefinitely for cancellation-resistant callbacks.
No new persistent queue, database writer, retry protocol, configuration field, or dependency is introduced.
Full durability, device verification, shared workload deadlines, and the original capacity acceptance predicates remain unchanged.

## Implementation plan

Use the test-driven-development and verification-before-completion skills, with independent focused review before publication.

- [x] Inspect current preparation, source ownership, grouping, and shutdown paths; establish the existing coalescing test baseline.
- [x] Add a failing real-gate test that blocks root A's preparation and observes independent root B starting with its original semantic key and target.
- [x] Add bounded-concurrency, source-ownership, failure/retry, graceful drain, bounded cancellation, and metadata-cleanup regressions in `tests/test_root_preparation_scheduling.py`.
- [x] Update `src/mindroom/coalescing.py` to register bounded detached root claims and include them in existing ownership and drain traversal.
- [x] Preserve explicit-thread and room-mode ordering, media/caption grouping, readiness, and bypass barriers; run the owning-seam and integration tests.
- [x] Compare serial and concurrent preparation with a controlled real-gate benchmark, then run the unchanged real Tuwunel and Synapse 200-conversation controls on verified source. Preserve failed acceptance results.
- [x] Run the full scheduling test suite: 15,850 passed, 22 skipped (16 warnings), including all 12 new scheduling cases.
- [x] Update the Nio artifact pin to verified commit `398dae4d7e7079e3dd5e7a3f360b4f1ec2573c6e`; its installed files match the tested wheel. Complete repository hooks pass, including full-project types and frontend checks.
- [x] Record actual latency, acceptance results, and production line growth here; complete independent review and publish the implementation and evidence on the existing PR.

## Results

Baseline: 53 existing coalescing tests pass before the change.
Both the real-gate and real-controller regression fail against the published baseline because root B cannot begin while root A awaits context preparation.
The new scheduling suite passes 12 cases, including a zero-budget drain with eight active roots and a ninth waiting for capacity.
The broader coalescing and live integration group passes 241 cases before that final added test; all repository hooks pass.
The production change adds 63 net lines in `coalescing.py`.

Two existing cancellation tests now use explicit-thread events and keys because they test cancellation of an active serial conversation queue.
Their former root fixtures assumed independent roots shared that queue; new root-specific tests separately verify the changed ownership and cancellation behavior.
Review found and fixed the drain snapshot boundary: a queue task can create root tasks after the drain's first snapshot, so completion also requires that no owned preparation tasks remain.

Three alternating local runs of 40 roots with controlled 10-millisecond asynchronous preparation measured median completion of 409.05 milliseconds with one slot and 56.39 milliseconds with eight.
Preparation start spread fell from 396.48 to 43.38 milliseconds, and peak active preparation was exactly one and eight respectively.
The probe also preserves configured room-mode ordering, explicit-thread ordering, and same-key bypass barriers.
This measures scheduler overlap under controlled asynchronous work, not application reply latency.
The real 200-conversation comparison uses frozen production source and unchanged health, overlap, deadline, exact-reply, fence, drain, and shutdown predicates.

One sequential frozen-source Tuwunel pair used published Nio `09d5a25` and MindRoom `b45966053`; the candidate differed only in `coalescing.py`.
All source hashes and loaded module paths matched their manifests throughout both runs.

| Measure | Serial | Eight preparations |
| --- | ---: | ---: |
| Exact completed replies | 200 | 200 |
| Preparation start spread | 21.076 s | 19.845 s |
| Initial visible reply spread | 21.147 s | 20.571 s |
| Input-to-completion median | 74.579 s | 74.677 s |
| Input-to-completion p95 | 86.211 s | 85.139 s |
| First input to last completion | 86.842 s | 85.708 s |
| Full visible overlap | 42.491 s | 43.247 s |

Both runs settled the post-terminal reaction for all three principals, retained zero journal/outbox debt, continued sync and shut down cleanly.
Both failed only the unchanged 45-second overlap requirement.
The workload includes approximately 60 seconds of synthetic generation.
This single pair establishes no large application speedup: median reply time was unchanged, and initial visibility spread improved by 0.576 seconds.
Retain the change for independently blocked root preparation and bounded ownership, rather than claiming it resolves the remaining throughput limit.

The previous serial dispatch diagnostic attributed 20.337 of 21.314 seconds to `record_pending_turn`: 8.297 seconds awaiting the per-agent ledger lock and 11.836 seconds awaiting the database call.
Only 0.607 seconds occurred inside its database transactions; 10.259 seconds preceded execution and 0.969 seconds followed it before the caller resumed.
These overlapping-stage timings explain why adding preparations does not remove the shared persistence bottleneck; they are prior diagnostic evidence, not a current-candidate breakdown or disk-only measurement.
The durable handoff and shared writer remain unchanged. A writer redesign or removing pending-turn persistence is outside this scheduling change.
That earlier diagnostic used Nio `5e9c79d` with the then-current companion source; it sampled the process at 25 Hz and must not be compared as an uninstrumented latency result.

The first full-suite run started with all 72 Nio source files matching the built key-share wheel, but a test subprocess restored the old Git dependency pin during that run.
Its passing result covers the scheduling suite but does not certify the final combined artifact.
A repeat with automatic resync disabled for the suite and its subprocesses passed 15,850 tests, with 22 skipped and 16 warnings, in 133.41 seconds.
All 72 installed Nio source files matched the wheel and reviewed source both before and after the suite.
The final Git pin `398dae4d7e7079e3dd5e7a3f360b4f1ec2573c6e` installs those same files; this equality check connects the built-artifact tests to the committed dependency.

## Final integrated capacity results

The final controls used Nio `398dae4` and MindRoom `55c37f9f3`, with unchanged production source, installed-package hashes, 200 roots, 180-second shared deadline, 45-second full-overlap requirement and two-second health read.
All three runs completed 200 exact replies and reported no application errors, event-loop stalls or degraded reads.
Times include approximately 60 seconds of synthetic generation.

| Control | Reply median | Reply p95 | Full overlap | Fence principals settled | Application pending / outbox |
| --- | ---: | ---: | ---: | ---: | ---: |
| Tuwunel | 74.607 s | 83.888 s | 44.250 s | 3/3 | 0 / 0 |
| Synapse 5,000-event cap | 80.235 s | 86.141 s | 47.714 s | 2/3 | 1 / 0 |
| Same-source Synapse repeat | 80.058 s | 87.582 s | 48.262 s | 1/3 | 0 / 0 |

Tuwunel passed the fence, drain, later-sync and clean-shutdown checks; its sole acceptance failure was overlap below 45 seconds.
Both Synapse runs failed fence qualification. The repeat's failure traceback identifies expiration of the shared 180-second deadline during fence observation, rather than the two-second health timeout.
The responder continued admitting older streaming edits through the deadline and cleanup; this is catch-up capacity exhaustion, with no evidence of a frozen owner.
The first failed Synapse run contained 7.7% more observer journal traffic than the earlier passing control, so those runs do not establish a per-event processing regression.

Application queue counts after cleanup are not a Nio drain certificate: the repeat still retained authenticated Frames and Work even though its application journal/outbox counts were zero.
Keep both failures visible. These changes isolate independent preparation and restore interactive key sharing; they do not qualify this single-process workload at the full 200-conversation target.
The shared persistence and catch-up throughput limit remains separate work, with no relaxed deadline, dropped durable events, second writer or codec replacement in this change.

Runtime CI passed: Nio Python 3.12/3.13/3.14 tests, types and hooks; MindRoom's remote suite (15,848 passed, 12 skipped), all four full/minimal AMD64/ARM64 image builds, and the complete smoke stack.


## Durable batch replacement qualification

The coordinated durable batch adapter replaces the earlier Nio Frames/Work
engine. The root preparation scheduler still uses eight slots. A diagnostic
sixteen-slot trial at Nio `34699bf` and MindRoom `72732c22b` changed Tuwunel's
200-request median from 71.754 to 71.648 seconds and p95 from 81.323 to 81.196;
full overlap remained below 45 seconds (41.207 versus 42.406). Summed overlapping
preparation durations grew from 159.4 to 296.7 seconds. This does not justify a
larger cap or another writer; the production scheduler remains unchanged.

Initial NORMAL adapter controls completed all 200 replies on both servers. The
final FULL build at Nio `ed316ed` exposed a separate Tuwunel recovery defect:
only 73 roots completed before the deadline despite zero remaining queue debt.
A trace showed an exclusive pagination end preventing proof of retained-tail
overlap. Nio `aac2e32` removes that HTTP upper bound while retaining fixed page,
byte and continuation limits. Initial history and genuine unprovable gaps keep
their existing authorization rules. This is a producer pagination correction,
not a scheduling or durability relaxation. Nio's tracked design and measured
results preserve the failed control and regression coverage.


## Final durable batch controls

The final runtime is Nio `aac2e32` with MindRoom `ed4a209d6`. All installed
Python files and loaded module paths match those commits before and after each
control. No production behavior, deadline, health timeout, overlap threshold,
workload, or shutdown check was patched. Each run uses FULL durability, 200 roots,
a 180-second shared reply deadline, a 45-second minimum full visible overlap,
and a two-second health timeout. Reply times include about 60 seconds of
synthetic generation; they are not LLM latency predictions.

| Control | Reply median | Reply p95 | Full overlap | Acceptance |
| --- | ---: | ---: | ---: | --- |
| Tuwunel | 73.927 s | 86.501 s | 37.567 s | Overlap below 45 s |
| Tuwunel repeat | 74.830 s | 86.533 s | 37.470 s | Overlap below 45 s |
| Synapse 5,000-event cap | 76.987 s | 82.719 s | 47.352 s | PASS |

Every run completed exactly 200 replies and all three post-terminal fence
principals, with zero input/batch rows, journal work or delivery-outbox debt after
clean shutdown. No event-loop stalls, degraded reads, or incomplete-drain
warnings were recorded. Synapse passes every predicate. Both Tuwunel runs fail
only the original overlap requirement; do not describe them as full capacity
passes. Their recovered-request failure is corrected, and the failed pre-fix
73/200 run remains recorded above.

Compared with the earlier published controls, Tuwunel's median is roughly
unchanged and p95 is about 2.6 seconds slower (86.5 versus 83.9). Synapse's median
and p95 improve from roughly 80.1/87.6 to 77.0/82.7 seconds, and its previous fence
and retained-input failures are absent. These few runs are workload evidence,
not a statistical guarantee of general speedup. The much lower engine cost does
not remove the application's shared startup persistence cost. In the final
Tuwunel runs, preparation starts span 23.5-23.9 seconds; the sixteen-slot trial
already failed to improve this class of delay materially. A separate application
persistence optimization needs its own profile and design. No writer redesign,
serializer dependency, durability reduction, or relaxed threshold is justified
as part of this adapter replacement.

Detailed final controls: `controls-20260906T093405Z` and
`controls-20260906T094006Z` in the retained capacity workspace. The producer design and this scheduling
contract retain the guarantees and deliberate limits.


Final dependency verification pins Nio
`a686d43119a8c14b48d46a57858f70ee1579de55`, whose Python files match the tested
`aac2e32` wheel. The locked dependency installs from that Git commit. The full
consumer suite passes 15,522 tests with 22 skipped and 16 warnings in 119.58
seconds, with automatic resync disabled for the suite and its subprocesses.
All producer and consumer Python files match before and after the run. Complete
repository hooks pass, including ty, frontend checks and generated-document
checks. Nio's remote Python 3.12/3.13/3.14 tests, types, hooks and coverage also
pass. No production scheduling change accompanies this dependency update.


## Startup throughput follow-up and scale direction

The long-term capacity goal is more than 1,000 concurrent replies.
This is a design direction, not a capacity guarantee established by the current 200-root controls.
Measure burst startup, sustained streaming, recovery catch-up, memory, and health responsiveness separately.
Eight preparation slots bound preparation ownership; they do not limit the number of active model replies.
Future capacity qualification should increase the same workload through 200, 500, 1,000 and beyond, with explicit hardware and model timing, rather than extrapolating a local database benchmark.
Retain exact replies, durable admission, recovery, ordering, cancellation and shutdown checks at each size.

A follow-up investigation at MindRoom `556cacacb` and Nio `04c7f72` compared four small prototypes outside production.
The fresh uninstrumented Tuwunel baseline completed all 200 replies with a 73.392-second median, 84.028-second p95, and 22.539-second initial visibility spread.
Its 39.233-second full overlap still failed the unchanged 45-second requirement.
The baseline and all prototype runs settled the fence for all three principals and drained producer, journal and delivery work.

| Experiment | Initial visibility spread | Full overlap |
| --- | ---: | ---: |
| Unchanged baseline | 22.539 s | 39.233 s |
| Batch worker handoffs, retain separate FULL transactions | 21.788 s | 40.156 s |
| Group queued writes into a FULL commit | 22.820 s | 39.248 s |
| Repeat grouped commits | 22.520 s | 39.606 s |
| Exclude already-owned deferrals from ordinary pending payload reads | 22.294 s | 39.499 s |
| Combine producer capture and initial preparation commits | 21.931 s | 40.064 s |

These trials do not demonstrate a sufficient startup improvement to justify their additional production behavior.
No writer batching, transaction grouping, pending-query exclusion, or scheduler expansion is adopted from them.
The producer trial also needs consolidated failure ownership before it could ship: simply nesting existing transactions attempts to close a poisoned store before the outer transaction unwinds.
A single-run 0.608-second startup difference does not justify changing that boundary.
Reconsider these candidates only with new measurements demonstrating a worthwhile gain and tests covering their changed failure behavior.
The group-repeat operation histogram confirms that writes actually grouped; its interrupted final child hash report limits that run's source proof to the parent before/after hashes.
The other prototype conclusions remain supported by complete runtime source checks.

The timing trace contains two pending-turn writes per root: durable intent before generation and binding of the first visible response event.
They represent distinct facts, not redundant writes that can simply be removed.
Ledger waits overlap across concurrent preparations, so their sum is not end-to-end latency or time spent inside SQLite.
During one diagnostic startup interval, the 392 turn-record transactions used about 1.09 seconds of worker wall time; other admission and settlement work also occupied the shared writer.
Grouping application-writer commits experimentally did not remove the startup delay.
Treat the earlier shared-persistence attribution as a waiting location, not proof that disk or the writer design causes the remaining limit.

Keep the simpler producer/application ownership split and FULL durability.
Any next optimization needs a measured critical path and an unchanged-workload comparison before it becomes production code.
Do not add a second writer, persistent queue, recovery protocol, serializer dependency, or weaker durability merely to pursue the scale goal.
The retained `startup-throughput` capacity evidence contains the diagnostic wrappers, exact-cohort analysis, and rejected trials.


A separate 100 Hz sampled diagnostic places 8.66 seconds of main-thread sample weight in Nio SQLite commits across the full run, spread across capture, preparation, acknowledgement, and sync completion.
This measures sampled execution and blocking locations, not CPU-only time or an exact startup critical path.
The profiled run had a 28.576-second initial visibility spread; instrumentation perturbs timing, so its latency is not an uninstrumented capacity result.
This trace motivated the producer transaction trial above; neither result establishes the Matrix server as the cause of the remaining delay.


This investigation leaves production code unchanged in both repositories.
The remaining startup bottleneck is unresolved; the negative experiments do not qualify Tuwunel at the 200-root target or establish capacity for 1,000 active replies.
The next useful investigation is an exact-startup trace of producer processing, streaming traffic and scheduling together, rather than another isolated writer optimization.

Verification for this documentation follow-up: 15,522 consumer tests passed, with 22 skipped and 16 warnings; all repository hooks passed.
The producer suite passed 700 tests with three skipped, its full type check reported no issues in 58 source files, and its repository hooks passed.
Installed producer and consumer source hashes still match the tested commits; both production deltas are zero.

## Correlated profiling and ledger ownership

The subsequent exact-startup trace identifies the agent-wide handled-turn ledger lock as the critical serialization point.
At baseline, it remained held across unrelated admission and settlement work in the shared writer queue.
Nio commit stalls overlap these waits and are not additional wall time to sum into them.
The [ledger ownership design](ledger-write-ownership.md) records the correlated 50/100/200 traces, calibrated py-spy samples, rejected unsafe prototype, conflict rules and final controls.

MindRoom `5502b1177` reserves conflicting event identities through commit or rollback while allowing unrelated turns to await their own writes concurrently.
It retains both durable pending-turn writes, existing backend transactions, eight preparation slots and FULL durability.
The ledger grows by 47 net lines; shorter caller documentation makes the total production-file increase 28 lines.
Twenty new SQLite/Postgres cases cover independence, source/discovery aliases, old anchors, rollback, cancellation and cleanup.
The full suite passes 15,542 tests with 22 skipped; all repository hooks pass.

Alternating uninstrumented Tuwunel controls reduce initial reply spread from 21.69–21.81 seconds to 12.03–12.07 seconds.
Full visible overlap rises from 39.71–40.34 seconds to 49.74–49.90 seconds, passing the unchanged 45-second target in both fixed runs.
Completion p95 improves from about 83.3 seconds to 75.9–76.1 seconds, including approximately 60 seconds of synthetic generation.
All runs complete 200 exact replies, settle all three fence principals and drain producer, journal and outbox work cleanly.
Both fixed runs pass every acceptance predicate, resolving the previously recorded Tuwunel startup limitation for this workload.
This result does not qualify 1,000 concurrent replies or justify another scheduler, writer or durability change.
The same committed source also passes the unchanged Synapse control with 200 exact replies, 54.219 seconds of full overlap, all three fence principals and zero retained work after clean shutdown.
