###########
# justfile
###########

# Defaults
default:
    @just --list

default_instance := env_var_or_default("INSTANCE", "default")
default_matrix   := env_var_or_default("MATRIX", "tuwunel")

###################################
# Local: Matrix dev stack (Compose)
###################################

# Start Matrix + DB dev stack (Compose)
local-matrix-up:
    cd local/matrix && docker compose up -d

# Stop Matrix + DB dev stack
local-matrix-down:
    cd local/matrix && docker compose down

# Tail logs for Matrix + DB stack
local-matrix-logs:
    cd local/matrix && docker compose logs -f

# Reset Matrix + DB stack (remove volumes)
local-matrix-reset:
    cd local/matrix && docker compose down -v
    rm -f matrix_state.yaml
    docker volume prune -f
    rm -rf tmp/
    @echo "✅ Reset complete! Run 'just create' then 'mindroom run' to start fresh."

#########################################
# Local: Instances orchestration (Compose)
#########################################

# Create a local instance (Compose)
local-instances-create instance=default_instance matrix=default_matrix:
    #!/usr/bin/env bash
    if [ "{{matrix}}" = "none" ]; then
        cd local/instances/deploy && ./deploy.py create {{instance}}
    else
        cd local/instances/deploy && ./deploy.py create {{instance}} --matrix {{matrix}}
    fi

# Start a local instance
local-instances-start instance=default_instance:
    cd local/instances/deploy && ./deploy.py start {{instance}}

# Start a local instance (backend + Matrix only)
local-instances-start-backend instance=default_instance:
    cd local/instances/deploy && ./deploy.py start {{instance}} --no-frontend

# Stop a local instance
local-instances-stop instance=default_instance:
    cd local/instances/deploy && ./deploy.py stop {{instance}}

# Remove a local instance (containers + data)
local-instances-remove instance=default_instance:
    cd local/instances/deploy && ./deploy.py remove {{instance}} --force

# List local instances
local-instances-list:
    cd local/instances/deploy && ./deploy.py list

# Tail logs for a local instance
local-instances-logs instance=default_instance:
    cd local/instances/deploy && docker compose -p {{instance}} logs -f

# Shell into local backend container for an instance
local-instances-shell instance=default_instance:
    cd local/instances/deploy && docker compose -p {{instance}} exec backend bash

# Remove ALL local instances (containers + data)
local-instances-reset:
    cd local/instances/deploy && ./deploy.py remove --all --force

########################################
# Local: Platform dev stack (Compose)
########################################

# Start SaaS platform Compose stack (local)
local-platform-compose-up:
    cd saas-platform && docker compose up -d

# Stop SaaS platform Compose stack (local)
local-platform-compose-down:
    cd saas-platform && docker compose down

# Tail logs for SaaS platform Compose stack (local)
local-platform-compose-logs:
    cd saas-platform && docker compose logs -f

# ------------------------
# Development / CI helpers
# ------------------------

################################
# Cluster: Terraform / Helm / DB
################################

# Helm
# Render Helm manifests for platform chart (optional kubeconform validation)
cluster-helm-template:
    #!/usr/bin/env bash
    set -euo pipefail
    if command -v kubeconform >/dev/null 2>&1; then
        helm template platform ./cluster/k8s/platform -f cluster/k8s/platform/values.yaml | kubeconform -ignore-missing-schemas
    else
        echo "[warn] kubeconform not found; rendering manifests without validation" >&2
        helm template platform ./cluster/k8s/platform -f cluster/k8s/platform/values.yaml
    fi

# Lint platform chart (Helm)
cluster-helm-lint:
    helm lint ./cluster/k8s/platform

# Terraform
cluster-tf-up:
    bash cluster/terraform/terraform-k8s/scripts/up.sh

cluster-tf-build-snapshots:
    bash cluster/terraform/terraform-k8s/scripts/build_snapshots.sh

# Show Terraform outputs and cluster status
cluster-tf-status:
    bash cluster/terraform/terraform-k8s/scripts/status.sh

# Destroy platform + cluster (Terraform)
cluster-tf-destroy:
    bash cluster/terraform/terraform-k8s/scripts/destroy.sh

# Set up Terraform state symlinks (one-time setup for new clones)
cluster-tf-state-setup:
    bash cluster/scripts/setup-terraform-state.sh

# Backup Supabase database (requires env in saas-platform/.env)
cluster-db-backup:
    bash cluster/scripts/db/backup_supabase.sh

