"""Regex patterns derived from fixed tool-output templates, so a formatter and its parser share one source."""

from __future__ import annotations

import re

INT_FIELD = r"-?\d+"
FLOAT_FIELD = r"-?\d+(?:\.\d+)?"
TEXT_FIELD = r"[\s\S]*"


def template_pattern(template: str, **fields: str) -> re.Pattern[str]:
    """Return a full-match pattern for a ``str.format`` *template* whose ``{name}`` fields become named groups.

    A field repeated in the template must repeat the same text.
    """
    pattern = re.escape(template)
    for name, field_pattern in fields.items():
        first, *rest = pattern.split(re.escape(f"{{{name}}}"))
        pattern = f"(?P<{name}>{field_pattern})".join([first, f"(?P={name})".join(rest)]) if rest else first
    return re.compile(pattern)
