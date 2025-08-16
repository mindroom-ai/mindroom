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
		cd deploy && ./instance_manager.py create $(INSTANCE); \
	else \
		cd deploy && ./instance_manager.py create $(INSTANCE) --matrix $(MATRIX); \
	fi

start:
	cd deploy && ./instance_manager.py start $(INSTANCE)

stop:
	cd deploy && ./instance_manager.py stop $(INSTANCE)

list:
	cd deploy && ./instance_manager.py list

# Cleanup commands
clean:
	@echo "🧹 Cleaning instance: $(INSTANCE)"
	cd deploy && ./instance_manager.py stop $(INSTANCE) 2>/dev/null || true
	rm -rf deploy/instance_data/$(INSTANCE)
	rm -f deploy/.env.$(INSTANCE)
	@echo "✅ Instance $(INSTANCE) cleaned"

reset:
	@echo "🔄 Full reset: removing all instances and data..."
	@cd deploy && docker ps -q --filter "name=mindroom-*" | xargs -r docker stop 2>/dev/null || true
	@cd deploy && docker ps -aq --filter "name=mindroom-*" | xargs -r docker rm 2>/dev/null || true
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