############################
# Cluster: Local kind setup #
############################

# Create kind cluster with ingress (local) via Nix shell
cluster-kind-up:
    nix-shell cluster/k8s/kind/shell.nix --run 'bash cluster/k8s/kind/up.sh'

# Build images and load into kind (avoids registry pulls) via Nix shell
cluster-kind-build-load:
    nix-shell cluster/k8s/kind/shell.nix --run 'env DOCKER_BUILDKIT=1 bash cluster/k8s/kind/build_load_images.sh'

# Install platform Helm chart into kind via Nix shell
cluster-kind-install-platform:
    nix-shell cluster/k8s/kind/shell.nix --run 'bash cluster/k8s/kind/install_platform.sh'

# Port-forward backend service (kind) via Nix shell
cluster-kind-port-backend:
    nix-shell cluster/k8s/kind/shell.nix --run 'kubectl -n mindroom-staging port-forward svc/platform-backend 8000:8000'

# Port-forward frontend service (kind) via Nix shell
cluster-kind-port-frontend:
    nix-shell cluster/k8s/kind/shell.nix --run 'kubectl -n mindroom-staging port-forward svc/platform-frontend 3000:3000'

# Tear down kind cluster via Nix shell
cluster-kind-down:
    nix-shell cluster/k8s/kind/shell.nix --run 'bash cluster/k8s/kind/down.sh'

# One-shot: fresh kind up + build+load + install
cluster-kind-fresh:
    nix-shell cluster/k8s/kind/shell.nix --run 'bash cluster/k8s/kind/start-fresh.sh'

#################
# Env helpers    #
#################

# Print exported env vars from saas-platform/.env (for eval)
env-saas:
    #!/usr/bin/env bash
    set -euo pipefail
    uvx --from python-dotenv[cli] dotenv -f saas-platform/.env list --format shell

############
# Test runs #
############

# SaaS platform backend tests
# Run SaaS platform backend tests with optional arguments
test-saas-backend *args:
    cd saas-platform/platform-backend && uv run pytest {{args}}

# Run SaaS platform frontend tests (Jest)
test-saas-frontend:
    cd saas-platform/platform-frontend && bun install && bun test

# Core frontend tests (vitest)
# Run core frontend tests (vitest)
test-front:
    cd frontend && bun install && bun test

# Core backend tests (pytest in repo)
# Run core backend tests (pytest) with optional arguments
test-backend *args:
    uv run pytest {{args}}

#############################
# Developer-friendly aliases
#############################

# Docker builds (local)
# Build core MindRoom frontend image (static, Nginx)
docker-build-frontend:
    docker build -t mindroom-frontend:dev -f local/instances/deploy/Dockerfile.frontend .

# Build core MindRoom backend image (FastAPI)
docker-build-backend:
    docker build -t mindroom-backend:dev -f local/instances/deploy/Dockerfile.backend .

# Build SaaS platform frontend (Next.js standalone)
docker-build-saas-frontend:
    #!/usr/bin/env bash
    set -euo pipefail
    # Load env from saas-platform/.env into current shell
    eval "$(uvx --from python-dotenv[cli] dotenv -f saas-platform/.env list --format shell)"
    cd saas-platform
    docker build \
        --build-arg SUPABASE_URL="${SUPABASE_URL:-}" \
        --build-arg SUPABASE_ANON_KEY="${SUPABASE_ANON_KEY:-}" \
        --build-arg PLATFORM_DOMAIN="${PLATFORM_DOMAIN:-}" \
        -t platform-frontend:dev \
        -f saas-platform/Dockerfile.platform-frontend .

# Build SaaS platform backend (FastAPI)
docker-build-saas-backend:
    docker build -t platform-backend:dev -f saas-platform/Dockerfile.platform-backend .

# Core MindRoom dev
# Start core MindRoom frontend (dev)
start-frontend-dev:
    cd frontend && bun install && bun run dev -- --host 0.0.0.0 --port 3003

# Start core MindRoom backend (dev)
start-backend-dev:
    ./run-backend.sh

# SaaS Platform app dev
# Start SaaS platform frontend (dev)
start-saas-frontend-dev:
    cd saas-platform/platform-frontend && bun install && bun dev

# Start SaaS platform backend (dev)
start-saas-backend-dev:
    cd saas-platform/platform-backend && uv run uvicorn main:app --host 0.0.0.0 --port 8000 --reload
