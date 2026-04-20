# Scripts Directory

This directory contains utility scripts for MindRoom self-hosting.

## Available Scripts

### 🧪 Testing
- **`testing/benchmark_matrix_throughput.py`** - Benchmark Matrix message throughput performance
- **`browser-login-cinny.py`** - Open a persistent Chromium profile for one-time Cinny login

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

### Seed a persistent Cinny login
Run the helper once per runtime and browser profile, log in until the Cinny room timeline is visible, then press Enter to close the headed browser.
The persistent Chromium profile lives at `<storage_root>/browser-profiles/<profile>`, so later browser-tool runs reuse the same localStorage-backed session.

```bash
uv run python scripts/browser-login-cinny.py --config-path /path/to/config.yaml --url http://localhost:8090/
```

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
