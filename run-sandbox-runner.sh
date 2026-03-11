#!/usr/bin/env bash
cd /app/workspace 2>/dev/null || true
exec /app/.venv/bin/python -m uvicorn mindroom.api.sandbox_runner_app:app --host 0.0.0.0 --port "${MINDROOM_SANDBOX_RUNNER_PORT:-8766}"
