#!/bin/bash

# Script to run the MindRoom Admin Dashboard

echo "🧠 Starting MindRoom Admin Dashboard..."

# Navigate to admin dashboard directory
cd apps/admin-dashboard

# Check if node_modules exists
if [ ! -d "node_modules" ]; then
    echo "Installing dependencies..."
    npm install
fi

# Check if .env exists
if [ ! -f .env ]; then
    echo "⚠️  No .env file found!"
    echo "Creating .env from .env.example..."
    cp .env.example .env
    echo "Please edit apps/admin-dashboard/.env with your credentials and restart."
    exit 1
fi

# Start the development server
echo "Starting admin dashboard on http://localhost:5173..."
npm run dev
