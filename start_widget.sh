#!/bin/bash
# Start Mindroom Widget Services

echo "Starting Mindroom Widget Services..."
echo "===================================="

# Function to kill existing processes on ports
kill_port() {
    local port=$1
    echo "Checking port $port..."
    local pids=$(ss -tulpn 2>/dev/null | grep :$port | awk -F'pid=' '{print $2}' | cut -d',' -f1)
    if [ ! -z "$pids" ]; then
        echo "Killing existing processes on port $port..."
        for pid in $pids; do
            kill -9 $pid 2>/dev/null
        done
    fi
}

# Kill any existing processes
kill_port 8765
kill_port 3003

# Start backend
echo ""
echo "Starting backend on port 8765..."
cd widget/backend
source ../../.venv/bin/activate
python -m src.main &
BACKEND_PID=$!
cd ../..

# Wait for backend to start
sleep 3

# Start frontend
echo ""
echo "Starting frontend on port 3003..."
cd widget/frontend
pnpm run dev &
FRONTEND_PID=$!
cd ../..

echo ""
echo "Services started!"
echo "Backend PID: $BACKEND_PID (port 8765)"
echo "Frontend PID: $FRONTEND_PID (port 3003)"
echo ""
echo "Access the app at: http://localhost:3003"
echo ""
echo "To stop services, run:"
echo "  kill $BACKEND_PID $FRONTEND_PID"
echo ""
echo "Or press Ctrl+C to stop all services"

# Wait for Ctrl+C
trap "echo 'Stopping services...'; kill $BACKEND_PID $FRONTEND_PID; exit" INT
wait
