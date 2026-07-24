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
uv run python scripts/testing/fuzz_matrix_event_cache.py --seed 42 --steps 500 --verify-reference-model --save-trace cache-trace.json
uv run python scripts/testing/fuzz_matrix_event_cache.py --trace cache-trace.json
uv run python scripts/testing/fuzz_live_matrix.py --seed 42 --steps 200 --threads 45
MINDROOM_NIO_FUZZ_COMMIT=REPLACE_WITH_FULL_SHA uv run python scripts/testing/fuzz_live_matrix.py --profile recovery --seed 42 --failure-log live-recovery-failure.json
MINDROOM_NIO_FUZZ_COMMIT=REPLACE_WITH_FULL_SHA uv run python scripts/testing/fuzz_live_matrix.py --trace live-recovery-failure.json --failure-log live-recovery-replay-failure.json
uv run python scripts/testing/fuzz_live_matrix.py --profile saturation
```

Cache traces preserve workload operations and concurrent batch boundaries, while reference-model mode uses sequential batches so every semantic transition has a deterministic independent oracle.
Live failure artifacts embed the replayable scenario, seed, loaded MindRoom and mindroom-nio paths and revisions, nio version and source hash, diagnostics, and runtime output.
Setting `MINDROOM_NIO_FUZZ_COMMIT` enables the merge-gate mode, which rejects dirty, unverifiable, or different loaded nio source.
The saturation profile uses a 180-second per-reply deadline because its slow 12-way stream workload intentionally queues much more work than normal fuzz runs.

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
