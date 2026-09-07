# Ledger write ownership

## Purpose and evidence

Independent conversations must be able to prepare concurrently while every visible effect still waits for its required durable write.
Before this change, the handled-turn ledger held one agent-wide lock across both in-memory publication and the awaited database operation.
A correlated Tuwunel startup trace at MindRoom `738ee46bc` and Nio `fa587be` identifies that lock as the critical serialization point.

| Roots | First-to-last initial reply | Startup from first input | Ledger held | Ledger waiting in writer queue | Ledger's own worker execution |
| --- | ---: | ---: | ---: | ---: | ---: |
| 50 | 4.335 s | 4.663 s | 4.573 s | 3.943 s | 0.228 s |
| 100 | 9.983 s | 10.418 s | 10.315 s | 8.908 s | 0.473 s |
| 200 | 23.736 s | 24.277 s | 24.102 s | 20.660 s | 1.698 s |

These are instrumented timings, not uninstrumented capacity claims.
The matched 200-root control had a 21.811-second initial reply spread and 39.712 seconds of full overlap.
Tracing increased the observed spread by 8.8%; one pair cannot separate overhead from ordinary run variation.
All 50/100/200 controls completed exactly one reply per root, settled all three fence principals and drained producer, application and outbox work.
The 50/100 controls passed every predicate; the ordinary 200-root control failed only the unchanged 45-second overlap requirement.

The last traced 200-root request was admitted 0.354 seconds after its Matrix timestamp, then waited 22.060 seconds for preparation.
Admission and settlement operations occupied 12.612 seconds of the ledger's writer-queue wait.
Nio transactions also blocked the event loop for 8.297 seconds during startup, with about 1.89 seconds of transaction thread CPU.
That blocking overlaps the ledger waits and must not be added to them.
Deferral probes used 0.244 seconds, which does not justify another ownership-index redesign.

A 25 Hz py-spy trace includes idle threads and two clock markers.
Its clock offsets differ by 24.5 ms; the analysis retains approximately 92 ms of alignment uncertainty.
The sampled startup spends 8.64 seconds under Nio durable processing and 6.13 seconds in event-loop idle stacks.
Samples identify blocking locations, not CPU-only cost.
Request traces record the synthetic model generator and first chunk explicitly: this workload does not use an HTTP model for its replies.
The retained `startup-trace` evidence includes metadata-only request correlation, worker operation IDs, source hashes, an exact-cohort analysis, and a Perfetto timeline.

A diagnostic that partitions the ledger lock by benchmark root reduces initial visibility spread from 23.736 to 12.684 seconds with the same tracing.
It completes all 200 replies, fences and drains, with 49.611 seconds of full overlap.
This establishes a worthwhile optimization hypothesis.
That shortcut is not a production implementation: benchmark root context does not express alias conflicts or concurrent cleanup.

## Chosen ownership

Keep the existing backend transaction and cancellation behavior.
SQLite retains its one writer; Postgres retains its backend-owned transactions.
Keep FULL durability, both pending-turn writes, the eight preparation slots, and every existing producer guarantee.
Only conflicting ledger mutations need to serialize their complete read/derive/publish/commit-or-rollback sequence.
There is no required global commit order between unrelated turns.

The shared ledger state owns a transient map from affected event identities to completion futures.
The existing asynchronous lock protects reservation and exclusive maintenance, rather than remaining held while every unrelated database operation runs.
An update waits for earlier mutations affecting its lookup identities before deriving a candidate.
It also checks every identity and anchor that the candidate or resolved record can affect.
Existing records' anchors matter because the SQL upsert can delete old sibling indexes while re-anchoring a record.
A conflict causes the update to wait for settlement and derive again from the resulting state.
Update callbacks are synchronous derivations from the supplied records and may be evaluated again after such a wait.

Reserve all affected identities and publish the provisional record together under the existing state lock.
Release the reservation lock before awaiting persistence.
Keep each identity reserved until the write's outcome is known and any rollback is complete.
A completion future signals settlement, not success; the writer's own caller receives its exception.
Cancelling a waiter must not cancel another update's persistence or completion notification.
Different ledger instances for the same agent and backend share these reservations.

