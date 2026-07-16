"""Ready-to-run callback script generation."""

from __future__ import annotations

import os
import shlex
from contextlib import suppress
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from pathlib import Path

_GITIGNORE_CONTENT = "*\n"


def build_callback_script(*, callback_url: str, token: str) -> str:
    """Render a callback script that needs only Bash and curl."""
    return f"""#!/usr/bin/env bash
# Usage: bash <this script> "<short result summary>"
set -euo pipefail
CALLBACK_URL={shlex.quote(callback_url)}
CALLBACK_TOKEN={shlex.quote(token)}
MESSAGE="${{*:-Background task finished.}}"

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

BODY=$(printf '{{"message":"%s"}}' "$(json_escape "$MESSAGE")")
if curl -fsS --connect-timeout 10 --max-time 60 -X POST "$CALLBACK_URL" \\
  -H "Authorization: Bearer $CALLBACK_TOKEN" \\
  -H 'Content-Type: application/json' \\
  --data "$BODY"; then
  rm -f -- "$0"
  echo "MindRoom notified."
else
  echo "Could not notify MindRoom; retry this script later." >&2
  exit 1
fi
"""


def write_callback_script(callbacks_dir: Path, *, callback_id: str, script_text: str) -> Path:
    """Create one mode-0700 callback script in a gitignored directory."""
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
