#!/usr/bin/env bash

echo "Running MindRoom Widget Tests"
echo "============================="

# Frontend tests
echo ""
echo "Running Frontend Tests (TypeScript/React)..."
echo "-------------------------------------------"
cd frontend
pnpm test -- --run

# Backend tests
echo ""
echo "Running Backend Tests (Python/FastAPI)..."
echo "----------------------------------------"
cd ../backend
uv run pytest tests/ -v -o addopts=""

echo ""
echo "Test run complete!"