Cleanup excludes new reservations and waits for all active updates before deriving its retained set or deleting rows.
Loading and legacy import retain exclusive ownership.
No new persistent format, background writer, write-behind acknowledgment, retry protocol, serializer dependency or scheduler is introduced.
The reservation map exists only while callers have writes in flight and releases entries on success, failure and cancellation.

## Alternatives and deliberate limits

Keeping the global lock preserves correctness but imposes the measured unnecessary serialization.
Changing writer priority would make unrelated database operations compete under a new scheduling policy without expressing which ledger mutations actually conflict.
Locks keyed only by a benchmark root, conversation, or one source ID miss alias and old-anchor interactions.
Identity reservations belong in the ledger that already derives these relationships.

Durability still means the caller waits for its own committed write before continuing.
Synchronous readers may see provisional claims while persistence is in flight, preserving duplicate suppression.
Definite write failure restores the prior claim before dependent updates resume.
The existing conservative treatment of an externally cancelled write with an unknown outcome remains unchanged.
This change does not promise exactly-once external effects, remove bounded recovery limits, or qualify 1,000 concurrent replies.

## Implementation and verification

- Reproduce the blocking of an unrelated durable ledger update with a controlled store and real SQLite persistence.
- Preserve ordering for shared source IDs, discovery aliases, old anchors and re-anchoring; verify the resulting records after reload.
- Cover a failed provisional write followed by a dependent update, repeated cancellation, cancelled waiters and cleanup racing an active write.
- Implement reservations only in `src/mindroom/handled_turns.py`; keep test helpers in the ledger tests.
- Run the focused ledger/store tests, the complete suite and repository hooks, then self-review the changed ownership and cancellation paths.
- Compare alternating unchanged-workload controls against the original source, checking exact replies, fence, drain, health, shutdown and source identity.
- Record actual production size and measured results here before pushing the completed change.

## Implementation review

The production implementation changes only ledger ownership in `src/mindroom/handled_turns.py` and its caller documentation in `src/mindroom/turn_store.py`.
Relative to `738ee46bc`, the ledger adds 98 lines and removes 51: 47 net lines.
Condensing the caller documentation removes another 19 net lines, making the total production-file increase 28 lines.
The source change introduces no database or producer changes.

Twenty new cases exercise both real SQLite and Postgres backends.
The unrelated-write regression fails against the original global lock on both backends.
The conflict cases cover source and discovery IDs, old-anchor deletion, definite failure, repeated cancellation, cancelled waiters and cleanup exclusion.
Candidate-only conflicts are tested even when lookup IDs are unrelated: a provisional completed owner cannot permanently reject a competing candidate before its commit or rollback.
An unsafe early-return mutation fails all four candidate-only cases; the restored implementation passes them.
Independent review found no blocking issue in reservation closure, rollback, cancellation or cleanup ownership.
The complete suite passes 15,542 tests with 22 skipped and 15 warnings in 92.31 seconds.
The earlier focused ledger, turn-store and journal group passes 843 tests; the four candidate-only cases were added afterward and pass separately.
All repository hooks pass, including types, dependency boundaries, module privacy and frontend checks.

## Committed-source capacity controls

The actual implementation is `5502b1177`; the original-source control is `738ee46bc` in a separate checkout with the same locked dependencies.
Nio remains at source `fa587be`, matching the installed immutable pin `a686d43`.
The local host has 32 Neoverse-V2 ARM64 cores and about 126 GiB RAM; the application uses Python 3.13.14.
Each uninstrumented run checks committed package hashes and loaded module paths before and after execution.
FULL durability, eight preparations, 200 roots, a shared 180-second deadline, 45-second minimum full visible overlap and two-second health timeout remain unchanged.
Times include approximately 60 seconds of synthetic generation and are not real-model latency predictions.

