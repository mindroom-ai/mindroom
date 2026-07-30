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

#### Config-replacement regression profile

The `restart-regression` profile is a manual opt-in oracle for the agent and router replacement caused by a real `config.yaml` hot reload.
It creates a dormant public room, writes historical text and media there, adds that room to the managed agent configuration, waits for both replacement principals and the completed configuration update, then sends one fresh request.
The run sends the fresh request only after the replacement setup boundary completes.
It then uses the runtime's orderly callback and response drain as a quiescence boundary before the final Matrix audit.
The run passes only after both principals cache both historical events, the fresh request completes exactly once, no historical event reaches the fresh prompt, no historical reply appears, and the quiescence drain completes without bounded cancellation.

The profile requires Docker, `just`, `uv`, Python 3.13, available local ports, and permission to create and remove an isolated Tuwunel instance.
It starts its own deterministic model stub and disposable Matrix stack, so no external model credential is required.

```bash
uv run python scripts/testing/fuzz_live_matrix.py \
  --profile restart-regression \
  --reply-timeout 60 \
  --settle-seconds 0.75 \
  --failure-log restart-regression.log
```

`--reply-timeout` bounds lifecycle, cache, prompt, response, and quiescence observation.
`--settle-seconds` controls the final Matrix long-poll after the quiescence drain.
`--failure-log` preserves the complete MindRoom log when the oracle fails without printing content-bearing runtime output to the terminal.
`--save-trace` writes the fixed profile trace, and `--trace` loads that JSON through the normal validated replay path.
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
