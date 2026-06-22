"""Helpers for team history scope identifiers."""

from __future__ import annotations

import hashlib


def requester_scoped_team_scope_id(scope_id: str, requester_user_id: str) -> str:
    """Return a requester-partitioned variant of one team history scope id."""
    requester_digest = hashlib.sha256(requester_user_id.encode("utf-8")).hexdigest()[:12]
    return f"{scope_id}_requester_{requester_digest}"