| Tuwunel run | Initial reply spread | Full overlap | Completion median | Completion p95 | First input to last completion | Acceptance |
| --- | ---: | ---: | ---: | ---: | ---: | --- |
| Original, before implementation | 21.811 s | 39.712 s | 72.660 s | 83.334 s | 84.571 s | Overlap below 45 s |
| Implementation | 12.028 s | 49.901 s | 69.809 s | 76.096 s | 76.716 s | PASS |
| Original, repeated | 21.691 s | 40.339 s | 72.975 s | 83.304 s | 84.335 s | Overlap below 45 s |
| Implementation, repeated | 12.074 s | 49.744 s | 70.188 s | 75.945 s | 76.895 s | PASS |

Both implementations complete exactly 200 replies, settle all three post-terminal fence principals, retain no producer input/batch rows, application work or outbox debt, and shut down cleanly.
No event-loop stalls or degraded reads are reported.
The original fails only the performance requirement; both fixed runs pass every predicate.
The roughly 45% reduction in initial reply spread survives an alternating comparison without tracing or runtime patches.
Completion p95 improves by about 7.3 seconds; the full-overlap gain is not obtained by delaying completion.

Original process-tree CPU averages 61.9–65.6% of one core, versus 60.7–61.3% with the fix; peak rises from about 104% to 120% as work overlaps.
Peak process-tree RSS is 622–634 MiB originally and 625–640 MiB with the fix.
These few local controls establish a worthwhile improvement for this workload, not a universal latency bound or qualification for more than 1,000 concurrent replies.
The producer, writer, codec and durability policy remain unchanged.

Retained Tuwunel evidence is in `startup-trace/control-200-20260906T123518Z`, `control-200-20260906T130454Z`, `control-200-20260906T130753Z` and `control-200-20260906T131212Z` in the capacity workspace.
Earlier diagnostic failures and the unsafe root-partition prototype remain separately identified; neither substitutes for these actual-source controls.

The same committed implementation also passes the unchanged Synapse 5,000-event-cap control.
It completes all 200 replies with 13.156 seconds of initial reply spread, 54.219 seconds of full overlap, 75.327-second completion median and 80.001-second p95.
All three fence principals settle; producer, journal and outbox debt is zero after clean shutdown, with no event-loop stalls or degraded reads.
This is an additional server qualification, not an alternating Synapse performance comparison.
Its source-verified evidence is `controls-20260906T131520Z` in the capacity workspace.

## Next startup dependency, measured on the committed fix

The follow-up uses MindRoom `72a5f5d4f` (production change `5502b1177`) and Nio `adddd44`, with the same installed producer source and workload.
Diagnostic wrappers live outside the repositories; source hashes and loaded modules match before and after every run.
They correlate all 200 responder preparations with their actual task, awaited coroutine chain, capacity blockers, writer operation and worker thread.
A known 40 ms ready-but-not-running probe verifies the scheduling measurement, and all twelve real preparation-scheduling tests pass with instrumentation installed.
The trace drops no records.

The next dependency is the shared consumer database writer.
The eight preparation slots wait for active preparations; those preparations mostly await their first pending-turn persistence; that persistence queues behind other journal operations.
The previously removed global ledger lock is no longer the limiting owner.

| Measurement in the traced 200-root startup | Result |
| --- | ---: |
| First input to last initial visible reply | 13.357 s |
| Aggregate preparation lifetime, overlapping across tasks | 92.039 s |
| Aggregate preparation wait inside pending-turn persistence | 90.114 s (97.9%) |
| Preparation task CPU | 0.752 s |
| Capacity-wait wall-time union | 11.009 s |
| First slot release to capacity waiter resumption, median / p95 | 0.022 / 1.232 ms |
| Write call to dequeue, median / p95 | 498.646 / 751.012 ms |

