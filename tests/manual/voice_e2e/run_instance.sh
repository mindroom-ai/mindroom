#!/usr/bin/env bash
# Run the voice-test MindRoom instance against the local stack.
# Usage: run_instance.sh [venv-dir]   (default .venv314, the production Python version)
# MINDROOM_VOICETEST_REPO selects the checkout to run (default: this one).
# The process allows any same-user process to ptrace it, so voice_call_e2e.py
# can inject event-loop stalls with sys.remote_exec (test instance only).
set -euo pipefail
ROOT="${MINDROOM_VOICETEST_ROOT:-$HOME/.mindroom-voicetest}"
REPO="${MINDROOM_VOICETEST_REPO:-$(cd "$(dirname "$0")/../../.." && pwd)}"
VENV="${1:-.venv314}"
set -a
# shellcheck disable=SC1091
. "$ROOT/.env"
set +a
export MINDROOM_CONFIG_PATH="$ROOT/config.yaml"
export MINDROOM_STORAGE_PATH="$ROOT/mindroom_data"
mkdir -p "$ROOT/run"
echo $$ > "$ROOT/run/instance.pid"
cd "$ROOT"
exec "$REPO/$VENV/bin/python" -c '
import ctypes, sys
ctypes.CDLL(None).prctl(0x59616D61, ctypes.c_ulong(-1), 0, 0, 0)  # PR_SET_PTRACER_ANY
from mindroom.cli.main import main
sys.argv[0] = "mindroom"
sys.exit(main())
' run --config "$ROOT/config.yaml" --storage-path "$ROOT/mindroom_data" \
  --api-port "${MINDROOM_VOICETEST_API_PORT:-8799}" --api-host 127.0.0.1
