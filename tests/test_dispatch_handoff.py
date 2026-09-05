"""Tests for typed ingress payload metadata."""

from __future__ import annotations

import pytest

from mindroom.dispatch_handoff import payload_metadata_from_source


@pytest.mark.parametrize("user_ids", ["@agent:localhost", None, 7])
def test_payload_metadata_ignores_malformed_mention_user_ids(user_ids: object) -> None:
    """Malformed Matrix mention containers must not become user IDs or crash dispatch."""
    metadata = payload_metadata_from_source(
        {"content": {"m.mentions": {"user_ids": user_ids}}},
        trust_internal_metadata=False,
    )

    assert metadata.mentioned_user_ids == ()
