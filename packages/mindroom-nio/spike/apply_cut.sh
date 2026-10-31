#!/usr/bin/env bash
# Usage: apply_cut.sh REPO_ROOT  (cuts REPO_ROOT/packages/mindroom-nio/src/nio in place)
set -euo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"
cd "$1"
NIO=packages/mindroom-nio/src/nio
python3 -I "$HERE"/cut.py "$NIO" src/mindroom,tests --apply
uv run ruff check --isolated --select F401 --per-file-ignores "__init__.py:F401" --fix --unsafe-fixes -q "$NIO" || true
python3 -I "$HERE"/prune_imports.py "$PWD/.venv/bin/python" "$PWD/packages/mindroom-nio/src" nio,nio.durable,nio.crypto,nio.store,nio.event_builders | tail -1
uv run ruff format -q "$NIO"
