#!/usr/bin/env bash

# Start both MindRoom bot and API server

trap 'kill $(jobs -p)' EXIT

mkdir -p /app/logs /app/mindroom_data

# Start bot in background (logs to stdout)
.venv/bin/python -m mindroom.cli run --log-level INFO --storage-path /app/mindroom_data &

# Start API server in foreground (logs to stdout)
.venv/bin/uvicorn mindroom.api.main:app --host 0.0.0.0 --port 8765
