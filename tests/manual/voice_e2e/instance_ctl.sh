#!/usr/bin/env bash
# Start/stop the voice-test instance in the background.
# Usage: instance_ctl.sh start|stop|restart [venv-dir]
set -euo pipefail
ROOT="${MINDROOM_VOICETEST_ROOT:-$HOME/.mindroom-voicetest}"
HERE="$(cd "$(dirname "$0")" && pwd)"
stop() {
  if [[ -f "$ROOT/run/instance.pid" ]] && kill -0 "$(cat "$ROOT/run/instance.pid")" 2>/dev/null; then
    pid="$(cat "$ROOT/run/instance.pid")"
    kill "$pid"
    timeout 60 bash -c "while kill -0 $pid 2>/dev/null; do :; done"
  fi
  if [[ -f "$ROOT/run/instance.log" ]]; then
    mv "$ROOT/run/instance.log" "$ROOT/run/instance-$(date +%Y%m%d-%H%M%S).log"
  fi
}
start() {
  nohup "$HERE/run_instance.sh" "${1:-.venv314}" > "$ROOT/run/instance.log" 2>&1 &
  timeout 180 bash -c "until curl -sf 127.0.0.1:${MINDROOM_VOICETEST_API_PORT:-8799}/api/health >/dev/null; do :; done"
  echo "instance healthy (pid $(cat "$ROOT/run/instance.pid"))"
}
case "$1" in
  start) start "${2:-}" ;;
  stop) stop ;;
  restart) stop; start "${2:-}" ;;
esac
