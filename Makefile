# Minimal Makefile for mindroom

.PHONY: help up down setup run test clean reset

help:
	@echo "mindroom - Minimal commands:"
	@echo "------------------------"
	@echo "up      - Start Matrix server (Synapse + PostgreSQL + Redis)"
	@echo "down    - Stop Matrix server"
	@echo "setup   - Create bot and test users"
	@echo "run     - Run the mindroom bot"
	@echo "test    - Test bot connection"
	@echo "clean   - Clean up everything"
	@echo "reset   - Full reset: down compose, remove volumes, clean state"

up:
	docker compose up -d

down:
	docker compose down

setup:
	python scripts/mindroom.py setup

run:
	python scripts/mindroom.py run

test:
	python scripts/mindroom.py test

clean:
	docker compose down -v
	rm -f matrix_state.yaml .env.python
	find . -type d -name "__pycache__" -exec rm -rf {} +
	find . -type f -name "*.pyc" -delete

reset:
	@echo "🔄 Full reset: stopping containers, removing volumes, cleaning state..."
	docker compose down -v
	docker volume prune -f
	rm -f matrix_state.yaml .env.python
	rm -rf tmp/
	find . -type d -name "__pycache__" -exec rm -rf {} +
	find . -type f -name "*.pyc" -delete
	@echo "✅ Reset complete! Run 'make up' then 'mindroom run' to start fresh."
