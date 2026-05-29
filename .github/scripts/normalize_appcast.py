"""Normalize generated Sparkle appcast files for repository hooks."""

from __future__ import annotations

import argparse
from pathlib import Path


def normalize_appcast_file(path: Path) -> None:
    """Use LF line endings and exactly one final newline."""
    text = path.read_text(encoding="utf-8")
    path.write_text(text.rstrip("\n") + "\n", encoding="utf-8", newline="\n")


def main() -> None:
    """Run the command-line appcast normalizer."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("appcast", type=Path, help="Path to the generated appcast XML.")
    args = parser.parse_args()

    normalize_appcast_file(args.appcast)


if __name__ == "__main__":
    main()