The preparation lifetime and persistence waits overlap across tasks: they are not 92 or 90 seconds of end-to-end delay.
A ready preparation spends little time waiting to run; raising the preparation limit would not increase the serial writer's service rate.
The matched startup window contains 0.491 seconds handing work to a worker, 10.242 seconds inside the worker and 2.612 seconds propagating completion back through the writer.
Together these occupy 99.9% of the window, but worker thread CPU is only 0.971 seconds.
An occupied writer does not imply CPU saturation or continuous disk activity.
Admission accounts for 3.118 seconds of worker execution, turn upserts 1.767 seconds, delivery operations 3.421 seconds and hydration 1.619 seconds.
Nio synchronous transactions occupy approximately 1.008 seconds on the event loop in this run; this overlaps the other waits and must not be added to them.

A separate 25 Hz native py-spy capture finds 7.353 sampled wall-seconds in synchronization waits and 2.020 in filesystem synchronization out of 10.327 sampled writer wall-seconds.
These are weighted samples from the active writer's stacks, not CPU time or exact syscall durations.
Native symbols are incomplete; the condition-variable/futex frames do not identify every lock owner or prove that the GIL alone causes the delay.
Clock alignment retains 84 ms of uncertainty.
Native sampling increases initial reply spread to 15.612 seconds.
The application completes all replies, fences and drains, but the profiler parent exits with a child-reaping error after application shutdown.
That run fails the clean-shutdown predicate and is diagnostic evidence only.

Two separate uninstrumented sensitivity trials leave SQL, transactions, durability and preparation capacity unchanged.
One changes only the Python thread switch interval from 5 ms to 1 ms.
The other limits the existing ordinary SQLite offload pool from 32 workers to four; the separate recovery worker remains unchanged.
Both settings are verified in the child and labeled runtime diagnostic patches, rather than normal production controls.

| Run | Initial reply spread | Full overlap | Completion median | Completion p95 | Acceptance |
| --- | ---: | ---: | ---: | ---: | --- |
| Existing fixed-source controls | 12.028–12.074 s | 49.744–49.901 s | 69.809–70.188 s | 75.945–76.096 s | PASS |
| Task/writer tracing | 12.524 s | 49.891 s | 71.355 s | 77.134 s | PASS |
| 1 ms thread switch interval | 12.338 s | 49.771 s | 70.094 s | 76.093 s | PASS |
| Four ordinary SQLite workers | 12.430 s | 49.450 s | 70.454 s | 76.368 s | PASS |
| Final normal control | 12.083 s | 49.817 s | 69.975 s | 76.412 s | PASS |

The final normal control and both sensitivity trials complete all 200 replies, settle all three fence principals and leave zero producer, journal or outbox debt after clean shutdown.
The unchanged 45-second overlap and two-second health requirements pass.
Neither tuning trial establishes a startup improvement, so neither setting is adopted.
The four-worker trial uses less peak memory in this one run; that is not a repeated memory qualification or a reason to call it a latency fix.

Keep the existing design and FULL durability.
The next bounded investigation should count and time SQL statements within the existing admission transaction, especially repeated membership-state/epoch lookups visible in the native stacks.
Admission is the largest measured worker category; reducing repeated work there is a candidate to benchmark, not an established speedup.
Any reuse must preserve membership transitions within a batch and Postgres locking semantics.
Earlier grouped-commit and worker-handoff trials did not justify adoption; a saturated writer alone does not justify reviving those designs, adding another queue or increasing preparation concurrency.
No production code, dependency or correctness guarantee changes in this profiling follow-up; 1,000 concurrent replies remain unqualified.

Retained evidence is under `startup-waits` in the capacity workspace: `trace-200-20260906T141302Z`, `sample-200-20260906T141922Z`, `gil-001-200-20260906T142431Z`, `pool-four-200-20260906T142914Z` and `control-200-20260906T143211Z`.
The correlated reports distinguish task waits, ready delays, thread CPU, exclusive writer phases and sampled native waits rather than summing overlapping measurements.

## Membership-query experiment: not adopted

Candidate `ee48c59c1` passed the membership version already locked for each event into the existing admission helper, removing one redundant SELECT per event with nine net production lines.
Standalone admission retained its own transaction-local read, and later batch records still observed leave/rejoin transitions.
SQLite tracing confirmed the query reduction for normal and departure-suppressed events.
The candidate passed 42 focused tests and 15,546 full-suite tests, with 22 skipped and 15 warnings in 91.38 seconds; all repository hooks and scoped review passed.

