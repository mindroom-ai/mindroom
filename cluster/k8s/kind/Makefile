# Makefile for MindRoom local development with kind
# This provides a simple, consistent interface for developers

.PHONY: help up down status logs test clean reset frontend backend instance shell

# Default target
help:
	@echo "MindRoom Local Development (kind)"
	@echo "================================="
	@echo ""
	@echo "Quick Start:"
	@echo "  make up        - Create cluster and deploy everything"
	@echo "  make status    - Check status of all components"
	@echo "  make frontend  - Access platform frontend (http://localhost:3000)"
	@echo "  make backend   - Access platform backend (http://localhost:8000)"
	@echo "  make down      - Stop cluster (preserves data)"
	@echo "  make clean     - Delete cluster and all data"
	@echo ""
	@echo "Development:"
	@echo "  make logs      - Tail platform logs"
	@echo "  make test      - Run access tests"
	@echo "  make shell     - Shell into platform backend"
	@echo "  make instance  - Deploy a test instance"
	@echo ""
	@echo "Troubleshooting:"
	@echo "  make reset     - Full reset (clean + up)"
	@echo "  make fix-images - Fix image pull issues"
	@echo ""

# Check if kind cluster exists
CLUSTER_EXISTS := $(shell kind get clusters 2>/dev/null | grep -c '^mindroom$$' || echo 0)
KUBECONFIG := $(HOME)/.kube/kind-mindroom

# Bring up the entire stack
up: check-deps
	@echo "🚀 Starting MindRoom local environment..."
	@if [ $(CLUSTER_EXISTS) -eq 0 ]; then \
		echo "Creating kind cluster..."; \
		./up.sh; \
	else \
		echo "✓ Cluster already exists"; \
	fi
	@echo "Building and loading images..."
	@./build_load_images.sh
	@echo "Installing platform..."
	@./install_platform.sh
	@$(MAKE) fix-images
	@echo ""
	@echo "✅ Setup complete! Run 'make frontend' to access the platform"

# Fix image pull issues (common problem)
fix-images:
	@echo "🔧 Fixing image configuration..."
	@export KUBECONFIG=$(KUBECONFIG) && \
	kubectl set image deployment/platform-backend app=git.nijho.lt/basnijholt/platform-backend:amd64 -n mindroom-staging 2>/dev/null || true && \
	kubectl set image deployment/platform-frontend app=git.nijho.lt/basnijholt/platform-frontend:amd64 -n mindroom-staging 2>/dev/null || true && \
	kubectl patch deployment platform-backend -n mindroom-staging \
		-p '{"spec":{"template":{"spec":{"containers":[{"name":"app","imagePullPolicy":"IfNotPresent"}]}}}}' 2>/dev/null || true && \
	kubectl patch deployment platform-frontend -n mindroom-staging \
		-p '{"spec":{"template":{"spec":{"containers":[{"name":"app","imagePullPolicy":"IfNotPresent"}]}}}}' 2>/dev/null || true
	@sleep 5

# Shut down cluster (preserves it)
down:
	@echo "📦 Cluster preserved. Run 'make up' to restart or 'make clean' to delete."

# Delete everything
clean:
	@echo "🧹 Cleaning up..."
	@kind delete cluster --name mindroom 2>/dev/null || true
	@rm -f $(KUBECONFIG)
	@echo "✓ Cluster deleted"

# Full reset
reset: clean up

# Check status
status:
	@if [ $(CLUSTER_EXISTS) -eq 0 ]; then \
		echo "❌ No kind cluster found. Run 'make up' to create one."; \
		exit 1; \
	fi
	@echo "📊 MindRoom Status"
	@echo "=================="
	@echo ""
	@echo "Platform pods:"
	@export KUBECONFIG=$(KUBECONFIG) && \
	kubectl get pods -n mindroom-staging --no-headers 2>/dev/null | while read line; do \
		NAME=$$(echo $$line | awk '{print $$1}'); \
		READY=$$(echo $$line | awk '{print $$2}'); \
		STATUS=$$(echo $$line | awk '{print $$3}'); \
		if [ "$$STATUS" = "Running" ]; then \
			echo "  ✓ $$NAME ($$READY)"; \
		else \
			echo "  ✗ $$NAME ($$STATUS)"; \
		fi; \
	done || echo "  No platform pods found"
	@echo ""
	@echo "Instance pods:"
	@export KUBECONFIG=$(KUBECONFIG) && \
	kubectl get pods -n mindroom-instances --no-headers 2>/dev/null | while read line; do \
		NAME=$$(echo $$line | awk '{print $$1}'); \
		READY=$$(echo $$line | awk '{print $$2}'); \
		STATUS=$$(echo $$line | awk '{print $$3}'); \
		if [ "$$STATUS" = "Running" ]; then \
			echo "  ✓ $$NAME ($$READY)"; \
		else \
			echo "  ✗ $$NAME ($$STATUS)"; \
		fi; \
	done || echo "  No instances deployed"

