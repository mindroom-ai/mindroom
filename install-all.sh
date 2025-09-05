#!/usr/bin/env bash
# Install all project dependencies using pnpm

set -e

echo "📦 Installing all project dependencies with pnpm..."
echo ""

# Install frontend widget dependencies
if [ -d "frontend" ]; then
    echo "Installing frontend widget dependencies..."
    (cd frontend && pnpm install)
fi

# Install admin dashboard dependencies
if [ -d "apps/admin-dashboard" ]; then
    echo "Installing admin dashboard dependencies..."
    (cd apps/admin-dashboard && pnpm install)
fi

# Install customer portal dependencies
if [ -d "apps/customer-portal" ]; then
    echo "Installing customer portal dependencies..."
    (cd apps/customer-portal && pnpm install)
fi

# Install stripe handler dependencies
if [ -d "services/stripe-handler" ]; then
    echo "Installing stripe handler dependencies..."
    (cd services/stripe-handler && pnpm install)
fi

echo ""
echo "✅ All dependencies installed successfully!"
echo ""
echo "To start services, use:"
echo "  ./test-panels-local.sh"
