#!/usr/bin/env bash
set -euo pipefail

cd /app/workspace 2>/dev/null || true

if [[ -z "${MINDROOM_SANDBOX_STARTUP_MANIFEST_PATH:-}" ]]; then
  startup_manifest_path="$(
    /app/.venv/bin/python -P -s - <<'PY'
import os

from mindroom.constants import resolve_primary_runtime_paths, write_startup_manifest

runtime_paths = resolve_primary_runtime_paths(process_env=dict(os.environ))
print(write_startup_manifest(runtime_paths.storage_root, runtime_paths, public_runtime=True))
PY
  )"
  export MINDROOM_SANDBOX_STARTUP_MANIFEST_PATH="${startup_manifest_path}"
fi

# `-P -s`: the workspace cwd or `HOME` must not shadow MindRoom modules or inject site code.
exec /app/.venv/bin/python -P -s -m mindroom.api.sandbox_runner_app