Alternating uninstrumented controls compare the committed candidate with `d59028b0b` in a separate checkout and locked environment.
Every run verifies the committed source and loaded modules before and after execution, retaining FULL durability, eight preparations, 200 roots, the shared 180-second deadline, 45-second overlap and two-second health requirements.

| Run | Initial reply spread | Full overlap | Completion median | Completion p95 | Acceptance |
| --- | ---: | ---: | ---: | ---: | --- |
| Baseline A | 12.090 s | 49.852 s | 70.136 s | 76.161 s | PASS |
| Candidate A | 12.229 s | 49.792 s | 70.061 s | 76.040 s | PASS |
| Baseline B | 12.153 s | 49.846 s | 70.371 s | 76.463 s | PASS |
| Candidate B | 12.436 s | 49.827 s | 70.302 s | 76.371 s | PASS |

All four runs complete exactly 200 replies, settle all three fence principals and leave zero producer, journal or outbox debt after clean shutdown.
The candidate does not demonstrate a worthwhile startup gain: initial reply spread is slightly worse in both comparisons, and completion latency is nearly unchanged.
Removing this read changes neither transaction count nor worker handoffs; a lower query count alone does not justify expanding the production call signatures.
Restore the original production code and remove the query-count tests tied to the rejected implementation.
Keep the existing writer and durability policy; reconsider this optimization only with new evidence of a worthwhile end-to-end gain.

Retain the useful leave/rejoin test covering both single and separate batches on SQLite and Postgres.
An initial full run exposed a 500 ms guard in the projection-progress test; a controlled 650 ms delay after successful admission reproduced that failure without changing the database result.
That test now sets a 30-second retry backoff and a two-second hang guard, retaining the requirement that admission progress independently of unrelated retries while allowing ordinary database latency.
The controlled-delay probe passes, and scoped review confirms that waiting for another recovery pass still fails the adjusted test.
Production retry timing is unchanged.
Final retained-tree verification passes 15,544 tests with 22 skipped and 15 warnings in 87.87 seconds; production source matches the measured baseline exactly.

The four retained controls are `control-200-20260906T144825Z`, `control-200-20260906T145836Z`, `control-200-20260906T150134Z` and `control-200-20260906T150430Z` under `query-reuse` in the capacity workspace.

## Remaining writer time

The earlier correlated trace on the retained production code attributes the 13.357-second startup window as follows.
Each operation category includes its worker submission, execution and completion propagation, so these categories can be added without counting shared wait time twice.

| Operation category | Exclusive writer occupancy |
| --- | ---: |
| Delivery/outbox: enqueue, claim, sending-device binding and acknowledgment | 4.407 s |
| Batch admission | 3.242 s |
| Handled-turn ledger writes | 2.802 s |
| Conversation hydration | 2.225 s |
| Event settlement | 0.669 s |
| Writer unoccupied | 0.012 s |

Across those categories, 0.491 seconds is worker submission, 10.242 seconds worker execution and 2.612 seconds completion propagation; worker thread CPU is only 0.971 seconds.
Those phase totals and the separate native synchronization samples overlap the table and must not be added to it.
This identifies the application operations occupying the writer, but native symbols still do not resolve every low-level synchronization owner.
Reply-completion measurements additionally include approximately 60 seconds of synthetic model generation; the table describes startup, not that generation interval.

## SQL replay and native wait attribution

The next investigation resolves most of the previously unidentified writer waiting to CPython GIL acquisition.
It uses MindRoom `e276982fdbd3f58b8d471376c417767654819da4` and Nio `ce18a1fed0a592b93b789de4480ac3e61e8c3145`, without changing either production source tree.
The [performance evidence index](durable-ingestion-performance.md) records reproduction commands, source provenance, artifact locations and earlier decisions.

### Reply transaction boundaries

