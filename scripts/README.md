# Scripts Directory Structure

This directory contains all operational scripts for the MindRoom platform, organized by function.

## Directory Structure

### 📦 `/deployment`
Scripts for deploying and managing the platform infrastructure.

- **`deploy-all.sh`** - Complete platform deployment (infrastructure + services)
- **`cleanup-all.sh`** - Tear down all infrastructure and services
- **`update-from-registry.sh`** - Update Docker images from registry

### 🗄️ `/database`
Database management and data setup scripts.

- **`run-migrations.sh`** - Apply Supabase database migrations
- **`create-admin-user.js`** - Create admin user for the platform
- **`setup-stripe-products.js`** - Configure Stripe products and pricing

### 🛠️ `/development`
Local development utilities.

- **`start`** - Start development environment with Zellij
- **`stop`** - Stop all development services
- **`forward-ports.sh`** - Forward ports from remote servers for local testing
- **`zellij-mindroom.kdl`** - Zellij configuration for development

### 🧪 `/testing`
Testing and benchmarking scripts.

- **`test_stripe.py`** - Test Stripe integration
- **`benchmark_matrix_throughput.py`** - Benchmark Matrix message throughput

### 🔧 `/utilities`
General utility scripts.

- **`cleanup_agent_edits.sh`** - Clean up agent-edited files
- **`cleanup_agent_edits_docker.sh`** - Clean up agent edits in Docker
- **`cleanup_agent_edits.py`** - Python version of cleanup script
- **`generate_avatars.py`** - Generate avatar images
- **`rewrite_git_commits_ai.py`** - Rewrite git commit messages with AI
- **`rewrite_git_history_apply.py`** - Apply git history rewrites
- **`setup_cleanup_cron.sh`** - Setup cron job for cleanup

## Common Usage Examples

### Deploy Everything
```bash
./scripts/deployment/deploy-all.sh
```

### Database Operations
```bash
./scripts/database/run-migrations.sh
./scripts/database/setup-stripe-products.js
```

### Local Development
```bash
./scripts/development/start  # Start dev environment
./scripts/development/stop   # Stop everything
```

### Testing
```bash
./scripts/testing/test_stripe.py
```

## Environment Management

All scripts that need environment variables automatically handle loading from `.env`:

1. **With `uvx` (recommended)**: Automatically uses `python-dotenv` for robust env loading
2. **Fallback**: Sources `.env` file directly when `uvx` is not available

The scripts handle this internally, so you don't need any wrapper.

## Requirements

- **UV/UVX**: For Python scripts with automatic dependency management
- **Node.js**: For JavaScript database scripts
- **Terraform**: For infrastructure deployment
- **Docker**: For container management
- **Environment Variables**: Configure in `.env` file:
  - `STRIPE_SECRET_KEY`
  - `SUPABASE_URL`
  - `SUPABASE_SERVICE_KEY`
  - `HCLOUD_TOKEN`
  - `GITEA_TOKEN`
  - And more (see `.env.example`)

## Notes

- All scripts are idempotent where possible
- Python scripts use UV's inline script dependencies
- Deployment scripts include automatic rollback on failure
- Database migrations are run via SSH tunnel for security
