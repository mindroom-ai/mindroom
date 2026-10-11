"""Ready-to-run callback script generation."""

from __future__ import annotations

import shlex
from contextlib import suppress
from typing import TYPE_CHECKING

from mindroom.path_confinement import write_file_within_root

if TYPE_CHECKING:
    from pathlib import Path

_GITIGNORE_CONTENT = "*\n"


def build_callback_script(*, callback_url: str, token: str, label: str) -> str:
    """Render a callback script that needs only Bash and curl."""
    return f"""#!/usr/bin/env bash
# Usage: bash <this script> "<short result summary>"
set -euo pipefail
CALLBACK_URL={shlex.quote(callback_url)}
CALLBACK_TOKEN={shlex.quote(token)}
CALLBACK_LABEL={shlex.quote(label)}
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

BODY=$(printf '{{"kind":"mindroom.callback.completed","title":"✅ %s","message":"%s"}}' \\
  "$(json_escape "$CALLBACK_LABEL")" "$(json_escape "$MESSAGE")")
if printf 'Authorization: Bearer %s\n' "$CALLBACK_TOKEN" | \\
  curl -fsS --connect-timeout 10 --max-time 60 -X POST "$CALLBACK_URL" \\
  -H @- \\
  -H 'Content-Type: application/json' \\
  -o /dev/null \\
  --data "$BODY"; then
  rm -f -- "$0"
  echo "MindRoom notified."
else
  echo "Could not notify MindRoom; retry this script later." >&2
  exit 1
fi
"""


def write_callback_script(workspace_root: Path, callbacks_dir: Path, *, callback_id: str, script_text: str) -> Path:
    """Create one mode-0700 callback script in a gitignored directory of one workspace."""
    script_path = callbacks_dir / f"{callback_id}.sh"
    with suppress(FileExistsError):
        write_file_within_root(
            workspace_root,
            callbacks_dir / ".gitignore",
            _GITIGNORE_CONTENT.encode("utf-8"),
            file_mode=0o644,
            dir_mode=0o700,
            exclusive=True,
        )
    write_file_within_root(
        workspace_root,
        script_path,
        script_text.encode("utf-8"),
        file_mode=0o700,
        dir_mode=0o700,
        exclusive=True,
    )
    return workspace_root / script_path
