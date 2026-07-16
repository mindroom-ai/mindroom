"""Ready-to-run callback script generation for agent workspaces."""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from pathlib import Path

_GITIGNORE_CONTENT = "*\n"


def _shell_safe(text: str) -> str:
    """Strip characters that would break the generated script's quoting."""
    return "".join(character for character in text if character.isprintable() and character not in "'\"`$\\")


def build_callback_script(*, label: str, callback_url: str, token: str, expires_at_text: str) -> str:
    """Render one self-contained bash callback script (bash + curl, python3 for JSON only)."""
    safe_label = _shell_safe(label)
    return f"""#!/usr/bin/env bash
# MindRoom callback '{safe_label}' — expires {expires_at_text}
# Usage: bash <this script> [done|failed|blocked|progress] [message words...]
set -euo pipefail
STATUS="${{1:-done}}"
shift || true
MSG="${{*:-(no message)}}"
BODY=$(python3 - "$STATUS" "$MSG" <<'PYEOF'
import json, sys
print(json.dumps({{"status": sys.argv[1], "message": sys.argv[2]}}))
PYEOF
)
if curl -fsS -X POST '{callback_url}' \\
  -H 'Authorization: Bearer {token}' \\
  -H 'Content-Type: application/json' \\
  --data "$BODY"; then
  echo
  echo 'OK: MindRoom notified ({safe_label})'
else
  echo 'FAILED: could not notify MindRoom ({safe_label}); the callback may be expired or already used.' >&2
  exit 1
fi
"""


def write_callback_script(callbacks_dir: Path, *, callback_id: str, script_text: str) -> Path:
    """Write one callback script (mode 0700) and keep the directory gitignored."""
    callbacks_dir.mkdir(parents=True, exist_ok=True)
    gitignore_path = callbacks_dir / ".gitignore"
    if not gitignore_path.exists():
        gitignore_path.write_text(_GITIGNORE_CONTENT, encoding="utf-8")
    script_path = callbacks_dir / f"{callback_id}.sh"
    script_path.write_text(script_text, encoding="utf-8")
    script_path.chmod(0o700)
    return script_path
