# Durable ingestion performance evidence

This is the entry point for the September 2026 producer/consumer investigation.
Use it with the [ledger ownership and measured results](ledger-write-ownership.md) and the producer's `docs/design/durable-sync.md` and `docs/design/durable-sync-plan.md` in `mindroom-nio`.
Those documents define the retained guarantees and supersede the retired ingestion-engine plans.
The benchmark result is 200 concurrent conversations with one responder account, three syncing principals and a synthetic model; it does not qualify 200 independent agents or 1,000 concurrent replies.

## Decisions to retain

| Question | Result and decision | Evidence to read |
| --- | --- | --- |
| Was the old producer doing excessive work? | Yes; shared interpretation and durable batches replace the old engine. The final 200-event plain FULL capture/prepare/read/ack cycle is 23.2 ms versus 196.7 ms for the old NORMAL implementation. This is a local engine benchmark, not reply latency. | Producer plan, final engine table; `durable-sync-kernel/micro-final-20260906T091913Z`. |
| Do we need orjson or PR #57's implementation? | Retain stdlib JSON. Batching already reduces 200-event plain decoding from 1,854 to 3 and commits from 404 to 3; the old-engine patch is not used. | Producer plan, measured engine results. |
| Were the earlier failures just a slow server? | No; the old capacity evidence exposed real recovery/fence defects. The producer replacement and coordinated consumer fixes have separate correctness tests and final acceptance evidence. | Producer contract/plan; original `profiling-report.md` is historical evidence. |
| Did removing global ledger serialization help? | Yes; alternating Tuwunel controls reduce initial spread from 21.69–21.81 s to 12.03–12.07 s. Keep identity-based conflict ownership. | Ledger ownership document; `durable-sync-kernel/startup-trace`. |
| Should preparation concurrency increase? | The measured preparation wait is predominantly pending-turn persistence. Larger limits did not fix it. Keep eight slots. | Producer plan and ledger dependency-tracing sections. |
| Do smaller worker pools or a shorter GIL switch interval help? | Neither demonstrates a startup gain; retain existing settings. | `durable-sync-kernel/startup-waits`. |
| Does one fewer membership SELECT help? | No worthwhile end-to-end gain in two alternating comparisons. The nine-line production candidate was withdrawn; useful membership-transition tests remain. | Ledger membership-query section; `durable-sync-kernel/query-reuse`. |
| Why is the writer occupied without much CPU use? | Live SQLite execution contends for Python execution and then waits for completion propagation through the event loop. Same-SQL isolated replay is much faster, with important limits. | Ledger SQL/native-wait sections; `durable-sync-kernel/transaction-path`. |
| Should SQLite writes run directly on the event loop? | No useful gain in the diagnostic, and real database contention blocks the loop. Keep the existing writer. | Ledger direct-writer section; transaction-path lock probe and inline run. |
| Do fewer delivery transactions improve startup? | Neither tested candidate establishes a repeatable startup-tail improvement. Both are withdrawn; retain the existing delivery path. | Ledger bounded delivery-transaction trial; tracked JSON `delivery_transaction_trial`. |

The earlier writer investigation changed no production code.
Its measurements explain costs; they do not establish a general speedup, justify weaker durability, or prove all possible failures impossible.
Reply completion includes roughly 60 seconds of synthetic generation, so an engine or startup speedup does not translate proportionally into total response time.

## Retained artifacts

All paths below are relative to the persistent capacity evidence workspace originally supplied with `profiling-report.md`.
The searchable archive is `reproducibility/20260906-writer-investigation.tar.gz`, with an extracted sibling directory of the same name.
It contains the campaign's available scripts/reports/result manifests, dependency locks, runtime versions, server-image identities observed at preservation time, and the latest four runs' metadata traces.
It includes exact historical native libraries, the earlier trace-child variant reconstructed and verified against its recorded SHA-256, and native symbol tables for portable re-analysis.
`MANIFEST.sha256` checks every packaged file; the companion archive checksum verifies the package itself.
The archive is retained in the capacity workspace, not hosted in this Git repository.
Raw application logs, synthetic database files and server state remain in their original evidence directories; they are not needed for the packaged metadata analysis.
The tracked [measurement summary](durable-ingestion-performance-results.json) preserves the key numeric results, source revisions and run mappings independently of the archive.

