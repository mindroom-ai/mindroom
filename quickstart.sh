#!/usr/bin/env bash
set -euo pipefail

# Quickstart script for MindRoom
# This script sets up everything needed to run MindRoom

echo "🚀 MindRoom Quickstart"
echo "======================"
echo ""

# Check for required tools
echo "📋 Checking prerequisites..."

if ! command -v uv &> /dev/null; then
    echo "❌ uv is not installed. Please install it first:"
    echo "   curl -LsSf https://astral.sh/uv/install.sh | sh"
    exit 1
fi

if ! command -v python3 &> /dev/null; then
    echo "❌ Python is not installed. Please install Python 3.11 or later."
    exit 1
fi

# Check if we want to set up the widget UI
SETUP_WIDGET=false
if command -v node &> /dev/null && command -v pnpm &> /dev/null; then
    SETUP_WIDGET=true
    echo "✅ Found Node.js and pnpm - will set up widget UI"
else
    echo "⚠️  Node.js or pnpm not found - skipping widget UI setup"
    echo "   (Install them to get the web interface at http://localhost:3003)"
fi

echo ""
echo "📦 Installing Python dependencies..."
uv sync --all-extras

echo ""
echo "🔧 Setting up configuration..."
if [ ! -f config.yaml ]; then
    if [ -f config.example.yaml ]; then
        cp config.example.yaml config.yaml
        echo "✅ Created config.yaml from example"
    else
        echo "⚠️  No config.yaml found - you'll need to create one"
    fi
else
    echo "✅ config.yaml already exists"
fi

# Set up frontend if available
if [ "$SETUP_WIDGET" = true ]; then
    echo ""
    echo "🎨 Setting up frontend UI..."

    if [ -d "frontend" ]; then
        echo "  📦 Installing frontend dependencies..."
        (cd frontend && pnpm install)
    fi
fi

echo ""
echo "✅ Setup complete!"
echo ""
echo "🚀 To start MindRoom:"
echo ""

echo "   # Terminal 1: Start backend (agents + API)"
echo "   ./run-backend.sh"
if [ "$SETUP_WIDGET" = true ]; then
    echo ""
    echo "   # Terminal 2: Start frontend (optional, for web UI)"
    echo "   ./run-frontend.sh"
fi

echo ""
echo "📖 First time? Check the README for configuration details."
echo "💬 Join your Matrix client and start chatting with your agents!"
