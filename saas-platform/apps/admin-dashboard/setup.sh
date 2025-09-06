#!/bin/bash

# Setup script for MindRoom Admin Dashboard

echo "🧠 Setting up MindRoom Admin Dashboard..."

# Check if .env exists
if [ ! -f .env ]; then
    echo "Creating .env from .env.example..."
    cp .env.example .env
    echo "⚠️  Please edit .env with your actual credentials before running the dashboard"
fi

# Install dependencies
echo "Installing dependencies..."
npm install

echo "✅ Setup complete!"
echo ""
echo "To start the dashboard:"
echo "  1. Edit .env with your Supabase and API credentials"
echo "  2. Run: npm run dev"
echo ""
echo "The dashboard will be available at http://localhost:5173"