# Access frontend
frontend:
	@if [ $(CLUSTER_EXISTS) -eq 0 ]; then \
		echo "❌ No cluster found. Run 'make up' first."; \
		exit 1; \
	fi
	@echo "🌐 Platform frontend: http://localhost:3000"
	@echo "   Press Ctrl+C to stop"
	@export KUBECONFIG=$(KUBECONFIG) && \
	kubectl port-forward -n mindroom-staging svc/platform-frontend 3000:3000

# Access backend
backend:
	@if [ $(CLUSTER_EXISTS) -eq 0 ]; then \
		echo "❌ No cluster found. Run 'make up' first."; \
		exit 1; \
	fi
	@echo "🔧 Platform backend: http://localhost:8000"
	@echo "   Health check: http://localhost:8000/health"
	@echo "   Press Ctrl+C to stop"
	@export KUBECONFIG=$(KUBECONFIG) && \
	kubectl port-forward -n mindroom-staging svc/platform-backend 8000:8000

# View logs
logs:
	@if [ $(CLUSTER_EXISTS) -eq 0 ]; then \
		echo "❌ No cluster found. Run 'make up' first."; \
		exit 1; \
	fi
	@echo "📋 Following platform logs (Ctrl+C to stop)..."
	@export KUBECONFIG=$(KUBECONFIG) && \
	kubectl logs -n mindroom-staging -l app=platform-backend -f --tail=50

# Run tests
test:
	@if [ $(CLUSTER_EXISTS) -eq 0 ]; then \
		echo "❌ No cluster found. Run 'make up' first."; \
		exit 1; \
	fi
	@if [ -f test-access.sh ]; then \
		./test-access.sh; \
	else \
		echo "Test script not found"; \
	fi

# Deploy test instance
instance:
	@if [ $(CLUSTER_EXISTS) -eq 0 ]; then \
		echo "❌ No cluster found. Run 'make up' first."; \
		exit 1; \
	fi
	@echo "🎯 Deploying test instance..."
	@export KUBECONFIG=$(KUBECONFIG) && \
	helm upgrade --install instance-1 ../instance \
		--namespace mindroom-instances \
		--create-namespace \
		--set customer=1 \
		--set baseDomain=local \
		--set persistence.enabled=false \
		--set matrix.homeserver_url=https://matrix.org \
		--set matrix.admin_user="@test:matrix.org" \
		--set matrix.admin_password=test \
		--wait --timeout 2m || true
	@echo "Instance deployed. Check status with 'make status'"

# Shell into platform backend
shell:
	@if [ $(CLUSTER_EXISTS) -eq 0 ]; then \
		echo "❌ No cluster found. Run 'make up' first."; \
		exit 1; \
	fi
	@echo "🐚 Opening shell in platform-backend..."
	@export KUBECONFIG=$(KUBECONFIG) && \
	kubectl exec -it -n mindroom-staging deployment/platform-backend -- /bin/bash

# Check dependencies
check-deps:
	@which docker >/dev/null || (echo "❌ Docker not found. Please install Docker." && exit 1)
	@which kind >/dev/null || (echo "❌ kind not found. Please install: https://kind.sigs.k8s.io/docs/user/quick-start/#installation" && exit 1)
	@which kubectl >/dev/null || (echo "❌ kubectl not found. Please install kubectl." && exit 1)
	@which helm >/dev/null || (echo "❌ helm not found. Please install helm." && exit 1)
	@docker info >/dev/null 2>&1 || (echo "❌ Docker is not running. Please start Docker." && exit 1)
