"""Shared source-admission outcome vocabulary."""

from __future__ import annotations

from enum import StrEnum


class DispatchSourceAdmission(StrEnum):
    """Typed outcome for one source event at the replay fence."""

    ACCEPTED = "accepted"
    COLD_HISTORY_FENCED = "cold_history_fenced"
    DECRYPT_NOTICE_FENCED = "decrypt_notice_fenced"
