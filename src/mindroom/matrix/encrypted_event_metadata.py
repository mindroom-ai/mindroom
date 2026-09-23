"""Cleartext metadata intentionally exposed on encrypted Matrix events."""

from collections.abc import Mapping
from typing import Any

from mindroom.constants import (
    CONFIG_CONFIRMATION_REACTION_KEY,
    STREAM_STATUS_KEY,
    VISIBLE_ROUTER_VOICE_ECHO_KEY,
)


def encryption_visible_metadata(content: Mapping[Any, Any]) -> dict[str, str | bool]:
    """Return metadata copied outside the encrypted event payload.

    This is the single source of truth for both real encryption and preflight
    size estimation. Keeping the allowlist here prevents durable payloads from
    passing preparation with an envelope smaller than the one sent on the wire.
    """
    visible_metadata: dict[str, str | bool] = {}
    stream_status = content.get(STREAM_STATUS_KEY)
    if isinstance(stream_status, str):
        visible_metadata[STREAM_STATUS_KEY] = stream_status
    if content.get(VISIBLE_ROUTER_VOICE_ECHO_KEY) is True:
        visible_metadata[VISIBLE_ROUTER_VOICE_ECHO_KEY] = True
    config_reaction_id = content.get(CONFIG_CONFIRMATION_REACTION_KEY)
    if isinstance(config_reaction_id, str):
        visible_metadata[CONFIG_CONFIRMATION_REACTION_KEY] = config_reaction_id
    return visible_metadata
