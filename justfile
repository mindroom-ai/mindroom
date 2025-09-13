# justfile for mindroom - Federation deployment
# Improved replacement for the original Makefile

# Default configuration
default_instance := env_var_or_default("INSTANCE", "default")
default_matrix := env_var_or_default("MATRIX", "tuwunel")

# Colors for output (using just's escape syntax)
_red := '\e[31m'
_green := '\e[32m'
_yellow := '\e[33m'
_blue := '\e[34m'
_magenta := '\e[35m'
_cyan := '\e[36m'
_reset := '\e[0m'

# Default recipe - show help
default:
    @just --list

# Show comprehensive help with examples
help:
    #!/usr/bin/env bash
    printf "{{_cyan}}mindroom - Federation commands:{{_reset}}\n"
    printf "{{_cyan}}-------------------------------{{_reset}}\n"
    echo
    printf "{{_yellow}}Instance Management:{{_reset}}\n"
    printf "  {{_green}}create{{_reset}}        Create new instance (parameters: instance, matrix)\n"
    printf "  {{_green}}start{{_reset}}         Start instance with all services\n"
    printf "  {{_green}}start-backend{{_reset}} Start backend + Matrix only, no frontend\n"
    printf "  {{_green}}stop{{_reset}}          Stop instance\n"
    printf "  {{_green}}list{{_reset}}          List all instances\n"
    echo
    printf "{{_yellow}}Data Management:{{_reset}}\n"
    printf "  {{_green}}clean{{_reset}}         Clean instance data\n"
    printf "  {{_green}}reset{{_reset}}         Full reset: remove all instances and data\n"
    echo
    printf "{{_yellow}}Development Tools:{{_reset}}\n"
    printf "  {{_green}}logs{{_reset}}          View logs for instance\n"
    printf "  {{_green}}shell{{_reset}}         Shell into backend container\n"
    printf "  {{_green}}status{{_reset}}        Show instance status\n"
    echo
    printf "{{_yellow}}Parameters:{{_reset}}\n"
    printf "  {{_blue}}instance{{_reset}}      Instance name (default: {{default_instance}})\n"
    printf "  {{_blue}}matrix{{_reset}}        Matrix backend: tuwunel|synapse|none (default: {{default_matrix}})\n"
    echo
    printf "{{_yellow}}Examples:{{_reset}}\n"
    printf "  {{_cyan}}just create{{_reset}}                           # Create default instance with Tuwunel\n"
    printf "  {{_cyan}}just create prod synapse{{_reset}}              # Create prod instance with Synapse\n"
    printf "  {{_cyan}}just create test none{{_reset}}                 # Create test instance without Matrix\n"
    printf "  {{_cyan}}just start prod{{_reset}}                       # Start prod instance\n"
    printf "  {{_cyan}}just logs prod{{_reset}}                        # View prod logs\n"
    printf "  {{_cyan}}just shell prod{{_reset}}                       # Shell into prod backend\n"

# Validate instance name
_validate-instance instance:
    #!/usr/bin/env bash
    if [[ ! "{{instance}}" =~ ^[a-zA-Z0-9_-]+$ ]]; then
        printf "{{_red}}❌ Error: Invalid instance name '{{instance}}'{{_reset}}\n"
        printf "{{_yellow}}Instance names must contain only alphanumeric characters, hyphens, and underscores{{_reset}}\n"
        exit 1
    fi

# Validate matrix backend type
_validate-matrix matrix:
    #!/usr/bin/env bash
    if [[ "{{matrix}}" != "tuwunel" && "{{matrix}}" != "synapse" && "{{matrix}}" != "none" ]]; then
        printf "{{_red}}❌ Error: Invalid matrix backend '{{matrix}}'{{_reset}}\n"
        printf "{{_yellow}}Valid options: tuwunel, synapse, none{{_reset}}\n"
        exit 1
    fi

# Check if deploy.py exists and is executable
_check-deploy-script:
    #!/usr/bin/env bash
    if [[ ! -f "deploy/deploy.py" ]]; then
        printf "{{_red}}❌ Error: deploy/deploy.py not found{{_reset}}\n"
        printf "{{_yellow}}Make sure you're running this from the project root{{_reset}}\n"
        exit 1
    fi
    if [[ ! -x "deploy/deploy.py" ]]; then
        printf "{{_yellow}}⚠️  Making deploy/deploy.py executable...{{_reset}}\n"
        chmod +x deploy/deploy.py
    fi

# Execute deploy.py with proper error handling
_deploy-cmd +args:
    #!/usr/bin/env bash
    cd deploy || exit 1
    if ! ./deploy.py {{args}}; then
        printf "{{_red}}❌ Deploy command failed{{_reset}}\n"
        exit 1
    fi

