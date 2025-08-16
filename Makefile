# Makefile for mindroom - Federation deployment

.PHONY: help create start stop list clean reset logs shell

# Default values
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
	@echo "🧹 Removing instance: $(INSTANCE)"
	cd deploy && ./deploy remove $(INSTANCE) --force
	@echo "✅ Instance $(INSTANCE) removed"

reset:
	@echo "🔄 Full reset: removing all instances..."
	cd deploy && ./deploy remove --all --force
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
