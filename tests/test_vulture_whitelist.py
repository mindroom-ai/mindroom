"""Keep vulture_whitelist.py free of entries vulture no longer needs."""

import tomllib
from pathlib import Path

from vulture import Vulture

REPO_ROOT = Path(__file__).resolve().parents[1]
WHITELIST = REPO_ROOT / "vulture_whitelist.py"


def _whitelist_names() -> set[str]:
    names = set()
    for line in WHITELIST.read_text().splitlines():
        entry = line.strip()
        if entry and not entry.startswith("#"):
            names.add(entry.split()[0].removeprefix("_."))
    return names


def _names_vulture_reports_without_whitelist() -> set[str]:
    settings = tomllib.loads((REPO_ROOT / "pyproject.toml").read_text())["tool"]["vulture"]
    vulture = Vulture()
    vulture.scavenge([REPO_ROOT / path for path in settings["paths"] if path != WHITELIST.name])
    return {item.name for item in vulture.get_unused_code(min_confidence=settings["min_confidence"])}


def test_every_whitelist_entry_is_still_reported_by_vulture() -> None:
    """Vulture never fails on an entry it does not need, so this test catches stale ones."""
    stale = sorted(_whitelist_names() - _names_vulture_reports_without_whitelist())
    assert not stale, (
        f"vulture no longer reports these vulture_whitelist.py entries as unused; delete them: {', '.join(stale)}"
    )
