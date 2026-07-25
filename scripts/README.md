# Scripts Directory

This directory contains utility scripts for MindRoom self-hosting.

## Available Scripts

### 🧪 Testing
- **`testing/benchmark_matrix_throughput.py`** - Benchmark Matrix message throughput performance
- **`testing/benchmark_tool_call_overhead.py`** - Benchmark synthetic tool-call bridge overhead
- **`testing/fuzz_matrix_event_cache.py`** - Replay deterministic randomized mutations directly against both cache backends
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

### Fuzz Matrix cache behavior
```bash
uv run python scripts/testing/fuzz_matrix_event_cache.py --seed 42 --steps 500
uv run python scripts/testing/fuzz_live_matrix.py --nio-overlay ../mindroom-nio --seed 42 --steps 200 --threads 45
uv run python scripts/testing/fuzz_live_matrix.py --nio-overlay ../mindroom-nio --profile saturation
uv run python scripts/testing/fuzz_live_matrix.py --nio-overlay ../mindroom-nio --profile chaos --seed 42 --steps 200 --clients 4 --rooms 2
uv run python scripts/testing/fuzz_live_matrix.py --nio-overlay ../mindroom-nio --profile stress
uv run python scripts/testing/fuzz_live_matrix.py --nio-overlay ../mindroom-nio --profile stress --api-port 18765
uv run python scripts/testing/fuzz_live_matrix.py --nio-overlay ../mindroom-nio --profile stress --api-port 18765 --state-root ~/.mindroom/live-fuzz-stress
uv run python scripts/testing/fuzz_live_matrix.py --nio-overlay ../mindroom-nio --profile stress --threads 10 --stream-seconds 10 --waves 1
uv run python scripts/testing/fuzz_live_matrix.py --nio-overlay ../mindroom-nio --mindroom-runtime ../mindroom-main --profile stress --write-baseline artifacts/live-matrix-stress/main-baseline.json
uv run python scripts/testing/fuzz_live_matrix.py --nio-overlay ../mindroom-nio --profile stress --fault-mode serialize-streams
```

The live harness requires `--nio-overlay` to name a clean exact Git checkout of mindroom-nio.
It rejects missing or dirty overlays, revision mismatches, and a child process that imports nio from another checkout.
The default durable state root serializes live stacks across worktrees.
Use a separate `--state-root` only when every port and disposable resource is isolated from another active campaign.

The saturation profile uses a 180-second per-reply deadline because its slow 12-way stream workload intentionally queues much more work than normal fuzz runs.

The chaos profile runs sustained multi-sender multi-room load that only settles at generated checkpoints, mixing hot-thread floods, in-flight edits and redactions, MindRoom warm/kill/cold restarts, Tuwunel restarts, and full outage windows with recovery gaps.
Every failure persists the exact logical workload as `scenario.json` in the failure bundle and prints its path for replay with `--trace`.
Replay preserves operation batches and inputs, but external scheduling and runtime output can differ.

The stress profile uses ten rooms, one agent, 100 independent threads, the built-in seeded `synthetic` model, 45-second maximum streams, 0.5-second chunks, real seeded `sleep` tool calls, two waves, and a disposable PostgreSQL event cache by default.
It disables automatic thread summaries so background summary reads cannot cross cache-wave boundaries.
It clears only namespace data after deterministic history preparation so the first wave proves cold scans and the second wave proves warm reuse without replacing cache-certification metadata.
Between waves, it sends a harmless reaction and waits for that exact event in the agent's principal-scoped PostgreSQL cache so the warm wave cannot overtake pending sync of the cold wave's real Matrix edits.
Every stress run retains sanitized success or failure evidence under `artifacts/live-matrix-stress/`.
Use `--write-baseline scripts/testing/baselines/live-matrix-stress.json` for three identical clean main runs before enabling `--baseline` and `--enforce-performance`.
The first two baseline runs retain a sample collection, and the third run writes the versioned median-of-three baseline only when dispersion is within the configured 25 percent allowance.
Use `--mindroom-runtime` to drive a separate clean exact MindRoom checkout with the current harness, which lets three exact-main runs establish a same-machine baseline before the candidate run.
Use `--fault-mode serialize-streams` without baseline flags to prove the synchronized concurrency gate rejects deliberately serialized model streams.
Warm-wave invalidation scans remain reported metrics rather than an accepted threshold until cache self-healing lands, while multiple repair scans for one thread in one wave always fail.
The external API-health probe has an absolute five-second ceiling matching MindRoom's native event-loop stall detector.
The sync-age probe has an absolute 120-second ceiling matching the production sync-watchdog restart threshold.

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
