#!/usr/bin/env bash
set -euo pipefail

echo "🚀 MindRoom Quickstart"
echo ""

# Check for uv (installs Python automatically)
if ! command -v uv &> /dev/null; then
    echo "❌ uv not found. Install: curl -LsSf https://astral.sh/uv/install.sh | sh"
    exit 1
fi

# Check for bun (optional, for frontend)
if ! command -v bun &> /dev/null; then
    echo "⚠️  bun not found - skipping frontend. Install: curl -fsSL https://bun.sh/install | bash"
fi

echo "📦 Installing dependencies..."
uv sync --all-extras
command -v bun &> /dev/null && (cd frontend && bun install)

echo ""
echo "✅ Ready! Run: ./run-backend.sh"
command -v bun &> /dev/null && echo "   Frontend: ./run-frontend.sh"
