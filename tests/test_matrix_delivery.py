"""Tests for Matrix delivery configuration."""

from __future__ import annotations

from typing import Any, cast
from unittest.mock import AsyncMock, MagicMock

import nio
import pytest

from mindroom.config.main import Config
from mindroom.matrix.client_delivery import send_message_result
from tests.conftest import load_config_yaml


def _mock_client(*, encrypted: bool = False) -> AsyncMock:
    """Create a mock Matrix client with one room."""
    client = AsyncMock(spec=nio.AsyncClient)
    room = MagicMock()
    room.encrypted = encrypted
    client.rooms = {"!room:localhost": room}
    client.room_send.return_value = nio.RoomSendResponse(event_id="$event:localhost", room_id="!room:localhost")
    return client


def test_matrix_delivery_default_ignores_unverified_devices() -> None:
    """Matrix delivery should ignore unverified devices by default.

    Bots have no interactive device-verification flow, so enforcing device
    trust would fail every encrypted-room send with OlmUnverifiedDeviceError.
    """
    config = Config()

    assert config.matrix_delivery.ignore_unverified_devices is True


def test_matrix_delivery_yaml_opt_out(tmp_path) -> None:  # noqa: ANN001
    """Operators should be able to explicitly opt in to strict device-trust checks."""
    config_path = tmp_path / "config.yaml"
    config_path.write_text(
        "matrix_delivery:\n  ignore_unverified_devices: false\n",
        encoding="utf-8",
    )

    config = load_config_yaml(config_path)

    assert config.matrix_delivery.ignore_unverified_devices is False


@pytest.mark.asyncio
async def test_send_message_result_defaults_ignore_unverified_devices_to_true() -> None:
    """Direct Matrix delivery should pass the bot-friendly default explicitly."""
    client = _mock_client()

    await send_message_result(client, "!room:localhost", {"body": "hello", "msgtype": "m.text"}, config=Config())

    assert client.room_send.await_args.kwargs["ignore_unverified_devices"] is True


@pytest.mark.asyncio
async def test_send_message_result_requires_config() -> None:
    """Delivery helpers should not weaken Matrix trust policy behind an optional config fallback."""
    client = _mock_client()
    unchecked_send_message_result = cast("Any", send_message_result)

    with pytest.raises(TypeError):
        await unchecked_send_message_result(client, "!room:localhost", {"body": "hello", "msgtype": "m.text"})


@pytest.mark.asyncio
async def test_send_message_result_passes_matrix_delivery_opt_out_to_room_send() -> None:
    """The strict device-trust opt-out should reach nio room_send."""
    client = _mock_client()
    config = Config(matrix_delivery={"ignore_unverified_devices": False})

    await send_message_result(
        client,
        "!room:localhost",
        {"body": "hello", "msgtype": "m.text"},
        config=config,
    )

    assert client.room_send.await_args.kwargs["ignore_unverified_devices"] is False


@pytest.mark.asyncio
async def test_send_message_result_passes_matrix_delivery_opt_out_to_encrypted_room_send() -> None:
    """The strict device-trust opt-out should reach nio for encrypted room sends."""
    client = _mock_client(encrypted=True)
    config = Config(matrix_delivery={"ignore_unverified_devices": False})

    await send_message_result(
        client,
        "!room:localhost",
        {"body": "hello", "msgtype": "m.text"},
        config=config,
    )

    assert client.room_send.await_args.kwargs["ignore_unverified_devices"] is False
