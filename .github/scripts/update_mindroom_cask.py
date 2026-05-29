"""Update the MindRoom Homebrew cask version and checksum."""

from __future__ import annotations

import argparse
import re
from pathlib import Path

VERSION_RE = re.compile(r'^(?P<indent>\s*)version "[^"]+"$')
SHA256_RE = re.compile(r'^(?P<indent>\s*)sha256 "[0-9a-f]{64}"$')


def _validate_sha256(value: str) -> str:
    if not re.fullmatch(r"[0-9a-f]{64}", value):
        msg = "sha256 must be 64 lowercase hexadecimal characters"
        raise ValueError(msg)
    return value


def _validate_version(value: str) -> str:
    value = value.removeprefix("v")
    if not re.fullmatch(r"\d+(?:\.\d+)*(?:[-,._A-Za-z0-9]+)?", value):
        msg = f"unsupported cask version: {value}"
        raise ValueError(msg)
    return value


def update_cask_text(text: str, *, version: str, sha256: str) -> str:
    """Replace the single version and sha256 stanzas in a cask."""
    version = _validate_version(version)
    sha256 = _validate_sha256(sha256)

    version_count = 0
    sha256_count = 0
    lines: list[str] = []
    for source_line in text.splitlines(keepends=True):
        newline = "\n" if source_line.endswith("\n") else ""
        content = source_line.removesuffix("\n")

        if match := VERSION_RE.match(content):
            output_line = f'{match.group("indent")}version "{version}"{newline}'
            version_count += 1
        elif match := SHA256_RE.match(content):
            output_line = f'{match.group("indent")}sha256 "{sha256}"{newline}'
            sha256_count += 1
        else:
            output_line = source_line
        lines.append(output_line)

    if version_count != 1:
        msg = f"expected exactly one version stanza, found {version_count}"
        raise ValueError(msg)
    if sha256_count != 1:
        msg = f"expected exactly one sha256 stanza, found {sha256_count}"
        raise ValueError(msg)

    return "".join(lines)


def update_cask_file(path: Path, *, version: str, sha256: str) -> None:
    """Update a cask file in place."""
    path.write_text(
        update_cask_text(path.read_text(), version=version, sha256=sha256),
        encoding="utf-8",
    )


def main() -> None:
    """Run the command-line cask updater."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--cask",
        type=Path,
        default=Path("Casks/mindroom.rb"),
        help="Path to the cask file to update.",
    )
    parser.add_argument("--version", required=True, help="Release version, with or without v prefix.")
    parser.add_argument("--sha256", required=True, help="SHA-256 checksum for the release DMG.")
    args = parser.parse_args()

    update_cask_file(args.cask, version=args.version, sha256=args.sha256)


if __name__ == "__main__":
    main()
