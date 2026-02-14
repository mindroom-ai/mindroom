#!/usr/bin/env bash
exec uv run uvicorn mindroom.api.sandbox_runner_app:app --host 0.0.0.0 --port 8766
