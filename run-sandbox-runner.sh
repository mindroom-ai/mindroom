#!/usr/bin/env bash
set -euo pipefail

cd /app/workspace 2>/dev/null || true

if [[ -z "${MINDROOM_SANDBOX_STARTUP_MANIFEST_PATH:-}" ]]; then
  export MINDROOM_SANDBOX_STARTUP_MANIFEST_PATH="$(
    /app/.venv/bin/python - <<'PY'
import os

from mindroom.constants import resolve_primary_runtime_paths, write_startup_manifest

runtime_paths = resolve_primary_runtime_paths(process_env=dict(os.environ))
print(write_startup_manifest(runtime_paths.storage_root, runtime_paths, public_runtime=True))
PY
  )"
fi

exec /app/.venv/bin/python -m uvicorn mindroom.api.sandbox_runner_app:app --host 0.0.0.0 --port "${MINDROOM_SANDBOX_RUNNER_PORT:-8766}"
