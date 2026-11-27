"""Shared fixtures for Slack tests."""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

from mindroom.slack import app as slack_app
from mindroom.slack import identity as slack_identity

if TYPE_CHECKING:
    from collections.abc import Iterator


@pytest.fixture(autouse=True)
def clear_slack_caches() -> Iterator[None]:
    """Keep Slack app identity, profile, and channel caches isolated across tests."""
    slack_app._IDENTITIES.clear()
    slack_identity._PROFILES.clear()
    slack_identity._CHANNELS.clear()
    yield
    slack_app._IDENTITIES.clear()
    slack_identity._PROFILES.clear()
    slack_identity._CHANNELS.clear()
