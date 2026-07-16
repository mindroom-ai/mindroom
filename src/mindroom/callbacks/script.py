"""Ready-to-run callback script generation for agent workspaces."""

from __future__ import annotations

import os
import shlex
from contextlib import suppress
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from pathlib import Path

_GITIGNORE_CONTENT = "*\n"


def build_callback_script(*, label: str, callback_url: str, token: str, expires_at_text: str) -> str:
    """Render one self-contained callback script that needs only bash and curl."""
    return f"""#!/usr/bin/env bash
# MindRoom callback — expires {expires_at_text}
# Usage: bash <this script> [done|failed|blocked|progress] [message words...]
set -euo pipefail
CALLBACK_LABEL={shlex.quote(label)}
CALLBACK_URL={shlex.quote(callback_url)}
CALLBACK_TOKEN={shlex.quote(token)}
STATUS="${{1:-done}}"
shift || true
MSG="${{*:-(no message)}}"

json_escape() {{
  local value="$1"
  local escaped=""
  local character code
  local i
  for ((i = 0; i < ${{#value}}; i++)); do
    character="${{value:i:1}}"
    case "$character" in
      '"') escaped+='\\"' ;;
      '\\') escaped+='\\\\' ;;
      $'\b') escaped+='\\b' ;;
      $'\f') escaped+='\\f' ;;
      $'\n') escaped+='\\n' ;;
      $'\r') escaped+='\\r' ;;
      $'\t') escaped+='\\t' ;;
      *)
        printf -v code '%d' "'$character"
        if ((code < 32)); then
          printf -v character '\\u%04x' "$code"
        fi
        escaped+="$character"
        ;;
    esac
  done
  printf '%s' "$escaped"
}}

BODY=$(printf '{{"status":"%s","message":"%s"}}' "$(json_escape "$STATUS")" "$(json_escape "$MSG")")
if curl -fsS -X POST "$CALLBACK_URL" \\
  -H "Authorization: Bearer $CALLBACK_TOKEN" \\
  -H 'Content-Type: application/json' \\
  --data "$BODY"; then
  echo
  echo "OK: MindRoom notified ($CALLBACK_LABEL)"
else
  echo "FAILED: could not notify MindRoom ($CALLBACK_LABEL); the callback may be expired or already used." >&2
  exit 1
fi
"""


def write_callback_script(callbacks_dir: Path, *, callback_id: str, script_text: str) -> Path:
    """Create one callback script as mode 0700 and keep the directory gitignored."""
    callbacks_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    gitignore_path = callbacks_dir / ".gitignore"
    if not gitignore_path.exists():
        gitignore_path.write_text(_GITIGNORE_CONTENT, encoding="utf-8")
    script_path = callbacks_dir / f"{callback_id}.sh"
    descriptor = os.open(script_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o700)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as script_file:
            script_file.write(script_text)
    except (OSError, UnicodeError):
        with suppress(OSError):
            script_path.unlink(missing_ok=True)
        raise
    return script_path
