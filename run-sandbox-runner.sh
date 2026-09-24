#!/usr/bin/env bash
set -euo pipefail

cd /app/workspace 2>/dev/null || true

if [[ -z "${MINDROOM_SANDBOX_STARTUP_MANIFEST_PATH:-}" ]]; then
  # No primary published a manifest for this runner, so derive one from the
  # immutable container env on every boot and publish its digest alongside it:
  # the runner refuses to boot from a manifest it cannot prove.
  startup_manifest_state="$(
    /app/.venv/bin/python - <<'PY'
import os

from mindroom.constants import resolve_primary_runtime_paths, startup_manifest_sha256, write_startup_manifest

runtime_paths = resolve_primary_runtime_paths(process_env=dict(os.environ))
manifest_path = write_startup_manifest(runtime_paths.storage_root, runtime_paths, public_runtime=True)
print(manifest_path)
print(startup_manifest_sha256(runtime_paths, public_runtime=True))
PY
  )"
  mapfile -t startup_manifest_lines <<<"${startup_manifest_state}"
  MINDROOM_SANDBOX_STARTUP_MANIFEST_PATH="${startup_manifest_lines[-2]}"
  MINDROOM_SANDBOX_STARTUP_MANIFEST_SHA256="${startup_manifest_lines[-1]}"
  export MINDROOM_SANDBOX_STARTUP_MANIFEST_PATH
  export MINDROOM_SANDBOX_STARTUP_MANIFEST_SHA256
fi

exec /app/.venv/bin/python -m uvicorn mindroom.api.sandbox_runner_app:app --host 0.0.0.0 --port "${MINDROOM_SANDBOX_RUNNER_PORT:-8766}"