| Latest run | Driver directory under `durable-sync-kernel` | Separate evidence directory under `durable-sync-kernel` |
| --- | --- | --- |
| Valid SQL/native trace | `transaction-path/trace-200-20260906T153922Z` | `mindroom-live-matrix-fuzz-68e81282` |
| Valid trace counting short native waits | `transaction-path/trace-200-20260906T154420Z` | `mindroom-live-matrix-fuzz-bc2989a3` |
| Normal control | `startup-waits/control-200-20260906T154923Z` | `mindroom-live-matrix-fuzz-71dab4e7` |
| Inline diagnostic | `transaction-path/inline-200-20260906T155552Z` | `mindroom-live-matrix-fuzz-ecc9c42a` |
| External writer contention | `transaction-path/lock-contention-20260906T155542Z` | Same directory; `results.json` and probe databases. |

`trace-200-20260906T153559Z` failed postprocessing and `trace-200-20260906T154323Z` was stopped; neither is an accepted capacity control.
The 50-root trace is an instrumentation calibration, not 200-root qualification.
Older measurements belong to their recorded revisions; the original report's Nio CPU percentages do not describe the replacement producer.

## Re-analysis and fresh reproduction

The package README gives the complete commands and path configuration; use its checksums before analysis.
From the extracted package, run the following with a Python 3.13 environment:

```sh
sha256sum -c MANIFEST.sha256
uv run --no-project --python 3.13 python analyze-retained.py mindroom-live-matrix-fuzz-bc2989a3
```

The portable wrapper uses archived symbol tables, avoiding a dependency on this host's absolute interpreter/library paths.
It reproduces the SQL/native/future timing report from retained metadata.
SQL parameters and fetched results existed only in the diagnostic child's memory and were discarded at exit.
The old exact SQL replay therefore cannot be rerun from database snapshots alone; reproduce it by running the synthetic workload again with the SQL probe installed.

For a fresh run, restore MindRoom `e276982fdbd3f58b8d471376c417767654819da4` and Nio `ce18a1fed0a592b93b789de4480ac3e61e8c3145` in persistent checkouts, install the locked consumer environment and verify installed producer source against the Nio checkout.
Use the package's complete helper hierarchy and update the documented checkout/evidence path constants when relocating it.
Run the normal control first, then the diagnostic, sequentially on the same filesystem without competing tests or benchmarks.
The controls retain FULL durability, eight preparations, 200 roots, the shared 180-second deadline, 45-second overlap, two-second health timeout, three-principal fence, debt audit and clean shutdown.
Verify source hashes before and after each run; runtime probes must be labeled diagnostic even when the production source hashes match.
Never change runtime source or probe files during a run, compare profiled latency directly with normal latency as a speedup, or add overlapping task/native/CPU timings together.

The original launcher identity hashed the extracted control function rather than every helper, and did not record the native compiler invocation or immutable server image identity per run.
The package preserves the full available helper files, exact native binaries and a rebuild recipe, but these missing historical facts cannot be recreated as contemporaneous proof.
New runs should capture complete helper hashes and resolved image IDs at launch.
The exact native/source probe variants used by the two valid traces are mapped in the package README; the earlier trace child matches its recorded hash.


## Delivery-transaction trial addendum

