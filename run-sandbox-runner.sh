#!/usr/bin/env bash
set -euo pipefail

cd /app/workspace 2>/dev/null || true

if [[ -z "${MINDROOM_RUNTIME_PATHS_JSON:-}" ]]; then
  export MINDROOM_RUNTIME_PATHS_JSON="$(
    /app/.venv/bin/python - <<'PY'
import json
import os

from mindroom.constants import resolve_primary_runtime_paths, serialize_runtime_paths

runtime_paths = resolve_primary_runtime_paths(process_env=dict(os.environ))
print(json.dumps(serialize_runtime_paths(runtime_paths), separators=(",", ":"), sort_keys=True))
PY
  )"
fi

exec /app/.venv/bin/python -m uvicorn mindroom.api.sandbox_runner_app:app --host 0.0.0.0 --port "${MINDROOM_SANDBOX_RUNNER_PORT:-8766}"
