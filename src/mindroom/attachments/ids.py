"""Leaf helpers for attachment ID lists.

Kept free of matrix-client imports so the tool registry chain can dedupe
attachment IDs without dragging in ``nio`` (#1436).
"""

from __future__ import annotations

import re
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Iterable

_ATTACHMENT_ID_PATTERN = re.compile(r"^[A-Za-z0-9_][A-Za-z0-9_-]{0,127}$")


def normalize_attachment_id(raw_attachment_id: str) -> str | None:
    """Normalize attachment IDs and reject unsafe values."""
    attachment_id = raw_attachment_id.strip()
    if not attachment_id or not _ATTACHMENT_ID_PATTERN.fullmatch(attachment_id):
        return None
    return attachment_id


def unique_attachment_ids(attachment_ids: Iterable[str]) -> list[str]:
    """Return unique non-empty attachment IDs preserving first-seen order."""
    unique_ids: list[str] = []
    seen_attachment_ids: set[str] = set()
    for attachment_id in attachment_ids:
        if attachment_id and attachment_id not in seen_attachment_ids:
            seen_attachment_ids.add(attachment_id)
            unique_ids.append(attachment_id)
    return unique_ids


def merge_attachment_ids(*attachment_id_lists: list[str]) -> list[str]:
    """Merge attachment IDs preserving first-seen order."""
    return unique_attachment_ids(
        attachment_id for attachment_ids in attachment_id_lists for attachment_id in attachment_ids
    )