| Transition | Existing persistence and required ordering |
| --- | --- |
| Prepare response | Commit pending turn context before response generation starts. |
| Send initial placeholder | Enqueue frozen delivery, claim attempt and device, bind sending device, send to Matrix, then acknowledge. |
| Bind visible reply | Persist the returned Matrix event ID in the pending turn context. |
| Finish response | Enqueue FINAL intent and settle its source events atomically; claim/bind device, send the edit, then acknowledge with terminal facts and projection. |
| Publish terminal state | Reconcile committed terminal facts with concurrent ledger mutations and controller completion. |

Network effects require committed intent before sending and acknowledgment after the result.
FINAL source settlement, acknowledgment projections, membership fences and INITIAL-before-FINAL ordering remain required.
There are possible redundant local writes, but these must be distinguished from those crash boundaries.
Fresh claim already persists the sending device; its following device-binding transaction is a candidate for removal only when the caller proves it owns that fresh claim.
Live enqueue and claim could potentially share a transaction, provided an accepted FINAL still transfers source ownership when an unresolved INITIAL prevents immediate claiming.
Recovery of an older attempt must retain its original device until reconciliation succeeds.
The durable terminal publication rewrite is not generally redundant: the concurrent-redaction regression in `test_response_delivery_gateway.py` demonstrates why it exists.
None of these candidates is implemented or claimed as a measured speedup here.

### Same SQL, different execution environment

The diagnostic captures writer SQL, parameters and fetched results in memory, starting from a SQLite backup after schema setup.
After the application stops, it replays the same statements and commit boundaries on the same filesystem with `synchronous=FULL`.
Full direct replay checks every fetched result and the final logical database contents; threaded replay checks the startup prefix against the direct replay's matching prefix.
Both live/replay comparisons use exactly the same complete operation IDs that start between the first workload input and last initial visible reply.
Operations may finish after that window; their summed phases are not a clipped wall-time partition of it.

| Diagnostic | Startup operations | Live worker wall time | Direct SQL replay | Threaded SQL replay, including handoff |
| --- | ---: | ---: | ---: | ---: |
| First valid 200-root trace | 1,722 | 11.439 s | 1.409 s | 1.459 s |
| Trace with all native waits counted | 1,719 | 11.130 s | 1.340 s | 1.520 s |

The second direct replay verifies 80,407 fetched results across 10,508 full-run transactions and matches the final database.
Its threaded startup prefix verifies 20,859 fetched results across 2,023 prefix transactions; the table selects the same 1,719 startup operations from each replay.
Those operations include shared sync, admission and hydration work, not just 200 independent reply lifecycles.
Replay omits application computation, concurrent readers and most tracing overhead, and changes cache/checkpoint and competing I/O conditions.
The ratio is evidence of runtime contention; it is not an eightfold achievable application speedup.

### What occupies the live writer

In the second trace, the selected worker operations use 1.272 seconds of thread CPU during 11.130 seconds of worker wall time.
Native counters record 98,212 timed condition waits totaling 7.693 seconds, all on one condition object whose sampled native stacks identify CPython `take_gil`.
This includes wakeup scheduling and mutex reacquisition, not exclusively time another thread holds the GIL.
They also record 1,379 `fsync` calls totaling 2.298 seconds.
Counters cover every selected operation, including waits shorter than the 500-microsecond stack-capture threshold, with no buffer overflow or unattributed condition-object time.
The native wrapper covers slightly more bookkeeping than the worker timer, and timed calls include some CPU/kernel time; these values and thread CPU must not be summed as an exact disjoint decomposition.
The probe intercepts four wait/sync APIs, not every possible blocking operation or scheduler delay.

