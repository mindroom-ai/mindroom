# Scripts Directory

This directory contains utility scripts for MindRoom self-hosting.

## Available Scripts

### 🧪 Testing
- **`testing/benchmark_matrix_throughput.py`** - Benchmark Matrix message throughput performance
- **`testing/benchmark_tool_call_overhead.py`** - Benchmark synthetic tool-call bridge overhead
- **`testing/fuzz_live_matrix.py`** - Replay concurrent Matrix mutations through disposable Tuwunel and MindRoom stacks

### 🔧 Utilities
- **`utilities/cleanup_agent_edits.sh`** - Clean up agent-edited files in Matrix database
- **`utilities/cleanup_agent_edits_docker.sh`** - Clean up agent edits in Docker environment
- **`utilities/cleanup_agent_edits.py`** - Python version of cleanup script with more options
- **`utilities/forward-ports.sh`** - Forward ports from remote servers for local testing
- **`utilities/rewrite_git_commits_ai.py`** - Rewrite git commit messages with AI
- **`utilities/rewrite_git_history_apply.py`** - Apply git history rewrites
- **`utilities/setup_cleanup_cron.sh`** - Setup cron job for periodic cleanup

## For SaaS Platform Scripts

If you're looking for platform deployment scripts (infrastructure, database migrations, etc.), those have been moved to the `saas-platform/` directory as they are specific to the hosted service offering.

## Usage Examples

### Clean up agent edits
```bash
# For Docker setup
./scripts/utilities/cleanup_agent_edits_docker.sh

# For direct database access
./scripts/utilities/cleanup_agent_edits.py --dry-run
```

### Benchmark Matrix performance
```bash
./scripts/testing/benchmark_matrix_throughput.py
```

### Benchmark tool-call overhead
```bash
uv run python scripts/testing/benchmark_tool_call_overhead.py --iterations 1000 --warmup 100
```

### Fuzz live Matrix behavior
```bash
uv run python scripts/testing/fuzz_live_matrix.py --seed 42 --steps 200 --threads 45 --restart-interval 5
uv run python scripts/testing/fuzz_live_matrix.py --profile restart-regression
uv run python scripts/testing/fuzz_live_matrix.py --profile short-stream-correctness
uv run python scripts/testing/fuzz_live_matrix.py --profile recovery-cliff --reply-timeout 180
uv run python scripts/testing/fuzz_live_matrix.py --profile recovery-cliff --threads 200 --reply-timeout 180
```

`--restart-interval` is the only knob that decides how much recovery a fuzz run exercises, so the command above passes it explicitly.
The default of 100 buys one interruption in a 200-step run; `5` buys around forty.

Forty rather than four because one interruption is not a sample.
A crashed turn has two ways back: the journal replays it, or the homeserver re-delivers it because the sync checkpoint never advanced past it, and the second path hides a broken first one whenever it happens to fire.
Measured against a MindRoom whose cross-process turn replay was disabled: four interruptions were all rescued by re-delivery and the run reported `PASS`, while twenty-four found the hole after sixteen batches and failed with `admitted_never_dispatched`.
That is the whole argument for the interval, and the reason a run that quietly took the default is not the gate.

Each interruption is scheduled as the tail of a batch that still owes the agent a reply, and the harness waits for that batch to become durable-but-unfinished in the journal before it takes the process down.
That is what makes it land inside a turn instead of against an idle runtime, which is all a restart between drained batches could ever do.
Interruptions alternate between two kinds, because they prove different things:

- `restart_mindroom` sends SIGINT, so MindRoom drains. The run fails if the child ignores the signal until the harness has to kill it, exits with an unexpected status, or never logs an orderly bot shutdown.
- `crash_mindroom` sends SIGKILL, so nothing drains and every committed, unsettled obligation is owed to durable recovery. There is no shutdown verdict to check here; the oracle is that each interrupted turn still produces exactly one reply.

A run whose interruptions all found an idle journal fails instead of reporting the count as coverage.
`restarts`, `crashes`, and `interruptions_with_work_outstanding` are all in the result JSON, and the third must equal the sum of the first two.
`restart_drain_incomplete` counts the production `runtime_drain_incomplete_with_durable_dispatch_recovery` marker over the whole run.
It is reported rather than gated: a graceful restart taken mid-turn is allowed to hand unfinished work to durable recovery, and that the work still comes back is what the reply oracle checks.

The `short-stream-correctness` profile preserves the existing 13-thread hot-then-parallel stream scenario with a 180-second per-reply deadline.
It proves exact short streamed-reply correctness and is not a capacity benchmark or capacity result.

The `sustained-stream-capacity` profile is the ordinary no-fault capacity workload and releases N configured managed roots together under one fixed 180-second whole-workload deadline, with 200 as the default example.
Its responder emits exactly 4,800 characters in 40-character chunks at 80 characters per second, making each stream nominally 60 seconds so launch spread does not make the 45-second all-stream overlap gate impossible.

