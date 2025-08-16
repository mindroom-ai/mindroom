# Makefile for mindroom - Federation deployment

.PHONY: help create start stop list clean reset logs shell

# Default values
INSTANCE ?= default
MATRIX ?= tuwunel

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
	@echo "  make create                        # Create default instance with Tuwunel"
	@echo "  make create INSTANCE=prod MATRIX=synapse"
	@echo "  make start INSTANCE=prod"
	@echo "  make stop INSTANCE=prod"
	@echo "  make logs INSTANCE=prod"

# Federation commands
create:
	@if [ "$(MATRIX)" = "none" ]; then \
		cd deploy && ./deploy.py create $(INSTANCE); \
	else \
		cd deploy && ./deploy.py create $(INSTANCE) --matrix $(MATRIX); \
	fi

start:
	cd deploy && ./deploy.py start $(INSTANCE)

start-backend:
	cd deploy && ./deploy.py start $(INSTANCE) --no-frontend

stop:
	cd deploy && ./deploy.py stop $(INSTANCE)

list:
	cd deploy && ./deploy.py list

# Cleanup commands
clean:
	@echo "🧹 Removing instance: $(INSTANCE)"
	@cd deploy && ./deploy.py remove $(INSTANCE) --force || true
	@echo "✅ Cleanup complete"

reset:
	@echo "🔄 Full reset: removing all instances..."
	cd deploy && ./deploy.py remove --all --force
	@echo "Cleaning up any remaining files..."
	rm -f matrix_state.yaml
	find . -type d -name "__pycache__" -exec rm -rf {} +
	find . -type f -name "*.pyc" -delete
	@echo "✅ Reset complete! Use 'make create' to start fresh."


# Development helpers
logs:
	cd deploy && docker compose -p $(INSTANCE) logs -f

shell:
	cd deploy && docker compose -p $(INSTANCE) exec backend bash

old-reset:
	docker compose down -v
	rm -f matrix_state.yaml
	docker volume prune -f
	rm -rf tmp/
	@echo "✅ Reset complete! Run 'make up' then 'mindroom run' to start fresh."
