"""Tests for agent cross-signing bootstrap at login."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import nio
import pytest

from mindroom.matrix.cross_signing import cross_signing_status_line, ensure_agent_cross_signing
from mindroom.matrix.users import AgentMatrixUser


def _agent_user() -> AgentMatrixUser:
    return AgentMatrixUser(
        agent_name="assistant",
        user_id="@mindroom_assistant:localhost",
        display_name="Assistant",
        password="pw",  # noqa: S106
    )


@pytest.mark.asyncio
async def test_bootstrap_calls_ensure_with_password() -> None:
    """Cross-signing bootstrap forwards the agent password for UIA."""
    client = AsyncMock(spec=nio.AsyncClient)
    client.user_id = "@mindroom_assistant:localhost"
    client.device_id = "DEVICEID"
    client.olm = MagicMock()
    client.ensure_cross_signing.return_value = "uploaded_and_signed"

    await ensure_agent_cross_signing(client, _agent_user())

    client.ensure_cross_signing.assert_awaited_once_with(password="pw")  # noqa: S106


@pytest.mark.asyncio
async def test_bootstrap_skipped_without_olm() -> None:
    """No encryption support means no cross-signing attempt."""
    client = AsyncMock(spec=nio.AsyncClient)
    client.olm = None

    await ensure_agent_cross_signing(client, _agent_user())

    client.ensure_cross_signing.assert_not_awaited()


@pytest.mark.asyncio
async def test_bootstrap_failure_does_not_raise() -> None:
    """A homeserver rejection must not break startup."""
    client = AsyncMock(spec=nio.AsyncClient)
    client.user_id = "@mindroom_assistant:localhost"
    client.device_id = "DEVICEID"
    client.olm = MagicMock()
    client.ensure_cross_signing.side_effect = nio.exceptions.LocalProtocolError("rejected")

    await ensure_agent_cross_signing(client, _agent_user())  # no exception


def test_status_line_not_bootstrapped() -> None:
    """The status line reports the absence of a cross-signing identity."""
    client = MagicMock(spec=nio.AsyncClient)
    client.cross_signing_identity = None

    assert "not bootstrapped" in cross_signing_status_line(client)


def test_status_line_active_when_device_signed() -> None:
    """The status line reports an active identity once the device is signed."""
    client = MagicMock(spec=nio.AsyncClient)
    client.device_id = "DEVICEID"
    identity = MagicMock()
    identity.signed_devices = ["DEVICEID"]
    identity.master_public_key = "MASTERKEY"
    client.cross_signing_identity = identity

    line = cross_signing_status_line(client)
    assert "active" in line
    assert "MASTERKEY" in line