# ═══════════════════════════════════════════════════════════════════════════════
# Instance Management Commands
# ═══════════════════════════════════════════════════════════════════════════════

# Create new instance with specified Matrix backend
create instance=default_instance matrix=default_matrix: (_validate-instance instance) (_validate-matrix matrix) _check-deploy-script
    #!/usr/bin/env bash
    printf "{{_cyan}}🚀 Creating instance '{{instance}}' with matrix backend '{{matrix}}'...{{_reset}}\n"

    cd deploy || exit 1
    if [[ "{{matrix}}" == "none" ]]; then
        if ./deploy.py create "{{instance}}"; then
            printf "{{_green}}✅ Instance '{{instance}}' created successfully (no Matrix backend){{_reset}}\n"
        else
            printf "{{_red}}❌ Failed to create instance '{{instance}}'{{_reset}}\n"
            exit 1
        fi
    else
        if ./deploy.py create "{{instance}}" --matrix "{{matrix}}"; then
            printf "{{_green}}✅ Instance '{{instance}}' created successfully with {{matrix}} backend{{_reset}}\n"
        else
            printf "{{_red}}❌ Failed to create instance '{{instance}}'{{_reset}}\n"
            exit 1
        fi
    fi

    printf "{{_yellow}}💡 Use 'just start {{instance}}' to start the instance{{_reset}}\n"

# Start instance with all services (frontend + backend + Matrix)
start instance=default_instance: (_validate-instance instance) _check-deploy-script
    #!/usr/bin/env bash
    printf "{{_cyan}}▶️  Starting instance '{{instance}}' with all services...{{_reset}}\n"
    just _deploy-cmd start "{{instance}}"
    printf "{{_green}}✅ Instance '{{instance}}' started successfully{{_reset}}\n"
    printf "{{_yellow}}💡 Use 'just logs {{instance}}' to view logs{{_reset}}\n"

# Start backend and Matrix services only (no frontend)
start-backend instance=default_instance: (_validate-instance instance) _check-deploy-script
    #!/usr/bin/env bash
    printf "{{_cyan}}▶️  Starting instance '{{instance}}' (backend + Matrix only)...{{_reset}}\n"
    just _deploy-cmd start "{{instance}}" --no-frontend
    printf "{{_green}}✅ Instance '{{instance}}' backend started successfully{{_reset}}\n"
    printf "{{_yellow}}💡 Use 'just logs {{instance}}' to view logs{{_reset}}\n"

# Stop instance services
stop instance=default_instance: (_validate-instance instance) _check-deploy-script
    #!/usr/bin/env bash
    printf "{{_cyan}}⏹️  Stopping instance '{{instance}}'...{{_reset}}\n"
    just _deploy-cmd stop "{{instance}}"
    printf "{{_green}}✅ Instance '{{instance}}' stopped successfully{{_reset}}\n"

# List all instances with their status
list: _check-deploy-script
    #!/usr/bin/env bash
    printf "{{_cyan}}📋 Listing all instances:{{_reset}}\n"
    just _deploy-cmd list

# Show detailed status for a specific instance
status instance=default_instance: (_validate-instance instance)
    #!/usr/bin/env bash
    printf "{{_cyan}}📊 Status for instance '{{instance}}':{{_reset}}\n"
    cd deploy || exit 1
    if docker compose -p "{{instance}}" ps --format table 2>/dev/null; then
        echo
        printf "{{_yellow}}Docker containers status:{{_reset}}\n"
        docker compose -p "{{instance}}" ps
    else
        printf "{{_yellow}}⚠️  No containers found for instance '{{instance}}'{{_reset}}\n"
    fi

# ═══════════════════════════════════════════════════════════════════════════════
# Data Management Commands
# ═══════════════════════════════════════════════════════════════════════════════

# Clean instance data (remove with confirmation)
clean instance=default_instance: (_validate-instance instance) _check-deploy-script
    #!/usr/bin/env bash
    printf "{{_red}}🧹 This will remove instance '{{instance}}' and ALL its data!{{_reset}}\n"
    printf "{{_yellow}}⚠️  This action cannot be undone!{{_reset}}\n"
    read -p "Are you sure? Type 'yes' to continue: " -r
    if [[ $REPLY == "yes" ]]; then
        printf "{{_cyan}}🧹 Removing instance: {{instance}}{{_reset}}\n"
        cd deploy || exit 1
        ./deploy.py remove "{{instance}}" --force || true
        printf "{{_green}}✅ Cleanup complete{{_reset}}\n"
    else
        printf "{{_yellow}}❌ Cleanup cancelled{{_reset}}\n"
        exit 1
    fi