The database worker repeatedly relinquishes and reacquires Python execution while stepping and fetching SQLite results.
[CPython's SQLite implementation](https://github.com/python/cpython/blob/3.13/Modules/_sqlite/cursor.c) releases the GIL at multiple execution and row-conversion boundaries.
The measured native stacks include statement execution and fetching; they do not prove all 98,212 waits originate at one particular source line.
After worker completion, scheduling the result-transfer callback costs another 1.411 seconds across these operations, followed by 1.552 seconds before the writer reports the copied result.
Submission adds 0.553 seconds; the remaining measured completion phases total about 0.043 seconds.

Inclusive main-loop callback CPU includes streaming-chunk consumption (1.418 seconds), lazy response startup (0.701), Nio sync (0.669), ingestion pumping (0.485) and pending-event lanes (0.342).
These callback names include awaited generator/provider code executed within the callback; they are leads for narrower profiling, not exclusive source-line costs.
The main loop and SQLite readers compete with the writer for Python execution even though the host has spare cores.
Changing the thread switch interval or pool size already failed to improve normal controls, so this finding does not revive those tuning changes.

Both valid traced runs complete 200 replies, settle three fence principals, drain all producer/application/outbox debt and shut down cleanly with verified source identity.
Their initial reply spreads are 14.056 and 13.726 seconds; a fresh normal control is 11.983 seconds with 49.873 seconds of full overlap and 69.730/75.561-second completion median/p95.
That normal control passes every unchanged predicate.
The tracing overhead is material and prevents treating diagnostic timings as normal production latency.
An earlier 200-root trace had a postprocessing error that mistook three principals' copies of one physical reply for duplicate replies; it is excluded from accepted controls.
An accidentally started duplicate diagnostic was stopped and is also excluded.

### Direct event-loop writer: not adopted

A final diagnostic executes the existing FULL transactions on the event loop, yielding explicitly between transactions.
It changes neither SQL nor commit boundaries, but changes both thread handoffs and event-loop scheduling, so it cannot isolate GIL effects alone.

| Execution policy | Initial reply spread | Full overlap | Completion median | Completion p95 | Capacity acceptance |
| --- | ---: | ---: | ---: | ---: | --- |
| Normal worker | 11.983 s | 49.873 s | 69.730 s | 75.561 s | PASS |
| Diagnostic event-loop writer | 11.768 s | 50.813 s | 71.042 s | 76.847 s | PASS |

Both runs complete all 200 replies, settle three fence principals, leave zero debt and shut down cleanly with source verification.
This single comparison establishes no useful startup improvement; completion latency is worse in the diagnostic.
A separate real SQLite contention probe holds `BEGIN IMMEDIATE` on another connection for 2.2 seconds.
Both writer policies commit successfully after about 2.232 seconds, but a callback scheduled for 20 ms runs only 0.115 ms late with the normal worker and 2.212 seconds late with the event-loop writer.
An external thread-export writer is a supported source of such contention; the backend's busy timeout is ten seconds.
The diagnostic therefore both lacks a demonstrated performance benefit and exposes loop blocking under realistic contention.
Keep the existing offloaded writer, FULL durability and eight preparation slots.

The investigation is complete; it ships zero production lines and no new dependency or guarantee reduction.
The next bounded candidate is eliminating the provably fresh attempt's duplicate device-binding transaction, followed by measuring whether combining live enqueue/claim is worthwhile.
These are candidates, not promised speedups or authorization to weaken recovery, membership or source-settlement semantics.
A new database process, different driver, larger preparation pool or broad scheduling rewrite is not justified by these measurements alone.
1,000 concurrent replies remain unqualified.

## Bounded delivery-transaction trial

This experiment compared two provisional candidates against the retained production source at `1b10db607`.
Both were subsequently withdrawn; the proposed API below is not part of the retained architecture.
First, omit device rebinding only when a fresh claim already committed this worker's sending device.
Previously attempted deliveries keep the existing reconciliation and pre-send device write, even when the returned marker matches.
Second, add `enqueue_and_claim_matrix_delivery` to the delivery store view, using the existing enqueue and claim operations in one backend transaction.
Its result distinguishes refused enqueue from accepted enqueue with a blocked claim: return `(accepted, claimed)` so FINAL still releases its source handoff when INITIAL prevents sending it yet.
The worker consumes that committed claim through the same post-claim checks used by recovery.
Separate enqueue and claim remain available to callers that intentionally persist an intent before a later recovery pass.
No network call or callback moves into a transaction, and the existing membership, frozen-payload, device-reconciliation, cancellation and INITIAL/FINAL ordering rules remain required.

- [x] Test that a fresh send's device intent is committed before network I/O without a duplicate device write; keep retry and changed-device coverage.
- [x] Implement and benchmark the first candidate against an uninstrumented 200-root control.
- [x] Test combined enqueue/claim atomicity, blocked FINAL source handoff, refusal and recovery behavior; then implement the second candidate.
- [x] Benchmark the combined candidate under the same FULL durability, eight preparations and original capacity predicates.
- [x] Withdraw both candidates, verify the restored tree, self-review and record the result for the authorized push.

Run controls sequentially with fixed source identity and no competing tests or benchmarks.
If a result is small or inconsistent, do not grow the implementation to rescue it.

### Measured outcome: withdraw both candidates

Candidate A (`91eb09197`) omitted the duplicate fresh-claim device write and added two net production lines.
Candidate B (`92c19b9dc`) additionally combined live enqueue/claim, adding another 82 net production lines: 84 total versus the pre-trial source.
Candidate A passed 231 focused tests; candidate B passed 244, including real SQLite/Postgres commit-crash, failed-claim rollback, blocked FINAL handoff, membership refusal and device-recovery cases.
Both candidates passed scoped repository hooks before measurement.
A scoped review found no correctness blocker; it identified two fake-only cancellation pause points that would need moving after the combined commit if that candidate were retained.
Those candidate-specific tests and interfaces are withdrawn with the implementations, rather than expanding the rejected trial.

All four controls use the unchanged workload, FULL durability, eight preparations, no profiler and no competing tests.
The later baseline checkout at `d59028b0b` has the same production source as `1b10db607`; the two launchers' control implementations and child wrappers are byte-identical.
All runs verify runtime source, complete 200 replies, settle the three-principal fence, leave zero producer/application/outbox debt and shut down cleanly.

| Source | Initial reply spread | Input to initial median | Input to initial p95 | Completion p95 |
| --- | ---: | ---: | ---: | ---: |
| Earlier baseline | 11.983 s | 6.012 s | 11.924 s | 75.561 s |
| A: fresh claim only | 12.726 s | 6.149 s | 12.626 s | 76.300 s |
| B: A plus combined enqueue/claim | 12.516 s | 5.580 s | 12.273 s | 75.835 s |
| Fresh baseline | 13.493 s | 6.593 s | 13.173 s | 80.150 s |

Full overlaps are respectively 49.873, 49.651, 49.713 and 50.629 seconds; every original capacity predicate passes.
Initial latency uses each exact workload input and its physical Matrix reply creation timestamp, deduplicating projections across principals.
Completion includes approximately 60 seconds of synthetic generation.
The combined candidate has a promising median in one run, but neither candidate improves the earlier baseline's startup tail; both spreads lie inside the two controls' range.
These few runs do not establish statistical equivalence, rule out small gains, or attribute the slower final control to a particular cause.
They provide insufficient evidence to keep 84 extra production lines for a startup optimization.
Both implementations are withdrawn and the production source, tests and whitelist restored exactly to the pre-trial tree.
The previously retained ledger-concurrency improvement remains intact; no durability or ownership guarantee changes.
Do not repeat these candidates without a new workload or evidence that changes the decision; this bounded trial is complete, and 1,000 replies remain unqualified.

Exact revisions, run directories, metrics and result checksums are tracked in `durable-ingestion-performance-results.json` under `delivery_transaction_trial`.
The persistent `durable-sync-kernel/delivery-transactions` directory contains test logs, all three new control logs, the comparison helper and its output.
The evidence index records the reproduction addendum.

Final retained-tree verification passes 15,544 tests with 22 skipped and 16 warnings in 121.51 seconds; all consumer hooks, including types and frontend checks, pass.
Nio documentation hooks pass, and both repositories retain their previously qualified production source.
Portable re-analysis reproduces all six reported reply timing metrics exactly for each of the four runs from payload-free cohort timestamps.
