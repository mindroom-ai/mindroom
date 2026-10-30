"""What an approval card's prepared payload lets a reviewer see."""

from __future__ import annotations

from typing import Any

from mindroom.matrix.large_messages import sidecar_upload_is_usable


def card_approvable(content: dict[str, Any]) -> bool:
    """Return whether an approval of a prepared card can be honored: a reviewer can see every argument.

    The journal stores this when the card is reserved, so a click never reads the card's payload to decide.
    """
    approvable = content.get("approvable", True)
    if approvable is not True:
        return False
    return not content.get("arguments_truncated") or _full_arguments_available(content)


def _full_arguments_available(content: dict[str, Any]) -> bool:
    """Return whether one card delivers the complete arguments inline or via a sidecar."""
    full_arguments = content.get("full_arguments")
    if isinstance(full_arguments, dict) and bool(full_arguments):
        return True
    encrypted_file = content.get("full_arguments_file")
    encrypted_url = encrypted_file.get("url") if isinstance(encrypted_file, dict) else None
    if sidecar_upload_is_usable(encrypted_url, encrypted_file, room_encrypted=True):
        return True
    # LEGACY_COMPAT: Approval cards whose oversized arguments sit in a plaintext sidecar.
    # Legacy format: `full_arguments_url` plus `full_arguments_info`, written for cards in unencrypted rooms.
    # Last legacy release: v2026.10.705; replacement: the next release always writes an encrypted `full_arguments_file`.
    # Handling: such a card stays approvable when its plaintext sidecar is usable, as when it was sent.
    # Coverage: tests/test_tool_approval.py::test_card_approvable_reads_sidecar_full_arguments_availability.
    url = content.get("full_arguments_url")
    file_info = content.get("full_arguments_info")
    return sidecar_upload_is_usable(url, file_info, room_encrypted=False)
