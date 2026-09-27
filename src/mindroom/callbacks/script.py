"""Ready-to-run callback script generation."""

from __future__ import annotations

import os
import shlex
from contextlib import suppress
from typing import TYPE_CHECKING

from mindroom.path_confinement import open_directory_below_root, open_directory_within_root

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


def _create_new_file(directory_fd: int, name: str, text: str, mode: int) -> None:
    """Write a file that did not exist; ``O_EXCL`` also refuses a planted link, even a dangling one."""
    descriptor = os.open(name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, mode, dir_fd=directory_fd)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as output:
            output.write(text)
    except (OSError, UnicodeError):
        with suppress(OSError):
            os.unlink(name, dir_fd=directory_fd)
        raise


def write_callback_script(storage_root: Path, callbacks_dir: Path, *, callback_id: str, script_text: str) -> Path:
    """Create one mode-0700 callback script in a gitignored directory.

    The directory sits in a workspace sandbox workers can write, so it is reached
    by a no-follow walk from the trusted storage root.
    """
    with (
        open_directory_below_root(storage_root, callbacks_dir.parent, create=True) as parent_fd,
        open_directory_within_root(parent_fd, callbacks_dir.name, create=True, mode=0o700) as directory_fd,
    ):
        with suppress(FileExistsError):
            _create_new_file(directory_fd, ".gitignore", _GITIGNORE_CONTENT, 0o666)
        script_name = f"{callback_id}.sh"
        _create_new_file(directory_fd, script_name, script_text, 0o700)
    return callbacks_dir / script_name
