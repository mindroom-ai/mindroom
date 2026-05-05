"""Tests for Matrix delivery configuration."""

from __future__ import annotations

from typing import Any, cast
from unittest.mock import AsyncMock, MagicMock

import nio
import pytest

from mindroom.config.main import Config
from mindroom.matrix.client_delivery import send_message_result


def _mock_client(*, encrypted: bool = False) -> AsyncMock:
    """Create a mock Matrix client with one room."""
    client = AsyncMock(spec=nio.AsyncClient)
    room = MagicMock()
    room.encrypted = encrypted
    client.rooms = {"!room:localhost": room}
    client.room_send.return_value = nio.RoomSendResponse(event_id="$event:localhost", room_id="!room:localhost")
    return client


def test_matrix_delivery_default_keeps_device_trust_policy() -> None:
    """Matrix delivery should not ignore unverified devices by default."""
    config = Config()

    assert config.matrix_delivery.ignore_unverified_devices is False


def test_matrix_delivery_yaml_opt_in(tmp_path) -> None:  # noqa: ANN001
    """Operators should be able to explicitly opt in to ignoring unverified devices."""
    config_path = tmp_path / "config.yaml"
    config_path.write_text(
        "matrix_delivery:\n  ignore_unverified_devices: true\n",
        encoding="utf-8",
    )

    config = Config.from_yaml(config_path)

    assert config.matrix_delivery.ignore_unverified_devices is True


@pytest.mark.asyncio
async def test_send_message_result_defaults_ignore_unverified_devices_to_false() -> None:
    """Direct Matrix delivery should pass the safe nio default explicitly."""
    client = _mock_client()

    await send_message_result(client, "!room:localhost", {"body": "hello", "msgtype": "m.text"}, config=Config())

    assert client.room_send.await_args.kwargs["ignore_unverified_devices"] is False


@pytest.mark.asyncio
async def test_send_message_result_requires_config() -> None:
    """Delivery helpers should not weaken Matrix trust policy behind an optional config fallback."""
    client = _mock_client()
    unchecked_send_message_result = cast("Any", send_message_result)

    with pytest.raises(TypeError):
        await unchecked_send_message_result(client, "!room:localhost", {"body": "hello", "msgtype": "m.text"})


@pytest.mark.asyncio
async def test_send_message_result_passes_matrix_delivery_opt_in_to_room_send() -> None:
    """The Matrix delivery config opt-in should reach nio room_send."""
    client = _mock_client()
    config = Config(matrix_delivery={"ignore_unverified_devices": True})

    await send_message_result(
        client,
        "!room:localhost",
        {"body": "hello", "msgtype": "m.text"},
        config=config,
    )

    assert client.room_send.await_args.kwargs["ignore_unverified_devices"] is True


@pytest.mark.asyncio
async def test_send_message_result_passes_matrix_delivery_opt_in_to_encrypted_room_send() -> None:
    """The Matrix delivery opt-in should reach nio for encrypted room sends."""
    client = _mock_client(encrypted=True)
    config = Config(matrix_delivery={"ignore_unverified_devices": True})

    await send_message_result(
        client,
        "!room:localhost",
        {"body": "hello", "msgtype": "m.text"},
        config=config,
    )

    assert client.room_send.await_args.kwargs["ignore_unverified_devices"] is True
