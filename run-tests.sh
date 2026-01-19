#!/usr/bin/env bash

echo "Running MindRoom Tests"
echo "======================"

# Frontend tests
echo ""
echo "Running Frontend Tests (TypeScript/React)..."
echo "-------------------------------------------"
cd frontend
bun run vitest run

# Backend tests
echo ""
echo "Running Backend Tests (Python/FastAPI)..."
echo "----------------------------------------"
cd ..
uv run pytest tests/api/ -v -o addopts=""

# Bot tests
echo ""
echo "Running Bot Tests (Python)..."
echo "-----------------------------"
uv run pytest tests/ -v -o addopts="" --ignore=tests/api/

echo ""
echo "Test run complete!"
