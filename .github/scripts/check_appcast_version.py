"""Report the newest Sparkle appcast version strictly greater than a target tag."""

from __future__ import annotations

import argparse
import sys
from xml.etree import ElementTree as ET

SPARKLE_NS = "http://www.andymatuschak.org/xml-namespaces/sparkle"


def parse_version(raw: str) -> tuple[int, ...] | None:
    """Parse a dotted numeric version string (with optional leading 'v') into a tuple."""
    parts = raw.strip().removeprefix("v").split(".")
    if not parts or not all(part.isascii() and part.isdigit() for part in parts):
        return None
    return tuple(int(part) for part in parts)


def find_newer_version(xml_text: str, target_tag: str) -> str | None:
    """Return the highest appcast shortVersionString strictly newer than target_tag."""
    target = parse_version(target_tag)
    if target is None:
        return None
    try:
        root = ET.fromstring(xml_text)  # noqa: S314 - parses repository-controlled Sparkle appcast XML
    except ET.ParseError:
        return None

    newer: list[tuple[tuple[int, ...], str]] = []
    for elem in root.findall(f".//item/{{{SPARKLE_NS}}}shortVersionString"):
        version_text = (elem.text or "").strip()
        parsed = parse_version(version_text)
        if parsed is not None and parsed > target:
            newer.append((parsed, version_text))
    return max(newer, key=lambda item: item[0])[1] if newer else None


def main() -> None:
    """Read appcast XML from stdin and print any version strictly newer than --newer-than."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--newer-than", required=True, help="Target release tag to compare against.")
    args = parser.parse_args()

    newer = find_newer_version(sys.stdin.read(), args.newer_than)
    if newer is not None:
        print(newer)


if __name__ == "__main__":
    main()
