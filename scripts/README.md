# Scripts Directory

This directory contains utility scripts for MindRoom self-hosting.

## Available Scripts

### 🧪 Testing
- **`testing/benchmark_matrix_throughput.py`** - Benchmark Matrix message throughput performance

### 🔧 Utilities
- **`utilities/cleanup_agent_edits.sh`** - Clean up agent-edited files in Matrix database
- **`utilities/cleanup_agent_edits_docker.sh`** - Clean up agent edits in Docker environment
- **`utilities/cleanup_agent_edits.py`** - Python version of cleanup script with more options
- **`utilities/forward-ports.sh`** - Forward ports from remote servers for local testing
- **`utilities/generate_avatars.py`** - Generate avatar images for agents
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

### Generate agent avatars
```bash
./scripts/utilities/generate_avatars.py
```

## Requirements

- **Python 3.11+**: For Python scripts
- **UV/UVX** (optional): For automatic dependency management in Python scripts
- **Docker**: For Docker-based utilities
- **PostgreSQL client**: For database cleanup scripts
