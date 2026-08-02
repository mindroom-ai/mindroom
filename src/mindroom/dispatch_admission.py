"""Shared source-admission outcome vocabulary."""

from __future__ import annotations

from enum import StrEnum


class DispatchCallbackKind(StrEnum):
    """Exact correctness-critical callback purposes."""

    MESSAGE = "message"
    MEDIA = "media"
    REACTION = "reaction"
    APPROVAL = "approval"
    INVITE = "invite"
    ROOM_LIFECYCLE = "room_lifecycle"
    REDACTION = "redaction"
    DECRYPTION_FAILURE = "decryption_failure"


class DispatchSourceAdmission(StrEnum):
    """Typed outcome for one source event at the replay fence."""

    ACCEPTED = "accepted"
    COLD_HISTORY_FENCED = "cold_history_fenced"
    DECRYPT_NOTICE_FENCED = "decrypt_notice_fenced"