The follow-up tested skipping duplicate fresh-claim device binding and then combining live enqueue/claim, adding two and 84 cumulative net production lines respectively.
The candidates passed focused correctness checks and all 200-root capacity predicates, but did not establish a repeatable startup-tail benefit against normal controls.
Both are withdrawn: zero retained production lines, dependencies or guarantee changes.
The [ledger decision](ledger-write-ownership.md#measured-outcome-withdraw-both-candidates) and tracked JSON retain the median/p95 results, source revisions and limitations.

| Run | Driver directory under `durable-sync-kernel` | Evidence directory |
| --- | --- | --- |
| Fresh-claim candidate | `startup-waits/control-200-20260906T162827Z` | `mindroom-live-matrix-fuzz-ba6c5689` |
| Combined candidate | `startup-waits/control-200-20260906T163429Z` | `mindroom-live-matrix-fuzz-d1624107` |
| Fresh baseline | `query-reuse/control-200-20260906T163755Z` | `mindroom-live-matrix-fuzz-22048dac` |

The earlier normal control is listed in the main artifact table.
The persistent `durable-sync-kernel/delivery-transactions` directory preserves the trial logs, comparison script and output; original synthetic databases and application logs remain in each run's evidence directory for cohort re-analysis.
The separate `reproducibility/20260906-delivery-transactions.tar.gz` addendum freezes the trial results, available helper files and verification logs with a `MANIFEST.sha256` and companion archive checksum.
It leaves the earlier writer-investigation archive unchanged.
Use the addendum README for reproduction commands; the original source-verification and historical helper/image-identity limitations still apply.

The addendum contains 120 files (1,034,820 compressed bytes), SHA-256 `93974903f48928b9cafd855af9be18071910c56f6ff79a2a40afa4d533031ae4`.
Its manifest verifies successfully, and standalone timestamp re-analysis exactly reproduces all six timing metrics for every run.


## Sliding restoration: matched 200-reply controls

Producer `f7e1dfd78812c8c58d846bc8e62df698903c18bf` and consumer
`d820bc3a40b6661ff709b3db09c253f29defea1e` pass the same 200-reply capacity
profile in both transports on Tuwunel and Synapse. Each run uses the locked
Python 3.13 consumer environment and the exact rebuilt producer wheel. The
runner verifies clean source, installed/loaded package hashes, helper hashes and
immutable Docker image IDs before and after the run. Controls run sequentially
without competing test processes. Synapse retains the earlier 5000-event cap.

All four pass exact 200-root/200-reply delivery, three-principal fences and source
advancement, the 180-second measured deadline, at least 45 seconds full overlap,
two-second health timeout, zero producer/application/outbox debt and clean stop.
The workload uses one responder account and roughly 60 seconds of synthetic
model generation. It does not measure 200 independent agents or qualify 1,000
concurrent replies.

| Server | Transport | Initial median / p95 (s) | Completion median / p95 (s) | Initial spread (s) | Full overlap (s) |
| --- | --- | --- | --- | --- | --- |
| Tuwunel | Classic | 5.862 / 11.928 | 69.898 / 76.008 | 11.669 | 50.538 |
| Tuwunel | Sliding | 6.172 / 11.974 | 70.088 / 76.017 | 11.817 | 50.134 |
| Synapse | Classic | 5.845 / 11.072 | 75.343 / 79.175 | 12.413 | 55.759 |
| Synapse | Sliding | 5.556 / 10.915 | 74.808 / 78.859 | 12.006 | 56.155 |

These single-run comparisons show no clear reply-speed advantage for Sliding.
Small differences change direction between servers; they establish neither a
repeatable speedup nor statistical equivalence. Keep Sliding for room discovery,
explicit subscriptions and bounded windows, with similar observed performance.
Classic remains the default. No serializer, SQLite durability, worker count or
preparation limit changed for this restoration.

The corresponding directories under `durable-sync-kernel/sliding-restoration`
are `tuwunel-classic-capacity-20260906T183405Z`,
`tuwunel-sliding-capacity-20260906T182731Z`,
`synapse5000-classic-capacity-20260906T183647Z`, and
`synapse5000-sliding-capacity-20260906T183045Z`.
Both Sliding runs also pass the real-server probe: 20 downtime messages and a
20-message limited live window recovered exactly, no loss or duplicates, ten
actual history pages and no stored debt. Actual encrypted late-key and
process-kill behavior have separate producer regression tests.

Live qualification found a real backward-expanded-window bug: old membership
context could rewind the joined tenure and demote a fresh request to history.
The producer fix uses the existing bounded checkpoint, retains the proven
recovery floor and keeps older context out of future recovery. It adds 58 net
production lines; the architectural policy and tests live in the producer's
`docs/design/sliding-sync-restoration.md`. Historical ciphertext remains observed
history; only actionable ciphertext is automatically eligible for promotion when
resent with a key. There is no new replay queue, store, owner or walker.

The complete restoration adds 1,526 net Nio production lines, including the
25-line released-store adoption guard. Against main `5b6de3bc`, the complete Nio
PR is +5,056/-6,437, or 1,381 fewer production lines. Counts include comments and
blank lines, and exclude tests, scripts and documentation. The final producer
suite passes 844 tests with three skips, mypy is clean across 60 source files,
and repository hooks pass. The rebuilt consumer wheel passes 205 affected tests
and all consumer hooks.

Earlier failures remain in the tracked JSON and retained evidence:

- `tuwunel-sliding-capacity-20260906T174737Z` failed its two-second health read
  timeout during the workload. It completed zero replies before abort and left
  331 application rows pending. Its cause was not established; passing later
  runs do not explain that failure.
- `tuwunel-classic-capacity-20260906T175135Z` timed out before the workload: the
  harness sent its warm-up before the responder's committed room baseline. The
  harness now waits for that baseline before warm-up (`4ef0f6734`), outside the
  measured deadline. The workload criteria are unchanged.
- `tuwunel-sliding-capacity-20260906T180630Z` exposed the actual expanded-window
  bug even after the harness correction. `20260906T181030Z` is its deliberately
  short startup diagnostic, not a capacity test. Both sent zero workload roots.
- `tuwunel-sliding-capacity-20260906T175756Z` completed all 200 while sampled,
  but its profiler wrapper failed to confirm clean shutdown, so it is not a
  qualified control. PySpy observed 3,929 main-thread wall samples at 25 Hz;
  612 were in SQLite commit, including 335 during acknowledgement. These cover
  setup and workload, are not CPU time or an additive phase budget, and do not
  explain the earlier unprofiled health timeout.

The pre-fix passing Synapse Sliding run and corrected Classic Tuwunel run are
also retained, with their original source revisions. They are not substituted
for the final matched controls above.

### Application restart qualification

The final Sliding restart profile passes on both servers with producer `f7e1dfd`
and consumer `9883dba3a`: Tuwunel `20260906T184907Z` and Synapse
`20260906T184922Z`, under the same `sliding-restoration` directory with server,
transport and `restart-regression` prefixes. It exercises actual bot replacement,
a fresh pending request interrupted during model execution, hard process restart,
recovered output, historical text/media remaining non-actionable, on-demand
history hydration, and empty retained producer/application/outbox work at clean
shutdown. Both pass with zero historical outputs and zero drain failures.

Two stale harness assumptions were fixed without changing production source:

1. The original room/model configuration update now applies in place. Both
   `20260906T183941Z` (Tuwunel) and `20260906T184117Z` (Synapse) timed out waiting
   for replacements that the planner correctly did not request. Commit
   `04bc7105d` changes a construction prompt to request both replacements while
   preserving transport, model latch routing and every lifecycle assertion.
   The written-config-to-real-planner test failed first in both sync modes.
2. The next Tuwunel run (`20260906T184417Z`) reached fresh admission, pending
   journal state and blocked model execution, then waited for the retired
   Classic checkpoint file. Commit `9883dba3a` checks exact event projection,
   then the matching SQLite producer's committed cursor and empty input/batch
   tables in one read. Producer input is replaced atomically by a completion
   batch, so this proves acknowledgement without an intermediate false-ready
   state. It does not require an idle server to change an opaque position.
   Real producer fixtures cover both kinds of pending work and an unrelated
   empty account. All 130 harness tests and applicable hooks pass.

These corrections affect only the restart harness and its tests. The four
matched capacity controls retain their original committed source and harness
hashes; no new production or capacity-path change followed those measurements.

### Reproduce the Sliding comparison

The addendum archive is `reproducibility/20260906-sliding-restoration.tar.gz` in the persistent capacity evidence
workspace, with an extracted sibling directory and `.tar.gz.sha256` companion.
It contains 94 manifested files (3,916,060 compressed bytes), SHA-256
`cb8b04d1404a319346da0cb8b279980bf59c975a7d8fc8e987afafc2933f4618`. It freezes the driver hierarchy, all 16 run/source/result
manifests, historical harness variants, final source and lock snapshots,
verification logs, metadata diagnostics and payload-free timestamp cohorts.
Application logs, databases, credentials and message bodies are excluded.

All manifest checks pass. Standalone timestamp re-analysis reproduces all seven
complete 200-reply cohorts exactly, including the explicitly unqualified sampled
run. From the extracted directory:

```sh
sha256sum -c MANIFEST.sha256
uv run --no-project --python 3.13 python capacity/durable-sync-kernel/sliding-restoration/reanalyze.py
```

The archive README gives the fresh-run commands, required committed checkouts,
locked environment, location configuration and immutable image checks. Run
`run_qualification.py` with `--profile capacity --sync-mode classic` or
`--sync-mode sliding`, selecting `--server tuwunel` or `--server synapse5000`.
Use `--gap-probe` for Sliding history probes and `--profile restart-regression`
for application restart checks. Keep runtime source frozen during each run.

## Streaming-frequency diagnostic

The next bounded experiment tests whether servicing existing streams delays new
replies. It follows the review corrections to producer `292ab7d`; the consumer
must first install an exactly pinned wheel containing those corrections.

Keep the existing 200-root Sliding workload on Tuwunel: 4,800 response characters,
80 characters per second, deterministic model/tool behavior, FULL durability,
eight preparations, 180-second deadline, 45-second full overlap, two-second
health timeout, exact delivery, three-principal fences and empty stores at clean
shutdown. Compare subsequent chunks of 40 versus 400 characters in ABBA order.
The first chunk of each model phase stays at 40 characters in both variants:
the synthetic provider sleeps before yielding, so changing that chunk would
confound startup overhead with a different first-token delay. Each phase still
emits the same text and sleeps for the same nominal total duration.

This is an explicitly declared diagnostic override in a source-verified child,
not a production configuration or a change to the original capacity control.
Record helper and package hashes, actual Matrix update counts, per-reply
input/initial/final timestamps, all acceptance predicates and every failed run.
Run sequentially without competing tests or profilers. Compare both pairs;
confirm on Synapse only if a consistent benefit warrants it. A small or mixed
result does not justify more code, another serializer or another database.
The load curve and 1,000-reply qualification remain separate questions.

### Streaming-frequency results

The four sequential Tuwunel Sliding runs use producer
`ba75e3a04bb5e333b40617027723e7982d4de09a` and consumer
`2fb726841f7f4c3526b1610ac91eea6afa9fedc1`, with the exact rebuilt wheel. The
producer fixes all four reported trust/membership/power-state failures. Its full
suite passes 863 tests with three skipped, mypy is clean across 60 files, and
repository hooks pass. The consumer passes 200 affected tests and all hooks.
Producer fixes add 101 net production lines; this experiment adds no production
code in either repository.

Run directories below are under the persistent capacity evidence workspace at
`durable-sync-kernel/review-streaming-20260906`. Each has the prefix
`tuwunel-sliding-capacity-` before the listed UTC run identifier.

| Arm / run | Subsequent chunk chars | Initial median / p95 (s) | Completion median / p95 (s) | Matrix updates | Full overlap (s) |
| --- | ---: | ---: | ---: | ---: | ---: |
| A1 / `20260906T195844Z` | 40 | 6.127 / 12.257 | 72.271 / 78.657 | 5,084 | 51.552 |
| B1 / `20260906T200146Z` | 400 | 6.280 / 11.674 | 68.887 / 74.538 | 3,032 | 49.867 |
| B2 / `20260906T200437Z` | 400 | 5.954 / 11.353 | 68.684 / 74.021 | 3,031 | 50.201 |
| A2 / `20260906T200725Z` | 40 | 6.265 / 12.231 | 70.205 / 76.001 | 5,027 | 49.870 |

All four attempts pass every original capacity predicate and the additional
source/diagnostic checks. Each delivers exactly 200 replies, passes the actual
Sliding limited-window/restart history probe, retains zero producer input/batch,
application journal and outbox debt, and shuts down cleanly. No competing tests
or profilers ran. Source/package/helper/image comparison identity is
`272bc11002e5366898258d294ed5df8fb7a749f7a4fadf028c5bc71d1d76162d` in all four.
There were no failed live attempts in this series; earlier campaign failures
remain recorded above. The offline analyzer was corrected to interpret the
recorded `clean_stops: [true]` list before final qualification and archive creation.

Matrix updates fell about 40%, from a median of 25 to 15 per reply. Counts include
the first message: edit counts are respectively 4,884, 2,832, 2,831 and 4,827.
Provider chunk counts, including the premeasurement warm-up, were 24,169, 2,688,
2,682 and 24,148. Each arm produced 964,800 characters in total. The default
consumer already coalesces many model chunks, so a roughly ninefold reduction in
provider yields does not produce a ninefold reduction in Matrix traffic.

Initial p95 improved 0.58 seconds in A1/B1 and 0.88 seconds in B2/A2; median
startup was slightly worse in the first pair and slightly better in the second.
Completion p95 improved 4.12 and 1.98 seconds. Control completion p95 itself varied
2.66 seconds. These are descriptive paired observations, not significance or
equivalence claims. The diagnostic changes provider wakeups, downstream chunk
processing and Matrix edits together; it does not identify which part causes the
difference. Nominal model generation remains 60 seconds per reply, but fewer
synthetic sleeps also change accumulated scheduling delay. Real provider timing
may differ. Tool continuation counts were 50, 41, 38 and 28: seed and probability
were fixed, while the model hashes assembled histories that differ between runs.

**Decision:** keep current production streaming behavior. This demonstrates a
modest traffic/cadence tradeoff, with only a small startup benefit. That makes
chunk-frequency tuning a lower priority for the 200-root workload. It does not
justify adding buffering, changing defaults or doing a Synapse confirmation in
this task. No production speedup or 1,000-reply qualification is claimed. The
first chunk remains prompt in both arms, but subsequent full 400-character chunks
arrive at nominal five-second intervals instead of half-second intervals.

### Reproduce the streaming diagnostic

The persistent archive is `reproducibility/20260906-review-streaming.tar.gz`,
with a checksum companion and extracted sibling directory. It contains 25
manifested files plus the manifest (266,371 compressed bytes), SHA-256
`535f54ef93f610b865b5a31eeacf4c46d2b7c5d18399961fc1f3c8ad0f79a92b`.
It preserves all four run manifests, payload-free timestamp/update cohorts,
provider phase lengths, exact source/package/helper/image identities, frozen
diagnostic and analysis helpers, and reproduction instructions. Raw logs,
databases, message bodies, credentials and encrypted test material are excluded.

Standalone reanalysis reproduces every recorded metric and the ABBA comparison
exactly. From the extracted `streaming-frequency` directory:

```sh
sha256sum -c MANIFEST.sha256
uv run --no-project --python 3.13 python helpers/analyze.py --archive-root . --expect-abba
```

Fresh live reproduction additionally needs the unchanged Sliding companion
archive described above, with SHA-256
`cb8b04d1404a319346da0cb8b279980bf59c975a7d8fc8e987afafc2933f4618`.
The new README specifies the exact producer/consumer revisions from this series,
the consumer's locked Python 3.13 environment, immutable images, location
adaptation and sequential `--arm 40`, `400`, `400`, `40` commands. Later
documentation-only heads are not substituted for those qualified revisions.

## Room lifecycle correction follow-up

The next review reproduced six correctness defects at producer `6143e8a`.
Producer `cb7c55da1ffca2f1cdf0733968b208ffe082b397` fixes them with 30 net
production lines (+79/-49 across seven files). The complete Nio PR remains
1,250 production lines smaller than main `5b6de3bc`. The consumer exactly pins
the rebuilt wheel; all 60 installed producer source files match that commit.

Local joins now preserve known encryption and invalidate the old room projection
and Sliding checkpoint together. Unknown room state cannot authorize plaintext
sending. Completed recipient lookup permits encryption without changing the
historical room-member projection. Invitation snapshots replace former joined
members, Classic reconciliation preserves explicit bans, and truncated HTTP
bodies use the existing bounded retry policy. The producer contract documents
these ownership rules without adding a queue, database, schema or public API.

Producer verification passes 879 tests with three skipped, zero mypy errors
across 60 source files and all hooks. Sixteen new cases cover real HTTP,
peer-side decryption and restart. The consumer passes 287 affected tests and all hooks,
including membership, device authorization, message delivery, durable admission
and the existing qualification harness. Its production source is unchanged from
`3c297613fe478a3526acea9127bbaae884760326`; only the dependency pin/lock and this
evidence index change.

Reproduction instructions, red/green logs, final verification logs and installed
wheel hashes are retained in the persistent capacity workspace at
`durable-sync-kernel/review-room-lifecycle-20260906`. The new tests and ownership
contract are Git-tracked in Nio. Prior performance measurements retain their
original source IDs; this correctness follow-up does not claim a fresh capacity
qualification, performance improvement or 1,000-reply result.

## Recipient and recovery invalidation follow-up

Producer `76e4fa4bf441430227205671bb48448477d2a38c` fixes the two blockers
reported at `785c835` with five added production lines across two existing owners.
Refreshing the recipient list retires the outbound Megolm session when the set
changes or its prior value is unknown; unchanged users retain their session.
Sliding recovery that loses a joined room's baseline requests fresh initial
state after the retained interval drains. Future LIVE delivery resumes while
the lost interval remains fenced. No schema, queue or public API is added.

Producer verification: 885 passed, three skipped, zero mypy errors across 60
source files and all hooks passed. Six regression cases exercise actual HTTP,
old-key decryption rejection, another room's continued recovery and retained
output restart. The complete Nio PR remains 1,245 production lines smaller than
main `5b6de3bc` (+5,226/-6,471). MindRoom's exact pin and lock select the fixed
wheel; all 60 installed source files match that production commit.

The broader consumer check also exposed a timing-sensitive watchdog fixture:
10 ms producer sleeps competed with a 20 ms watchdog deadline under parallel
load. Its fake bot never invokes Nio. The test now controls poll ticks and the
watchdog's clock while exercising real progress/grace handling; no production
timeout or runtime behavior changes. The original failure and isolated passing
control are retained with the qualification evidence.

Final consumer verification under the same automatic worker count passes 337
tests with one skipped in 7.78 seconds; all repository hooks pass. A subprocess
mutation disabling watchdog generation observation makes the corrected test
fail, confirming that it still detects the behavior it is intended to protect.
Consumer production source is unchanged.

Reproduction commands, red/green logs and wheel hashes live in the persistent
capacity workspace at `durable-sync-kernel/review-invalidation-20260906`.
The Nio contract and implementation plan retain the ownership decisions.
Previous performance measurements retain their exact original source IDs;
this follow-up adds no fresh capacity, speed or 1,000-reply claim.

## Native review boundary corrections

Producer `a4feac63abeeaf71e9a04c0db507e96c9365e83c` contains five reproduced
boundary corrections: disposed clients reject encryption and later HTTP work,
quiescence discards completed but unaccepted polls, malformed room state cannot
preserve LIVE authority, response validation logs omit server payloads, and
Classic leave-room account data survives durable handoff and restart.
The fixes use existing owners and add 20 net production lines. The full Nio PR
remains 1,225 production lines smaller than main. No new framework or guarantee
was introduced. A test-server connection cleanup also fixes the Python 3.12 hang
without changing production retry behavior.

Nio's full suite passes 911 tests with three skipped; 104 focused parser/sync
checks also pass on Python 3.12. Mypy reports zero errors across 60 files and
producer hooks pass. MindRoom pins
this exact wheel; all 60 installed source files match it. The affected consumer
check passes 337 tests with one skipped in 7.81 seconds. Consumer production
code is unchanged; only the pin, lock and evidence index change.

The persistent evidence directory is
`durable-sync-kernel/review-invalidation-20260906/native-loop` within the capacity
workspace. It retains independent whole-PR review dispositions, main-thread
red/green tests, Python-version results, wheel hashes and consumer qualification.
Review corrections require concrete realistic failures within the existing
contract; speculative safeguards and large new abstractions are excluded.
Prior performance measurements retain their original source revisions.

### Replay and invitation parser correction

The updated pin is producer `c4b4bf8e11eaa00b3307482145d263c7afd110ef`.
It restores room context when replaying undecrypted events, so missing-key
callbacks send a valid room ID. Malformed invitation envelopes now reach the
existing durable transaction guard instead of disappearing during parsing.
Valid unknown/redacted invitation events still remain ignored. The shared
parser compatibility change is recorded in the producer changelog and contract.

These two corrections add three net production lines. The full Nio PR remains
1,222 production lines smaller than main. Producer verification passes 922 tests
with three skipped, 88 affected checks on Python 3.12, mypy and repository hooks.
All 60 installed wheel source files match the pinned revision. MindRoom's
affected integration check passes 337 tests with one skipped in 7.57 seconds;
all consumer repository hooks pass. Consumer production code is unchanged.
Reproduction, red/green results, wheel
identity and independent review dispositions remain in the indexed `native-loop`
evidence directory (`replay-invite.md` and `consumer-c4b4bf8-*` logs). Existing
performance measurements retain their original revisions.
