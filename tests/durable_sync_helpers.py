"""Stubs for running nio's durable sync source against a fake homeserver."""

from __future__ import annotations

from typing import TYPE_CHECKING
from unittest.mock import AsyncMock

if TYPE_CHECKING:
    from nio.durable import DurableSync


def skip_crypto_maintenance(session: DurableSync) -> None:
    """Give the session no device-key work, so a fake homeserver needs no key endpoints.

    Stubbing the maintenance call alone is not enough: nio retains the next due key request while it prepares a
    sync, and its runner keeps calling maintenance instead of polling until that request is sent.
    """
    session._maintain_crypto = AsyncMock()
    session._crypto.next_request = lambda **_kwargs: None
