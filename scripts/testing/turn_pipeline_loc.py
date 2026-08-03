"""Report physical source lines for the checked turn-pipeline manifest.

The manifest at ``docs/architecture/turn-pipeline-manifest.txt`` is the exact
production file boundary for the turn-pipeline lifecycle refactor tracked in
``docs/superpowers/plans/2026-08-02-turn-pipeline-lifecycle-refactor.md``.
Run with ``uv run python scripts/testing/turn_pipeline_loc.py``.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
MANIFEST_PATH = REPO_ROOT / "docs" / "architecture" / "turn-pipeline-manifest.txt"

_GROUP_PREFIX = "# group: "


@dataclass(frozen=True)
class ManifestGroup:
    """One named group of pipeline files."""

    name: str
    files: tuple[str, ...]


def read_manifest(manifest_path: Path = MANIFEST_PATH) -> tuple[ManifestGroup, ...]:
    """Parse the checked manifest into ordered groups of repo-relative paths."""
    groups: list[ManifestGroup] = []
    name: str | None = None
    files: list[str] = []
    for raw_line in manifest_path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line:
            continue
        if line.startswith(_GROUP_PREFIX):
            if name is not None:
                groups.append(ManifestGroup(name, tuple(files)))
            name = line[len(_GROUP_PREFIX) :].strip()
            files = []
            continue
        if line.startswith("#"):
            continue
        files.append(line)
    if name is not None:
        groups.append(ManifestGroup(name, tuple(files)))
    return tuple(groups)


def count_lines(relative_path: str, repo_root: Path = REPO_ROOT) -> int:
    """Count physical lines in one manifest file."""
    return len((repo_root / relative_path).read_text(encoding="utf-8").splitlines())


def main() -> None:
    """Print per-group subtotals and the total boundary size."""
    total = 0
    for group in read_manifest():
        subtotal = sum(count_lines(path) for path in group.files)
        total += subtotal
        print(f"{group.name}: {len(group.files)} files, {subtotal} lines")
    print(f"total: {total} lines")


if __name__ == "__main__":
    main()
