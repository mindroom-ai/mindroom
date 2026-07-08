"""Tests for agent cross-signing bootstrap at login."""

from __future__ import annotations

from typing import TYPE_CHECKING
from unittest.mock import AsyncMock, MagicMock, patch

import nio
import pytest

from mindroom.matrix import cross_signing
from mindroom.matrix.cross_signing import cross_signing_status_line, ensure_agent_cross_signing
from mindroom.matrix.users import AgentMatrixUser

if TYPE_CHECKING:
    from pathlib import Path


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
    client.cross_signing_identity = None
    client.ensure_cross_signing.side_effect = nio.exceptions.LocalProtocolError("rejected")

    await ensure_agent_cross_signing(client, _agent_user())  # no exception


def _client_with_uploaded_identity(tmp_path: Path) -> tuple[AsyncMock, MagicMock]:
    client = AsyncMock(spec=nio.AsyncClient)
    client.user_id = "@mindroom_assistant:localhost"
    client.device_id = "DEVICEID"
    client.olm = MagicMock()
    client.store_path = str(tmp_path)
    identity = MagicMock()
    identity.uploaded = True
    identity.signed_devices = ["OLDDEVICE"]
    identity.master_public_key = "LOCALKEY"
    client.cross_signing_identity = identity
    return client, identity


@pytest.mark.asyncio
async def test_recovery_reuploads_when_server_lost_identity(tmp_path: Path) -> None:
    """A sidecar marked uploaded must re-upload once when the server has no master key."""
    client, identity = _client_with_uploaded_identity(tmp_path)
    client.ensure_cross_signing.side_effect = [
        nio.exceptions.LocalProtocolError("Device signature upload failed"),
        "uploaded_and_signed",
    ]

    with patch.object(cross_signing, "_server_master_public_key", new=AsyncMock(return_value=None)):
        await ensure_agent_cross_signing(client, _agent_user())

    assert client.ensure_cross_signing.await_count == 2
    assert identity.uploaded is False
    assert identity.signed_devices == []
    identity.save.assert_called_once()


@pytest.mark.asyncio
async def test_no_reupload_when_server_still_has_identity(tmp_path: Path) -> None:
    """A matching server master key means the failure was not identity loss."""
    client, identity = _client_with_uploaded_identity(tmp_path)
    client.ensure_cross_signing.side_effect = nio.exceptions.LocalProtocolError("transient")

    with patch.object(cross_signing, "_server_master_public_key", new=AsyncMock(return_value="LOCALKEY")):
        await ensure_agent_cross_signing(client, _agent_user())

    assert client.ensure_cross_signing.await_count == 1
    identity.save.assert_not_called()


@pytest.mark.asyncio
async def test_recovery_failure_still_does_not_raise(tmp_path: Path) -> None:
    """A failing re-upload must degrade to a warning, never break startup."""
    client, _identity = _client_with_uploaded_identity(tmp_path)
    client.ensure_cross_signing.side_effect = [
        nio.exceptions.LocalProtocolError("Device signature upload failed"),
        nio.exceptions.LocalProtocolError("still rejected"),
    ]

    with patch.object(cross_signing, "_server_master_public_key", new=AsyncMock(return_value=None)):
        await ensure_agent_cross_signing(client, _agent_user())  # no exception

    assert client.ensure_cross_signing.await_count == 2


@pytest.mark.asyncio
async def test_server_master_key_parsed_from_keys_query() -> None:
    """The server key check reads master_keys from /keys/query."""
    client = AsyncMock(spec=nio.AsyncClient)
    client.user_id = "@mindroom_assistant:localhost"
    client.access_token = "token"  # noqa: S105
    response = MagicMock()
    response.status = 200
    response.json = AsyncMock(
        return_value={
            "master_keys": {
                "@mindroom_assistant:localhost": {"keys": {"ed25519:SERVERKEY": "SERVERKEY"}},
            },
        },
    )
    client.send.return_value = response

    assert await cross_signing._server_master_public_key(client) == "SERVERKEY"

    response.json = AsyncMock(return_value={})
    assert await cross_signing._server_master_public_key(client) is None


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


def test_status_line_keys_present_but_device_unsigned() -> None:
    """Keys uploaded for another device must not report this device as active."""
    client = MagicMock(spec=nio.AsyncClient)
    client.device_id = "DEVICEID"
    identity = MagicMock()
    identity.signed_devices = ["OTHERDEVICE"]
    identity.master_public_key = "MASTERKEY"
    client.cross_signing_identity = identity

    line = cross_signing_status_line(client)
    assert "not yet self-signed" in line
    assert "active" not in line
