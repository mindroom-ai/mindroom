#!/usr/bin/env bash

echo "Running MindRoom Widget Tests"
echo "============================="

# Frontend tests
echo ""
echo "Running Frontend Tests (TypeScript/React)..."
echo "-------------------------------------------"
cd frontend
pnpm exec vitest run

# Backend tests (now in main project)
echo ""
echo "Running Backend Tests (Python/FastAPI)..."
echo "----------------------------------------"
cd ..  # Return to project root
uv run pytest tests/api/ -v -o addopts=""

echo ""
echo "Test run complete!"