# Force clean without confirmation (dangerous!)
clean-force instance=default_instance: (_validate-instance instance) _check-deploy-script
    #!/usr/bin/env bash
    printf "{{_red}}🧹 Force removing instance: {{instance}}{{_reset}}\n"
    cd deploy || exit 1
    ./deploy.py remove "{{instance}}" --force || true
    printf "{{_green}}✅ Force cleanup complete{{_reset}}\n"

# Full reset - remove all instances and data
reset: _check-deploy-script
    #!/usr/bin/env bash
    printf "{{_red}}🔄 This will remove ALL instances and their data!{{_reset}}\n"
    printf "{{_yellow}}⚠️  This action cannot be undone!{{_reset}}\n"
    read -p "Are you sure? Type 'RESET' to continue: " -r
    if [[ $REPLY == "RESET" ]]; then
        printf "{{_cyan}}🔄 Full reset: removing all instances...{{_reset}}\n"
        just _deploy-cmd remove --all --force
        printf "{{_green}}✅ Reset complete! Use 'just create' to start fresh.{{_reset}}\n"
    else
        printf "{{_yellow}}❌ Reset cancelled{{_reset}}\n"
        exit 1
    fi

# Force reset without confirmation (very dangerous!)
reset-force: _check-deploy-script
    #!/usr/bin/env bash
    printf "{{_red}}🔄 Force reset: removing all instances...{{_reset}}\n"
    just _deploy-cmd remove --all --force
    printf "{{_green}}✅ Force reset complete! Use 'just create' to start fresh.{{_reset}}\n"

# ═══════════════════════════════════════════════════════════════════════════════
# Development Tools
# ═══════════════════════════════════════════════════════════════════════════════

# View logs for instance (follow mode)
logs instance=default_instance: (_validate-instance instance)
    #!/usr/bin/env bash
    printf "{{_cyan}}📋 Viewing logs for instance '{{instance}}' (Ctrl+C to exit)...{{_reset}}\n"
    cd deploy || exit 1
    if ! docker compose -p "{{instance}}" logs -f; then
        printf "{{_red}}❌ Failed to view logs for instance '{{instance}}'{{_reset}}\n"
        printf "{{_yellow}}💡 Make sure the instance exists and is running{{_reset}}\n"
        exit 1
    fi

# View recent logs without following
logs-recent instance=default_instance lines="100": (_validate-instance instance)
    #!/usr/bin/env bash
    printf "{{_cyan}}📋 Recent {{lines}} lines for instance '{{instance}}':{{_reset}}\n"
    cd deploy || exit 1
    if ! docker compose -p "{{instance}}" logs --tail={{lines}}; then
        printf "{{_red}}❌ Failed to view logs for instance '{{instance}}'{{_reset}}\n"
        printf "{{_yellow}}💡 Make sure the instance exists and is running{{_reset}}\n"
        exit 1
    fi

# Shell into backend container
shell instance=default_instance: (_validate-instance instance)
    #!/usr/bin/env bash
    printf "{{_cyan}}🐚 Opening shell in backend container for instance '{{instance}}'...{{_reset}}\n"
    cd deploy || exit 1
    if ! docker compose -p "{{instance}}" exec backend bash; then
        printf "{{_red}}❌ Failed to open shell for instance '{{instance}}'{{_reset}}\n"
        printf "{{_yellow}}💡 Make sure the instance is running{{_reset}}\n"
        exit 1
    fi

# Execute command in backend container (e.g., just exec prod bash)
exec instance +command: (_validate-instance instance)
    #!/usr/bin/env bash
    printf "{{_cyan}}🎯 Executing command in backend container for instance '{{instance}}'...{{_reset}}\n"
    cd deploy || exit 1
    if ! docker compose -p "{{instance}}" exec backend {{command}}; then
        printf "{{_red}}❌ Failed to execute command for instance '{{instance}}'{{_reset}}\n"
        printf "{{_yellow}}💡 Make sure the instance is running{{_reset}}\n"
        exit 1
    fi

# ═══════════════════════════════════════════════════════════════════════════════
# Maintenance & Development
# ═══════════════════════════════════════════════════════════════════════════════

# Rebuild and restart an instance (useful for development)
rebuild instance=default_instance: (_validate-instance instance)
    #!/usr/bin/env bash
    printf "{{_cyan}}🔨 Rebuilding and restarting instance '{{instance}}'...{{_reset}}\n"
    cd deploy || exit 1
    printf "{{_yellow}}⏹️  Stopping instance...{{_reset}}\n"
    docker compose -p "{{instance}}" down || true
    printf "{{_yellow}}🔨 Rebuilding containers...{{_reset}}\n"
    docker compose -p "{{instance}}" build --no-cache
    printf "{{_yellow}}▶️  Starting instance...{{_reset}}\n"
    docker compose -p "{{instance}}" up -d
    printf "{{_green}}✅ Instance '{{instance}}' rebuilt and restarted{{_reset}}\n"

