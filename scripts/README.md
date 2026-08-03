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
uv run python scripts/testing/fuzz_live_matrix.py --seed 42 --steps 200 --threads 45
uv run python scripts/testing/fuzz_live_matrix.py --profile restart-regression
uv run python scripts/testing/fuzz_live_matrix.py --profile saturation
```

The saturation profile uses a 180-second per-reply deadline because its slow 12-way stream workload intentionally queues much more work than normal fuzz runs.

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
