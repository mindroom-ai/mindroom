#!/usr/bin/env bash
set -e

echo "Activating virtual environment..."
source .venv/bin/activate

echo "Starting mindroom in background..."
mindroom run &
BOT_PID=$!

echo "Bot PID: $BOT_PID"
echo "Waiting 10 seconds for bot to fully start..."
sleep 10

echo "Running test script..."
python test_bot_interaction.py

echo "Checking if memory was created..."
if [ -d "tmp/chroma" ]; then
    echo "✓ Memory storage directory created!"
    ls -la tmp/chroma/
else
    echo "✗ Memory storage directory not found"
fi

echo "Killing bot process..."
kill $BOT_PID 2>/dev/null || true

echo "Test complete!"
