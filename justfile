# justfile for mindroom - Federation deployment

# Default values
default_instance := env_var_or_default("INSTANCE", "default")
default_matrix := env_var_or_default("MATRIX", "tuwunel")

# Default recipe - show help
default:
    @just --list

# Show help
help:
    @echo "mindroom - Federation commands:"
    @echo "-------------------------------"
    @echo "create        - Create new instance (INSTANCE=name MATRIX=tuwunel|synapse|none)"
    @echo "start         - Start instance with all services (INSTANCE=name)"
    @echo "start-backend - Start backend + Matrix only, no frontend (INSTANCE=name)"
    @echo "stop          - Stop instance (INSTANCE=name)"
    @echo "list          - List all instances"
    @echo "clean         - Clean instance data (INSTANCE=name)"
    @echo "reset         - Full reset: remove all instances and data"
    @echo "logs          - View logs (INSTANCE=name)"
    @echo "shell         - Shell into backend container (INSTANCE=name)"
    @echo ""
    @echo "Examples:"
    @echo "  just create                        # Create default instance with Tuwunel"
    @echo "  just create prod synapse"
    @echo "  just start prod"
    @echo "  just stop prod"
    @echo "  just logs prod"

# Create new instance
create instance=default_instance matrix=default_matrix:
    #!/usr/bin/env bash
    if [ "{{matrix}}" = "none" ]; then
        cd deploy && ./deploy.py create {{instance}}
    else
        cd deploy && ./deploy.py create {{instance}} --matrix {{matrix}}
    fi

# Start instance with all services
start instance=default_instance:
    cd deploy && ./deploy.py start {{instance}}

# Start backend + Matrix only, no frontend
start-backend instance=default_instance:
    cd deploy && ./deploy.py start {{instance}} --no-frontend

# Stop instance
stop instance=default_instance:
    cd deploy && ./deploy.py stop {{instance}}

# List all instances
list:
    cd deploy && ./deploy.py list

# Clean instance data
clean instance=default_instance:
    @echo "🧹 Removing instance: {{instance}}"
    @cd deploy && ./deploy.py remove {{instance}} --force || true
    @echo "✅ Cleanup complete"

# Full reset - remove all instances and data
reset:
    @echo "🔄 Full reset: removing all instances..."
    cd deploy && ./deploy.py remove --all --force
    @echo "✅ Reset complete! Use 'just create' to start fresh."

# View logs
logs instance=default_instance:
    cd deploy && docker compose -p {{instance}} logs -f

# Shell into backend container
shell instance=default_instance:
    cd deploy && docker compose -p {{instance}} exec backend bash

# Legacy reset from original Makefile
old-reset:
    docker compose down -v
    rm -f matrix_state.yaml
    docker volume prune -f
    rm -rf tmp/
    @echo "✅ Reset complete! Run 'just create' then 'mindroom run' to start fresh."
