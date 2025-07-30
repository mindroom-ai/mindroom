# Minimal Makefile for mindroom

.PHONY: help up down setup run test clean

help:
	@echo "mindroom - Minimal commands:"
	@echo "------------------------"
	@echo "up      - Start Matrix server (Synapse + PostgreSQL + Redis)"
	@echo "down    - Stop Matrix server"
	@echo "setup   - Create bot and test users"
	@echo "run     - Run the mindroom bot"
	@echo "test    - Test bot connection"
	@echo "clean   - Clean up everything"

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
	rm -f matrix_users.yaml .env.python
	find . -type d -name "__pycache__" -exec rm -rf {} +
	find . -type f -name "*.pyc" -delete
