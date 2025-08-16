# Makefile for mindroom - Federation deployment

.PHONY: help create start stop list clean reset logs shell

# Default instance name
INSTANCE ?= default
MATRIX ?= tuwunel

help:
	@echo "mindroom - Federation commands:"
	@echo "-------------------------------"
	@echo "create  - Create new instance (INSTANCE=name MATRIX=tuwunel|synapse|none)"
	@echo "start   - Start instance (INSTANCE=name)"
	@echo "stop    - Stop instance (INSTANCE=name)"
	@echo "list    - List all instances"
	@echo "clean   - Clean instance data (INSTANCE=name)"
	@echo "reset   - Full reset: remove all instances and data"
	@echo "logs    - View logs (INSTANCE=name)"
	@echo "shell   - Shell into backend container (INSTANCE=name)"
	@echo ""
	@echo "Examples:"
	@echo "  make create                        # Create default instance with Tuwunel"
	@echo "  make create INSTANCE=prod MATRIX=synapse"
	@echo "  make start INSTANCE=prod"
	@echo "  make stop INSTANCE=prod"
	@echo "  make logs INSTANCE=prod"

# Federation commands
create:
	@if [ "$(MATRIX)" = "none" ]; then \
		cd deploy && ./deploy create $(INSTANCE); \
	else \
		cd deploy && ./deploy create $(INSTANCE) --matrix $(MATRIX); \
	fi

start:
	cd deploy && ./deploy start $(INSTANCE)

stop:
	cd deploy && ./deploy stop $(INSTANCE)

list:
	cd deploy && ./deploy list

# Cleanup commands
clean:
	@echo "🧹 Cleaning instance: $(INSTANCE)"
	cd deploy && ./deploy stop $(INSTANCE) 2>/dev/null || true
	rm -rf deploy/instance_data/$(INSTANCE)
	rm -f deploy/.env.$(INSTANCE)
	@echo "✅ Instance $(INSTANCE) cleaned"

reset:
	@echo "🔄 Full reset: stopping all instances and removing all data..."
	@echo "Stopping all running instances..."
	@docker ps -q --filter "label=com.docker.compose.project" | xargs -r docker stop 2>/dev/null || true
	@docker ps -aq --filter "label=com.docker.compose.project" | xargs -r docker rm 2>/dev/null || true
	@echo "Removing all instance data..."
	rm -rf deploy/instance_data/
	rm -f deploy/.env.*
	rm -f deploy/instances.json
	rm -f matrix_state.yaml
	find . -type d -name "__pycache__" -exec rm -rf {} +
	find . -type f -name "*.pyc" -delete
	@echo "✅ Reset complete! Use 'make create' to start fresh."


# Development helpers
logs:
	cd deploy && docker compose -p $(INSTANCE) logs -f

shell:
	cd deploy && docker compose -p $(INSTANCE) exec backend bash