```bash
uv run python scripts/testing/fuzz_live_matrix.py --profile sustained-stream-capacity --threads 200 --reply-timeout 180
```

This profile measures sustained overlapping streams without pausing MindRoom or injecting a recovery-cliff context gap.
It does not send SIGSTOP or inject a recovery-cliff context gap, and it does not require a recovery marker.
Unlike `short-stream-correctness`, it is a capacity workload with N long-lived overlapping streams rather than a short correctness scenario.
Unlike `recovery-cliff`, it does not fault-inject a context gap or require recovery-cliff-only delivery-retry, detached-worker, or unacknowledged-FINAL evidence.

PASS requires exactly N configured root source events with unique run/thread markers, the expected sender, and an explicit mention of the configured responder.
PASS requires exactly one completed canonical terminal reply for every configured root, with unique source and response identities and no missing, duplicate, orphan, malformed, nonterminal, or later-terminal replacement evidence.
PASS requires every stream to remain active for at least 45 seconds, all N streams to overlap for at least 45 seconds, and peak active streams to reach N.
PASS requires healthy sync samples throughout the run and at least one health sample while the root release is still outstanding.
PASS requires zero pending journal rows and unacknowledged outbox rows when the durable probe is available, with no recovery-abandonment, watchdog-stall, or durable-drain-failure markers.
PASS requires the post-load reaction to settle, sync time to advance after the reaction fence, and shutdown to complete cleanly within the same non-extending deadline.

The `recovery-cliff` profile configures a managed `load_sender`, a managed synthetic `general` responder, and Sliding Sync with a timeline limit of 100 for a workload that defaults to 100 roots and can be raised with `--threads`.
With the current timeline and nio recovery limits, 1,499 roots is the largest valid recoverable trace and 1,500 is refused before the held workload rather than misreported as a runtime capacity failure.
It pauses the managed runtime at a confirmed process-group boundary, sends a derived 601-event context gap, releases every configured managed root concurrently, and resumes the runtime even if a send fails.
Its single observer cursor enumerates every positioned sync interval forward through `/messages` before publication, so a limited or compacted `/sync` timeline cannot omit reply or edit evidence.
One fixed whole-workload `--reply-timeout` covers the fault boundary, root sends, terminal replies, durable debt observation, final drain, and post-load fence without extension.
PASS requires exact completed replies, sustained overlap of every configured stream, an attempted unacknowledged workload FINAL row, the detached delivery worker marker, the generic delivery-retry marker, complete durable drain, healthy advancing sync, zero watchdog stalls, no recovery abandonment, and clean shutdown.
Short-stream-correctness results are not recovery-cliff acceptance evidence.

#### The live gate is manual, and that is a decision rather than an omission

Nothing in `.github/`, the `justfile`, or pre-commit used to run this harness, so a change to Matrix ingress, the event journal, dispatch, or shutdown could reach `main` with no live evidence behind it at all.
The gate is now a single named command:

```bash
just test-live-journal-gate
```

It runs the fuzz profile with restarts turned up and then the restart-recovery profile, and it is the check to run before merging anything that touches those paths.
`tests/test_live_matrix_fuzz.py` is what CI runs, and it is a unit test of this harness against fakes: it proves the oracle and the invariants behave, and it boots no Docker, no homeserver, and no MindRoom.

This gate is deliberately not a CI job, and the reason is wall time and latency sensitivity rather than missing infrastructure.
The harness needs nothing supplied: it starts its own Tuwunel through `just local-instances-create`, its own model stub, and its own MindRoom child, so Docker is the only requirement and `ubuntu-latest` has it.
What it needs that a shared runner does not have is quiet.
Measured here on 32 cores at load 16, a deliberately small 40-step, 6-thread, 3-interruption run took 105 seconds end to end at 4.9 seconds per agent turn; the gate above is 200 steps across 45 threads with around forty interruptions, and every interruption is a full MindRoom boot.
The harness scales its deadlines from measured turn latency and prints a contention warning precisely because that latency is what decides whether a red run means anything, so a busy four-core runner does not fail fast — it fails slowly, for reasons that have nothing to do with the code under test.
A gate that is allowed to be flaky gets muted, which leaves the same hole this section exists to close while looking like it does not.
A job that skipped itself would be worse still: it would report green on every PR without ever having run.

A scaled-down live job is not obviously impossible, and the 105-second measurement above is the argument for someone trying it.
It is not claimed here because it has not been run on a GitHub runner, and shipping an unverified green check is the exact defect this section exists to remove.

#### Making a red run mean the product is broken

Every event in one Matrix room is handled by a single sequential lane, so a batch that asks forty-five threads for a reply is asking for forty-five agent turns back to back.
Holding that to the same flat deadline as a single turn made the fuzz profile fail on a busy machine for reasons that had nothing to do with the code under test, so the harness now derives its deadlines from the work and from measured latency.

