"""Shared report access-policy types."""

from __future__ import annotations

from enum import StrEnum


class ReportAccessPolicy(StrEnum):
    """Supported access policies for published reports."""

    PUBLIC = "public"
    ORIGIN_ROOM = "origin_room"
