# ruff: noqa: ANN001, ANN002, ANN003, ANN202, D103, PT012, TC003

"""Startup catch-up replay tests."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import nio
import pytest

from mindroom.startup_catchup import catch_up_missed_user_messages
from tests.conftest import make_matrix_client_mock
from tests.test_bot_ready_hook import _agent_bot


def _bot(tmp_path: Path):
    bot = _agent_bot(tmp_path)
    bot.client = make_matrix_client_mock(user_id=bot.agent_user.user_id or "@mindroom_code:localhost")
    bot._on_message = AsyncMock()
    bot._on_media_message = AsyncMock()
    return bot


def _text_event(event_id: str, *, body: str = "hello", sender: str = "@user:localhost", relates_to=None):
    content: dict[str, object] = {"body": body, "msgtype": "m.text"}
    if relates_to is not None:
        content["m.relates_to"] = relates_to
    event = nio.RoomMessageText.from_dict(
        {
            "content": content,
            "event_id": event_id,
            "sender": sender,
            "origin_server_ts": 1,
            "room_id": "!room:localhost",
            "type": "m.room.message",
        },
    )
    assert isinstance(event, nio.RoomMessageText)
    return event


def _media_event(event_class: type, event_id: str, *, body: str, msgtype: str):
    event = event_class.from_dict(
        {
            "content": {"body": body, "msgtype": msgtype, "url": "mxc://localhost/test"},
            "event_id": event_id,
            "sender": "@user:localhost",
            "origin_server_ts": 1,
            "room_id": "!room:localhost",
            "type": "m.room.message",
        },
    )
    assert event.event_id == event_id
    return event


def _sync_response(*events: object, next_batch: str = "s_next"):
    response = MagicMock()
    response.__class__ = nio.SyncResponse
    response.next_batch = next_batch
    response.rooms = MagicMock(join={"!room:localhost": MagicMock(timeline=MagicMock(events=list(events)))})
    return response


async def _run_catchup(bot, *events: object, next_batch: str = "s_next", handled=None):
    bot._turn_store.is_handled = handled or (lambda _event_id: False)
    response = _sync_response(*events, next_batch=next_batch)
    with (
        patch("mindroom.startup_catchup.load_sync_token", return_value="s_prev"),
        patch("mindroom.startup_catchup.save_sync_token") as save_sync_token,
    ):
        bot.client.sync = AsyncMock(return_value=response)
        await catch_up_missed_user_messages(bot)
    return save_sync_token


@pytest.mark.asyncio
async def test_catchup_dispatches_missed_user_messages(tmp_path: Path) -> None:
    bot = _bot(tmp_path)
    await _run_catchup(bot, _text_event("$1"), _text_event("$2"), _text_event("$3"))
    assert [call.args[1].event_id for call in bot._on_message.await_args_list] == ["$1", "$2", "$3"]


@pytest.mark.asyncio
async def test_catchup_skips_already_responded(tmp_path: Path) -> None:
    bot = _bot(tmp_path)
    await _run_catchup(
        bot,
        _text_event("$1"),
        _text_event("$2"),
        _text_event("$3"),
        handled=lambda event_id: event_id == "$2",
    )
    assert [call.args[1].event_id for call in bot._on_message.await_args_list] == ["$1", "$3"]


@pytest.mark.asyncio
async def test_catchup_skips_bot_own_messages(tmp_path: Path) -> None:
    bot = _bot(tmp_path)
    await _run_catchup(bot, _text_event("$1", sender=bot.agent_user.user_id or ""))
    bot._on_message.assert_not_awaited()


@pytest.mark.asyncio
async def test_catchup_skips_commands(tmp_path: Path) -> None:
    bot = _bot(tmp_path)
    await _run_catchup(bot, _text_event("$1", body="!status"))
    bot._on_message.assert_not_awaited()


@pytest.mark.asyncio
async def test_catchup_skips_edits(tmp_path: Path) -> None:
    bot = _bot(tmp_path)
    await _run_catchup(bot, _text_event("$1", body="* updated", relates_to={"rel_type": "m.replace", "event_id": "$0"}))
    bot._on_message.assert_not_awaited()


@pytest.mark.asyncio
async def test_catchup_no_token_no_op(tmp_path: Path) -> None:
    bot = _bot(tmp_path)
    bot.client.sync = AsyncMock()
    with patch("mindroom.startup_catchup.load_sync_token", return_value=None):
        await catch_up_missed_user_messages(bot)
    bot.client.sync.assert_not_awaited()


@pytest.mark.asyncio
async def test_catchup_sync_error_raises(tmp_path: Path) -> None:
    bot = _bot(tmp_path)
    response = MagicMock()
    response.__class__ = nio.SyncError
    response.status_code = "M_UNKNOWN"
    with (
        patch("mindroom.startup_catchup.load_sync_token", return_value="s_prev"),
        patch("mindroom.startup_catchup.save_sync_token") as save_sync_token,
        pytest.raises(RuntimeError, match="Startup catch-up sync failed"),
    ):
        bot.client.sync = AsyncMock(return_value=response)
        await catch_up_missed_user_messages(bot)
    save_sync_token.assert_not_called()


@pytest.mark.asyncio
async def test_catchup_saves_next_batch(tmp_path: Path) -> None:
    bot = _bot(tmp_path)
    save_sync_token = await _run_catchup(bot, _text_event("$1"), next_batch="s_after")
    assert bot.client.next_batch == "s_after"
    save_sync_token.assert_called_once_with(bot.storage_path, bot.agent_name, "s_after")


@pytest.mark.asyncio
async def test_catchup_preserves_order(tmp_path: Path) -> None:
    bot = _bot(tmp_path)
    ordered_event_ids: list[str] = []

    async def capture_message(_room: nio.MatrixRoom, event: nio.RoomMessageText) -> None:
        ordered_event_ids.append(event.event_id)

    bot._on_message = AsyncMock(side_effect=capture_message)
    await _run_catchup(bot, _text_event("$older"), _text_event("$middle"), _text_event("$newer"))
    assert ordered_event_ids == ["$older", "$middle", "$newer"]


@pytest.mark.asyncio
async def test_catchup_dispatches_media_messages(tmp_path: Path) -> None:
    bot = _bot(tmp_path)
    await _run_catchup(
        bot,
        _media_event(nio.RoomMessageImage, "$image", body="image.png", msgtype="m.image"),
        _media_event(nio.RoomMessageAudio, "$audio", body="audio.ogg", msgtype="m.audio"),
    )
    assert [call.args[1].event_id for call in bot._on_media_message.await_args_list] == ["$image", "$audio"]
    bot._on_message.assert_not_awaited()


@pytest.mark.asyncio
async def test_catchup_continues_after_dispatch_error_and_still_advances_token(tmp_path: Path) -> None:
    bot = _bot(tmp_path)
    bot._on_message = AsyncMock(side_effect=[None, RuntimeError("boom"), None])
    with patch.object(bot.logger, "exception") as log_exception:
        save_sync_token = await _run_catchup(
            bot,
            _text_event("$ok"),
            _text_event("$boom"),
            _text_event("$later"),
            next_batch="s_after",
        )
    assert [call.args[1].event_id for call in bot._on_message.await_args_list] == ["$ok", "$boom", "$later"]
    assert bot.client.next_batch == "s_after"
    save_sync_token.assert_called_once_with(bot.storage_path, bot.agent_name, "s_after")
    log_exception.assert_called_once()


@pytest.mark.asyncio
async def test_sync_forever_registers_callbacks_after_catchup(tmp_path: Path) -> None:
    bot = _bot(tmp_path)
    order: list[str] = []
    bot.client.add_event_callback.side_effect = lambda *_args, **_kwargs: order.append("register")
    bot.client.add_response_callback.side_effect = lambda *_args, **_kwargs: order.append("register")
    bot.client.sync_forever = AsyncMock(side_effect=lambda **_kwargs: order.append("sync_forever"))

    async def fake_catchup(_bot) -> None:
        assert bot.client.add_event_callback.call_count == 0
        assert bot.client.add_response_callback.call_count == 0
        order.append("catchup")

    with patch("mindroom.bot.catch_up_missed_user_messages", new=AsyncMock(side_effect=fake_catchup)):
        await bot.sync_forever()

    assert order[0] == "catchup"
    assert order[-1] == "sync_forever"


@pytest.mark.asyncio
async def test_sync_forever_does_not_register_callbacks_before_catchup(tmp_path: Path) -> None:
    bot = _bot(tmp_path)
    bot.client.event_callbacks = []
    bot.client.add_event_callback.side_effect = lambda callback, event_type: bot.client.event_callbacks.append(
        (callback, event_type),
    )
    bot.client.add_response_callback = MagicMock()
    response = _sync_response(_text_event("$1"))

    async def fake_sync(*_args, **_kwargs):
        assert bot.client.event_callbacks == []
        return response

    bot.client.sync = AsyncMock(side_effect=fake_sync)
    bot.client.sync_forever = AsyncMock()

    with (
        patch("mindroom.startup_catchup.load_sync_token", return_value="s_prev"),
        patch("mindroom.startup_catchup.save_sync_token"),
    ):
        await bot.sync_forever()

    assert [call.args[1].event_id for call in bot._on_message.await_args_list] == ["$1"]
