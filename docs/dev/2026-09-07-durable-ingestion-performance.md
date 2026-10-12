# Durable ingestion performance evidence

This record indexes the September 2026 producer/consumer performance investigation of durable Matrix ingestion: its decisions, measurements, retained artifacts and reproduction steps.
Use it with the [ledger ownership and measured results](2026-09-07-ledger-write-ownership.md) and the producer's `docs/design/durable-sync.md` and `docs/design/durable-sync-plan.md` in `mindroom-nio`, which define the retained guarantees and supersede the retired ingestion-engine plans.
The benchmark is 200 concurrent conversations with one responder account, three syncing principals and a synthetic model; it does not qualify 200 independent agents or 1,000 concurrent replies.
Reply completion includes roughly 60 seconds of synthetic generation, so an engine or startup speedup does not translate proportionally into total response time.

## Decisions

| Question | Result and decision | Evidence to read |
| --- | --- | --- |
| Was the old producer doing excessive work? | Yes; shared interpretation and durable batches replace the old engine. The final 200-event plain FULL capture/prepare/read/ack cycle is 23.2 ms versus 196.7 ms for the old NORMAL implementation. This is a local engine benchmark, not reply latency. | Producer plan, final engine table; `durable-sync-kernel/micro-final-20260906T091913Z`. |
| Do we need orjson or PR #57's implementation? | Retain stdlib JSON. Batching already reduces 200-event plain decoding from 1,854 to 3 and commits from 404 to 3; the old-engine patch is not used. | Producer plan, measured engine results. |
| Were the earlier failures just a slow server? | No; the old capacity evidence exposed real recovery/fence defects, which the producer replacement and coordinated consumer fixes address with separate correctness tests and acceptance evidence. | Producer contract/plan; original `profiling-report.md` is historical evidence. |
| Did removing global ledger serialization help? | Yes; alternating Tuwunel controls reduce initial spread from 21.69–21.81 s to 12.03–12.07 s. Keep identity-based conflict ownership. | Ledger ownership document; `durable-sync-kernel/startup-trace`. |
| Should preparation concurrency increase? | No; the measured preparation wait is predominantly pending-turn persistence, and larger limits did not fix it. Keep eight slots. | Producer plan and ledger dependency-tracing sections. |
| Do smaller worker pools or a shorter GIL switch interval help? | Neither demonstrates a startup gain; retain existing settings. | `durable-sync-kernel/startup-waits`. |
| Does one fewer membership SELECT help? | No worthwhile end-to-end gain in two alternating comparisons; the nine-line candidate was withdrawn. | Ledger membership-query section; `durable-sync-kernel/query-reuse`. |
| Why is the writer occupied without much CPU use? | Live SQLite execution contends for Python execution and then waits for completion propagation through the event loop. Same-SQL isolated replay is much faster, with important limits. | Ledger SQL/native-wait sections; `durable-sync-kernel/transaction-path`. |
| Should SQLite writes run directly on the event loop? | No useful gain in the diagnostic, and real database contention blocks the loop. Keep the offloaded writer. | Ledger direct-writer section; transaction-path lock probe and inline run. |
| Do fewer delivery transactions improve startup? | Neither tested candidate establishes a repeatable startup-tail improvement; both are withdrawn. | [Ledger trial outcome](2026-09-07-ledger-write-ownership.md#measured-outcome-withdraw-both-candidates); tracked JSON `delivery_transaction_trial`. |
| Does Sliding Sync reply faster than Classic? | No clear advantage; keep Classic as the default and Sliding for its room-discovery features. | [Sliding restoration](#sliding-restoration-matched-200-reply-controls). |
| Do less frequent streaming edits speed startup? | Only a modest traffic/cadence tradeoff; keep production streaming behavior. | [Streaming-frequency diagnostic](#streaming-frequency-diagnostic). |

The writer investigation shipped no production code.
Its measurements explain costs; they do not establish a general speedup, justify weaker durability, or prove all possible failures impossible.

## Writer investigation artifacts

All paths below are relative to the persistent capacity evidence workspace originally supplied with `profiling-report.md`; none of it is hosted in this Git repository.
The searchable archive is `reproducibility/20260906-writer-investigation.tar.gz`, with an extracted sibling directory of the same name.
It contains the campaign's available scripts, reports and result manifests, dependency locks, runtime versions, server-image identities observed at preservation time, the latest four runs' metadata traces, the exact historical native libraries, the earlier trace-child variant reconstructed and verified against its recorded SHA-256, and native symbol tables for portable re-analysis.
`MANIFEST.sha256` checks every packaged file; the companion archive checksum verifies the package itself.
Raw application logs, synthetic database files and server state remain in their original evidence directories; the packaged metadata analysis does not need them.
The tracked [measurement summary](2026-09-07-durable-ingestion-performance-results.json) preserves the key numeric results, source revisions and run mappings independently of the archive.

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

The package README gives the complete commands and path configuration; verify its checksums before analysis.
From the extracted package, with Python 3.13:

```sh
sha256sum -c MANIFEST.sha256
uv run --no-project --python 3.13 python analyze-retained.py mindroom-live-matrix-fuzz-bc2989a3
```

The portable wrapper uses the archived symbol tables, so it does not depend on the original host's absolute interpreter and library paths, and reproduces the SQL/native/future timing report from retained metadata.
SQL parameters and fetched results existed only in the diagnostic child's memory and were discarded at exit.
The exact SQL replay therefore cannot be rerun from database snapshots alone; reproduce it by running the synthetic workload again with the SQL probe installed.

For a fresh run, restore MindRoom `e276982fdbd3f58b8d471376c417767654819da4` and Nio `ce18a1fed0a592b93b789de4480ac3e61e8c3145` in persistent checkouts, install the locked consumer environment and verify the installed producer source against the Nio checkout.
Use the package's complete helper hierarchy and update the documented checkout and evidence path constants when relocating it.
Run the normal control first, then the diagnostic, sequentially on the same filesystem without competing tests or benchmarks.
The controls retain FULL durability, eight preparations, 200 roots, the shared 180-second deadline, 45-second overlap, two-second health timeout, three-principal fence, debt audit and clean shutdown.
Verify source hashes before and after each run; label runtime probes diagnostic even when the production source hashes match.
Never change runtime source or probe files during a run, compare profiled latency directly with normal latency as a speedup, or add overlapping task, native and CPU timings together.

The original launcher identity hashed the extracted control function rather than every helper, and did not record the native compiler invocation or immutable server image identity per run.
The package preserves the full available helper files, exact native binaries and a rebuild recipe, but these missing historical facts cannot be recreated as contemporaneous proof.
New runs should capture complete helper hashes and resolved image IDs at launch.
The package README maps the exact native/source probe variants used by the two valid traces.

## Delivery-transaction trial artifacts

The trial's results and decision are in the [ledger record](2026-09-07-ledger-write-ownership.md#measured-outcome-withdraw-both-candidates); its earlier normal control is in the writer-investigation table above.

| Run | Driver directory under `durable-sync-kernel` | Evidence directory |
| --- | --- | --- |
| Fresh-claim candidate | `startup-waits/control-200-20260906T162827Z` | `mindroom-live-matrix-fuzz-ba6c5689` |
| Combined candidate | `startup-waits/control-200-20260906T163429Z` | `mindroom-live-matrix-fuzz-d1624107` |
| Fresh baseline | `query-reuse/control-200-20260906T163755Z` | `mindroom-live-matrix-fuzz-22048dac` |

The persistent `durable-sync-kernel/delivery-transactions` directory preserves the trial logs, comparison script and output; original synthetic databases and application logs remain in each run's evidence directory for cohort re-analysis.
The separate addendum `reproducibility/20260906-delivery-transactions.tar.gz` freezes the trial results, available helper files and verification logs with a `MANIFEST.sha256` and companion archive checksum, leaving the writer-investigation archive unchanged.
It contains 120 files (1,034,820 compressed bytes), SHA-256 `93974903f48928b9cafd855af9be18071910c56f6ff79a2a40afa4d533031ae4`.
Its manifest verifies, and standalone timestamp re-analysis exactly reproduces all six timing metrics for every run.
Use the addendum README for reproduction commands; the source-verification and historical helper/image-identity limitations above still apply.

## Sliding restoration: matched 200-reply controls

Producer `f7e1dfd78812c8c58d846bc8e62df698903c18bf` and consumer `d820bc3a40b6661ff709b3db09c253f29defea1e` pass the same 200-reply capacity profile in both transports on Tuwunel and Synapse.
Each run uses the locked Python 3.13 consumer environment and the exact rebuilt producer wheel, and verifies clean source, installed and loaded package hashes, helper hashes and immutable Docker image IDs before and after the run.
Controls run sequentially without competing test processes; Synapse retains the earlier 5,000-event cap.
All four pass exact 200-root/200-reply delivery, three-principal fences and source advancement, the 180-second measured deadline, at least 45 seconds of full overlap, the two-second health timeout, zero producer/application/outbox debt and clean stop.

| Server | Transport | Initial median / p95 (s) | Completion median / p95 (s) | Initial spread (s) | Full overlap (s) |
| --- | --- | --- | --- | --- | --- |
| Tuwunel | Classic | 5.862 / 11.928 | 69.898 / 76.008 | 11.669 | 50.538 |
| Tuwunel | Sliding | 6.172 / 11.974 | 70.088 / 76.017 | 11.817 | 50.134 |
| Synapse | Classic | 5.845 / 11.072 | 75.343 / 79.175 | 12.413 | 55.759 |
| Synapse | Sliding | 5.556 / 10.915 | 74.808 / 78.859 | 12.006 | 56.155 |

These single-run comparisons show no clear reply-speed advantage for Sliding.
Small differences change direction between servers; they establish neither a repeatable speedup nor statistical equivalence.
Keep Sliding for room discovery, explicit subscriptions and bounded windows, with similar observed performance; Classic remains the default.
No serializer, SQLite durability, worker count or preparation limit changed for this restoration.

The run directories under `durable-sync-kernel/sliding-restoration` are `tuwunel-classic-capacity-20260906T183405Z`, `tuwunel-sliding-capacity-20260906T182731Z`, `synapse5000-classic-capacity-20260906T183647Z` and `synapse5000-sliding-capacity-20260906T183045Z`.
Both Sliding runs also pass the real-server gap probe: 20 downtime messages and a 20-message limited live window recovered exactly, with no loss or duplicates, ten actual history pages and no stored debt.
Encrypted late-key and process-kill behavior is covered by producer regression tests, not by this probe.

Live qualification found a real backward-expanded-window bug: old membership context could rewind the joined tenure and demote a fresh request to history.
The producer's `docs/design/sliding-sync-restoration.md` owns the fix's policy and tests.

Earlier failed runs remain in the tracked JSON and retained evidence:

- `tuwunel-sliding-capacity-20260906T174737Z` failed its two-second health read timeout during the workload, completing zero replies before abort and leaving 331 application rows pending; its cause was not established, and passing later runs do not explain it.
- `tuwunel-classic-capacity-20260906T175135Z` timed out before the workload because the harness sent its warm-up before the responder's committed room baseline; the harness fix (`4ef0f6734`) waits outside the measured deadline and leaves the workload criteria unchanged.
- `tuwunel-sliding-capacity-20260906T180630Z` exposed the expanded-window bug after that harness correction; `20260906T181030Z` is its deliberately short startup diagnostic, not a capacity test, and both sent zero workload roots.
- `tuwunel-sliding-capacity-20260906T175756Z` completed all 200 replies while sampled, but its profiler wrapper failed to confirm clean shutdown, so it is not a qualified control.
  PySpy observed 3,929 main-thread wall samples at 25 Hz, 612 in SQLite commit, including 335 during acknowledgement; these cover setup and workload, are not CPU time or an additive phase budget, and do not explain the earlier unprofiled health timeout.

The pre-fix passing Synapse Sliding run and the corrected Classic Tuwunel run are also retained with their original source revisions; they do not substitute for the final matched controls.

### Application restart qualification

The final Sliding restart profile passes on both servers with producer `f7e1dfd` and consumer `9883dba3a`: Tuwunel `20260906T184907Z` and Synapse `20260906T184922Z`, under the same `sliding-restoration` directory with server, transport and `restart-regression` prefixes.
It covers bot replacement, a fresh request interrupted during model execution by a hard process restart, recovered output, historical text and media staying non-actionable, on-demand history hydration and empty retained producer/application/outbox work at clean shutdown; both runs pass with zero historical outputs and zero drain failures.

Three earlier restart runs failed on stale harness assumptions, not production defects.
`20260906T183941Z` (Tuwunel) and `20260906T184117Z` (Synapse) timed out waiting for entity replacements that the planner correctly did not request, because the room/model configuration update applies in place.
`20260906T184417Z` (Tuwunel) waited for the retired Classic checkpoint file.
The harness corrections (`04bc7105d`, `9883dba3a`) changed no production source, and the four matched capacity controls retain their original committed source and harness hashes.
The corrected restart check reads exact event projection together with the producer's committed cursor and empty input and batch tables; because a completion batch replaces producer input atomically, that proves acknowledgement without an intermediate false-ready state and without requiring an idle server to change an opaque position.

### Reproduce the Sliding comparison

The addendum archive is `reproducibility/20260906-sliding-restoration.tar.gz` in the persistent capacity evidence workspace, with an extracted sibling directory and `.tar.gz.sha256` companion.
It contains 94 manifested files (3,916,060 compressed bytes), SHA-256 `cb8b04d1404a319346da0cb8b279980bf59c975a7d8fc8e987afafc2933f4618`.
It freezes the driver hierarchy, all 16 run/source/result manifests, historical harness variants, final source and lock snapshots, verification logs, metadata diagnostics and payload-free timestamp cohorts; application logs, databases, credentials and message bodies are excluded.
All manifest checks pass, and standalone timestamp re-analysis reproduces all seven complete 200-reply cohorts exactly, including the explicitly unqualified sampled run.
From the extracted directory:

```sh
sha256sum -c MANIFEST.sha256
uv run --no-project --python 3.13 python capacity/durable-sync-kernel/sliding-restoration/reanalyze.py
```

The archive README gives the fresh-run commands, required committed checkouts, locked environment, location configuration and immutable image checks.
Run `run_qualification.py` with `--profile capacity --sync-mode classic` or `--sync-mode sliding`, selecting `--server tuwunel` or `--server synapse5000`.
Use `--gap-probe` for Sliding history probes and `--profile restart-regression` for application restart checks.
Keep runtime source frozen during each run.

## Streaming-frequency diagnostic

This bounded experiment tests whether servicing existing streams delays new replies.
It keeps the 200-root Sliding workload on Tuwunel: 4,800 response characters at 80 characters per second, deterministic model and tool behavior, FULL durability, eight preparations, the 180-second deadline, 45-second full overlap, two-second health timeout, exact delivery, three-principal fences and empty stores at clean shutdown.
It compares subsequent chunks of 40 versus 400 characters in ABBA order.
The first chunk of each model phase stays at 40 characters in both arms because the synthetic provider sleeps before yielding, so changing it would confound startup overhead with a different first-token delay.
Each phase still emits the same text and sleeps for the same nominal total duration.
This is a declared diagnostic override in a source-verified child, not a production configuration or a change to the capacity control.
The decision rule, set before the runs: confirm on Synapse only if a consistent benefit warrants it, and a small or mixed result does not justify more code, another serializer or another database.

The four sequential runs use producer `ba75e3a04bb5e333b40617027723e7982d4de09a` and consumer `2fb726841f7f4c3526b1610ac91eea6afa9fedc1` with the exact rebuilt wheel; the experiment adds no production code in either repository.
Run directories are under `durable-sync-kernel/review-streaming-20260906` in the persistent capacity evidence workspace, each prefixed `tuwunel-sliding-capacity-`.

| Arm / run | Subsequent chunk chars | Initial median / p95 (s) | Completion median / p95 (s) | Matrix updates | Full overlap (s) |
| --- | ---: | ---: | ---: | ---: | ---: |
| A1 / `20260906T195844Z` | 40 | 6.127 / 12.257 | 72.271 / 78.657 | 5,084 | 51.552 |
| B1 / `20260906T200146Z` | 400 | 6.280 / 11.674 | 68.887 / 74.538 | 3,032 | 49.867 |
| B2 / `20260906T200437Z` | 400 | 5.954 / 11.353 | 68.684 / 74.021 | 3,031 | 50.201 |
| A2 / `20260906T200725Z` | 40 | 6.265 / 12.231 | 70.205 / 76.001 | 5,027 | 49.870 |

All four runs pass every capacity predicate and the source/diagnostic checks, including exactly 200 replies, the Sliding limited-window/restart history probe, zero producer, journal and outbox debt, and clean shutdown, with no competing tests or profilers and no failed attempts.
Their source/package/helper/image comparison identity is `272bc11002e5366898258d294ed5df8fb7a749f7a4fadf028c5bc71d1d76162d` in all four.

Matrix updates fell about 40%, from a median of 25 to 15 per reply; counts include the first message, and edit counts are 4,884, 2,832, 2,831 and 4,827.
Provider chunk counts, including the premeasurement warm-up, were 24,169, 2,688, 2,682 and 24,148, with 964,800 characters per arm.
The consumer already coalesces many model chunks, so a roughly ninefold reduction in provider yields does not produce a ninefold reduction in Matrix traffic.
Initial p95 improved 0.58 seconds in A1/B1 and 0.88 seconds in B2/A2; median startup was slightly worse in the first pair and slightly better in the second.
Completion p95 improved 4.12 and 1.98 seconds, while control completion p95 itself varied 2.66 seconds.
These are descriptive paired observations, not significance or equivalence claims.
The diagnostic changes provider wakeups, downstream chunk processing and Matrix edits together, so it does not identify which part causes the difference.
Fewer synthetic sleeps also change accumulated scheduling delay, and real provider timing may differ.
Tool continuation counts were 50, 41, 38 and 28: seed and probability were fixed, but the model hashes assembled histories that differ between runs.

**Decision:** keep production streaming behavior.
The result is a modest traffic/cadence tradeoff with only a small startup benefit, which makes chunk-frequency tuning a low priority for this workload and does not justify buffering, changed defaults or a Synapse confirmation.
With 400-character chunks the first chunk stays prompt, but later chunks arrive at nominal five-second instead of half-second intervals.
No production speedup is claimed; the load curve and 1,000-reply qualification remain separate open questions.

### Reproduce the streaming diagnostic

The persistent archive is `reproducibility/20260906-review-streaming.tar.gz`, with a checksum companion and extracted sibling directory.
It contains 25 manifested files plus the manifest (266,371 compressed bytes), SHA-256 `535f54ef93f610b865b5a31eeacf4c46d2b7c5d18399961fc1f3c8ad0f79a92b`.
It preserves all four run manifests, payload-free timestamp and update cohorts, provider phase lengths, exact source/package/helper/image identities, frozen diagnostic and analysis helpers, and reproduction instructions; raw logs, databases, message bodies, credentials and encrypted test material are excluded.
Standalone re-analysis reproduces every recorded metric and the ABBA comparison exactly.
From the extracted `streaming-frequency` directory:

```sh
sha256sum -c MANIFEST.sha256
uv run --no-project --python 3.13 python helpers/analyze.py --archive-root . --expect-abba
```

Fresh live reproduction also needs the unchanged Sliding archive above (SHA-256 `cb8b04d1404a319346da0cb8b279980bf59c975a7d8fc8e987afafc2933f4618`).
The streaming archive's README specifies this series' exact producer and consumer revisions, the consumer's locked Python 3.13 environment, immutable images, location adaptation and the sequential `--arm 40`, `400`, `400`, `40` commands.
Do not substitute later documentation-only heads for those qualified revisions.

## Producer correctness follow-ups

Later review rounds fixed reproduced producer correctness defects at producer `cb7c55da1ffca2f1cdf0733968b208ffe082b397` (room lifecycle), `76e4fa4bf441430227205671bb48448477d2a38c` (recipient and recovery invalidation), `a4feac63abeeaf71e9a04c0db507e96c9365e83c` (native review boundaries) and `c4b4bf8e11eaa00b3307482145d263c7afd110ef` (replay and invitation parsing); the Nio contract and changelog own the resulting rules.
Those rounds accepted only concrete realistic failures within the existing contract, excluding speculative safeguards and large new abstractions.
They were verified with correctness tests only and changed no consumer production code: they make no fresh capacity, performance or 1,000-reply claim, and every performance measurement above keeps its original source revision.
Red/green logs, wheel hashes, Python-version results and review dispositions are in `durable-sync-kernel/review-room-lifecycle-20260906` and `durable-sync-kernel/review-invalidation-20260906` (including `native-loop`, with `replay-invite.md` and `consumer-c4b4bf8-*` logs) in the capacity workspace.
