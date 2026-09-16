"""Conservative recovery of Chromium persistent profile locks."""

import os
import re
from pathlib import Path

from mindroom.logging_config import get_logger

logger = get_logger(__name__)


def clear_stale_singleton_locks(profile_dir: Path) -> None:
    """Best-effort cleanup for stale Chromium singleton lock symlinks."""
    for entry_name in ("SingletonLock", "SingletonCookie", "SingletonSocket"):
        entry = profile_dir / entry_name
        try:
            if not entry.is_symlink():
                continue
            target = entry.readlink()
            match = re.fullmatch(r".+-(\d+)", target.name)
            if match is None:
                continue
            pid = int(match.group(1))
            try:
                os.kill(pid, 0)
            except ProcessLookupError:
                entry.unlink()
        except OSError as exc:
            logger.warning(
                "Failed to clean Chromium singleton lock",
                entry=str(entry),
                error=str(exc),
            )