- For fuzz, restart-regression, and short-stream-correctness, `--reply-timeout` is the deadline for a *single* agent turn and the floor under every larger adaptive deadline.
- For recovery-cliff, `--reply-timeout` is one fixed non-extending deadline for the complete held-load, reply, drain, and fence workflow.
- A wait for N outstanding replies gets `N x measured-turn-latency x 3`, floored at `--reply-timeout`.
  The turn latency is measured from the warm-up exchange and from every completed wait after it, keeping the slowest observation, so nothing new is hardcoded.
- Silence, not the deadline, is what identifies a wedge.
  A wait that sees no new reply for four measured turn latencies (floored at `--reply-timeout`) fails immediately as wedged, well before the whole-batch deadline expires.
- A deadline that arrives while replies are still landing is extended up to three times, and each extension prints a `slow machine:` line to stderr.
  An extension is only granted to a window that actually produced a reply, so a wedged runtime can never extend its way out of failing.
- A managed MindRoom child that has exited fails the wait on the next poll instead of being waited out.

When a wait does fail, the harness reads the run's own `mindroom_data/tracking/event_journal.db` and reports where each missing reply's source event actually stopped: `not_admitted`, `admitted_never_dispatched`, `dispatched_never_sent`, `settled_without_reply`, or `sent_but_unobserved`.
The report also names the per-room pending depth and the event at the head of the blocked lane.

Every run prints a preflight line describing the contention it is competing with (host cores and load average, Docker CPU and memory limits, and the number of other test processes running), and repeats it at failure.
The same figures appear in the run's result JSON alongside `measured_turn_seconds` and `slow_wait_extensions`.

`--root-fanout` controls how many thread roots are released simultaneously per wave; it defaults to 8 because the single per-room lane serialises them anyway, and a smaller wave means a failure names the turn that stopped instead of reporting forty-four missing replies.
Pass `--root-fanout 0` to restore the original single simultaneous fan-out.
The `--threads` and `--max-batch-size` defaults are unchanged.

#### Restart-recovery regression profile

The `restart-regression` profile is a manual opt-in oracle for config replacement, cold-history suppression, and durable callback recovery across a hard MindRoom restart.
It creates a dormant public room, writes explicitly agent-mentioned historical text and media there, then atomically adds that room and switches only the managed agent to the replacement model used by the in-flight latch.
The disposable room is world-readable so replacement bots can actually receive and cache events authored before they joined.
The run waits for config-reload shutdown of both old bots, setup of both replacement bots, configuration-update completion, and durable caching of all four principal/event pairs.
It sends the fresh request only after that cold-history boundary, then waits for the exact callback, its unsettled durable obligation, a deterministic model request held in flight, and a sync checkpoint strictly later than the fresh event's cache write.
The harness hard-kills MindRoom, switches to a recovery-only deterministic model while the process is down, boots a new process, and waits for both recovered bots to complete setup.
The run passes only when the pending obligation becomes succeeded, the exact fresh event reaches semantic ingress once before and once after restart, and the recovered generation produces exactly one complete agent response and no router response.
Transport callback entry may repeat while Matrix sync and durable recovery race, so the oracle counts the `Received message` boundary after durable dedup instead of the lower-level callback-entry log.
Neither historical event may start a callback, reach the fresh prompt, or produce output.
An orderly final shutdown must complete without the production durable-recovery drain-failure marker.

The profile requires Docker, `just`, `uv`, available local ports, permission to create and remove an isolated Tuwunel instance, and permission for `uv` to provision the managed MindRoom child on Python 3.13.
It starts its own deterministic model stub and disposable Matrix stack, so no external model credential is required.

```bash
uv run python scripts/testing/fuzz_live_matrix.py \
  --profile restart-regression \
  --reply-timeout 60 \
  --settle-seconds 0.75 \
  --failure-log restart-regression.log
```

`--reply-timeout` bounds lifecycle, cache, durable-obligation, model-latch, response, and final-drain observation.
`--settle-seconds` controls the final Matrix long-poll after the orderly drain.
`--failure-log` preserves the complete MindRoom log when the oracle fails without printing content-bearing runtime output to the terminal.
`--seed`, `--steps`, `--threads`, `--max-batch-size`, and `--restart-interval` do not change this fixed profile.
Failures report content-free invariant coordinates, while the optional failure log contains the raw diagnostics needed for local investigation.

### Generate and sync managed avatars
Run MindRoom at least once before syncing so the router account exists in Matrix state.
When you run this from a source checkout, generated files are written under `./avatars/`.
In containerized deployments, generated overrides are stored under the persistent MindRoom storage path instead of the image-bundled `/app/avatars`.

```bash
GOOGLE_API_KEY=your-google-api-key uv run mindroom avatars generate
uv run mindroom avatars sync
```

## Requirements

- **Python 3.12+**: For Python scripts
- **UV/UVX** (optional): For automatic dependency management in Python scripts
- **Docker**: For Docker-based utilities
- **PostgreSQL client**: For database cleanup scripts