# Pull latest images and restart instance
update instance=default_instance: (_validate-instance instance)
    #!/usr/bin/env bash
    printf "{{_cyan}}📥 Updating instance '{{instance}}' with latest images...{{_reset}}\n"
    cd deploy || exit 1
    printf "{{_yellow}}📥 Pulling latest images...{{_reset}}\n"
    docker compose -p "{{instance}}" pull
    printf "{{_yellow}}🔄 Recreating containers...{{_reset}}\n"
    docker compose -p "{{instance}}" up -d --force-recreate
    printf "{{_green}}✅ Instance '{{instance}}' updated{{_reset}}\n"

# Show docker compose configuration for debugging
config instance=default_instance: (_validate-instance instance)
    #!/usr/bin/env bash
    printf "{{_cyan}}⚙️  Docker Compose configuration for instance '{{instance}}':{{_reset}}\n"
    cd deploy || exit 1
    docker compose -p "{{instance}}" config

# Clean up unused Docker resources
cleanup-docker:
    #!/usr/bin/env bash
    printf "{{_cyan}}🧹 Cleaning up unused Docker resources...{{_reset}}\n"
    printf "{{_yellow}}Removing unused containers...{{_reset}}\n"
    docker container prune -f || true
    printf "{{_yellow}}Removing unused images...{{_reset}}\n"
    docker image prune -f || true
    printf "{{_yellow}}Removing unused networks...{{_reset}}\n"
    docker network prune -f || true
    printf "{{_yellow}}Removing unused volumes (be careful!)...{{_reset}}\n"
    read -p "Remove unused volumes? This may delete data! (y/N): " -n 1 -r
    echo
    if [[ $REPLY =~ ^[Yy]$ ]]; then
        docker volume prune -f || true
    fi
    printf "{{_green}}✅ Docker cleanup complete{{_reset}}\n"

# ═══════════════════════════════════════════════════════════════════════════════
# Legacy & Migration Support
# ═══════════════════════════════════════════════════════════════════════════════

# Legacy reset command from original Makefile (kept for compatibility)
old-reset:
    #!/usr/bin/env bash
    printf "{{_yellow}}⚠️  Running legacy reset command...{{_reset}}\n"
    printf "{{_red}}This is deprecated - use 'just reset' instead{{_reset}}\n"
    docker compose down -v || true
    rm -f matrix_state.yaml || true
    docker volume prune -f || true
    rm -rf tmp/ || true
    printf "{{_green}}✅ Legacy reset complete! Run 'just create' then start to begin fresh.{{_reset}}\n"

# Migrate from Makefile workflow to justfile (informational)
migrate-info:
    #!/usr/bin/env bash
    printf "{{_cyan}}🔄 Migrating from Makefile to justfile:{{_reset}}\n"
    echo
    printf "{{_yellow}}Old Command{{_reset}}                  {{_cyan}}→{{_reset}} {{_yellow}}New Command{{_reset}}\n"
    printf "{{_green}}make help{{_reset}}                    {{_cyan}}→{{_reset}} {{_green}}just help{{_reset}}\n"
    printf "{{_green}}make create{{_reset}}                  {{_cyan}}→{{_reset}} {{_green}}just create{{_reset}}\n"
    printf "{{_green}}make create INSTANCE=prod{{_reset}}    {{_cyan}}→{{_reset}} {{_green}}just create prod{{_reset}}\n"
    printf "{{_green}}make start INSTANCE=prod{{_reset}}     {{_cyan}}→{{_reset}} {{_green}}just start prod{{_reset}}\n"
    printf "{{_green}}make stop INSTANCE=prod{{_reset}}      {{_cyan}}→{{_reset}} {{_green}}just stop prod{{_reset}}\n"
    printf "{{_green}}make logs INSTANCE=prod{{_reset}}      {{_cyan}}→{{_reset}} {{_green}}just logs prod{{_reset}}\n"
    printf "{{_green}}make shell INSTANCE=prod{{_reset}}     {{_cyan}}→{{_reset}} {{_green}}just shell prod{{_reset}}\n"
    echo
    printf "{{_yellow}}New features available:{{_reset}}\n"
    printf "  {{_green}}just status prod{{_reset}}           # Show detailed status\n"
    printf "  {{_green}}just rebuild prod{{_reset}}          # Rebuild and restart\n"
    printf "  {{_green}}just logs-recent prod{{_reset}}      # Show recent logs\n"
    printf "  {{_green}}just cleanup-docker{{_reset}}        # Clean Docker resources\n"
