#!/usr/bin/env bash
cd /app/workspace 2>/dev/null || true
exec uv run uvicorn mindroom.api.sandbox_runner_app:app --host 0.0.0.0 --port 8766
