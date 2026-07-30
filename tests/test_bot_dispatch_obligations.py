"""Focused AgentBot wiring tests for durable Matrix callback obligations."""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING, cast
from unittest.mock import MagicMock

import nio
import pytest

from mindroom.background_tasks import wait_for_background_tasks
from mindroom.bot import AgentBot
from mindroom.config.agent import AgentConfig
from mindroom.config.main import Config
from mindroom.config.models import ModelConfig
from mindroom.dispatch_obligations import (
    DispatchCallbackKind,
    _DispatchCallback,
)
from mindroom.dispatch_obligations import (
    _DispatchCallbackResult as DispatchCallbackResult,
)
from mindroom.dispatch_obligations import (
    _DispatchObligationTaskWrapper as DispatchObligationTaskWrapper,
)
from mindroom.matrix.media import MATRIX_MEDIA_EVENT_TYPES
from mindroom.matrix.users import AgentMatrixUser
from tests.conftest import (
    TEST_PASSWORD,
    bind_runtime_paths,
    install_runtime_cache_support,
    runtime_paths_for,
    test_runtime_paths,
)

if TYPE_CHECKING:
    from pathlib import Path


def _agent_bot(tmp_path: Path) -> AgentBot:
    runtime_paths = test_runtime_paths(tmp_path)
    config = bind_runtime_paths(
        Config(
            agents={"code": AgentConfig(display_name="Code", rooms=["!room:localhost"])},
            models={"default": ModelConfig(provider="test", id="test-model")},
        ),
        runtime_paths,
    )
    bot = AgentBot(
        agent_user=AgentMatrixUser(
            agent_name="code",
            password=TEST_PASSWORD,
            display_name="Code",
            user_id="@mindroom_code:localhost",
        ),
        storage_path=tmp_path,
        config=config,
        runtime_paths=runtime_paths_for(config),
        rooms=["!room:localhost"],
    )
    return install_runtime_cache_support(bot)


def _text_event(event_id: str) -> nio.RoomMessageText:
    return nio.RoomMessageText.from_dict(
        {
            "content": {"body": "hello", "msgtype": "m.text"},
            "event_id": event_id,
            "sender": "@user:localhost",
            "origin_server_ts": 1,
            "room_id": "!room:localhost",
            "type": "m.room.message",
        },
    )


@pytest.mark.asyncio
async def test_callback_wrapper_persists_before_background_execution(tmp_path: Path) -> None:
    """Returning to nio must require durable acceptance before the callback task runs."""
    bot = _agent_bot(tmp_path)
    entered = asyncio.Event()
    release = asyncio.Event()

    async def callback(_room: nio.MatrixRoom, _event: nio.Event) -> DispatchCallbackResult:
        entered.set()
        await release.wait()
        return DispatchCallbackResult.SUCCEEDED

    callbacks = cast("dict[DispatchCallbackKind, _DispatchCallback]", bot._dispatch_obligation_runner.callbacks)
    callbacks[DispatchCallbackKind.MESSAGE] = callback
    wrapper = bot._dispatch_obligation_runner.task_wrapper(
        DispatchCallbackKind.MESSAGE,
        owner=bot._runtime_view,
    )
    event = _text_event("$durable")

    await wrapper(nio.MatrixRoom("!room:localhost", "@mindroom_code:localhost"), event)

    assert bot._dispatch_obligation_store.has_pending("$durable", DispatchCallbackKind.MESSAGE)
    await entered.wait()
    release.set()
    await wait_for_background_tasks(timeout=1.0, owner=bot._runtime_view)
    assert not bot._dispatch_obligation_store.has_pending("$durable", DispatchCallbackKind.MESSAGE)


def test_correctness_callbacks_have_explicit_durable_kinds(tmp_path: Path) -> None:
    """Every source-backed correctness callback must register through the durable wrapper."""
    bot = _agent_bot(tmp_path)
    client = MagicMock(spec=nio.AsyncClient)

    bot._dispatch_obligation_runner.register_source_callbacks(
        client,
        owner=bot._runtime_view,
    )

    registrations = {
        event_type: callback
        for callback, event_type in (call.args for call in client.add_event_callback.call_args_list)
    }
    expected_kinds = {
        nio.RoomMessageText: DispatchCallbackKind.MESSAGE,
        nio.ReactionEvent: DispatchCallbackKind.REACTION,
        nio.RedactionEvent: DispatchCallbackKind.REDACTION,
        nio.UnknownEvent: DispatchCallbackKind.APPROVAL,
        nio.MegolmEvent: DispatchCallbackKind.DECRYPTION_FAILURE,
        **dict.fromkeys(MATRIX_MEDIA_EVENT_TYPES, DispatchCallbackKind.MEDIA),
    }
    assert expected_kinds.keys() <= registrations.keys()
    for event_type, callback_kind in expected_kinds.items():
        callback = registrations[event_type]
        assert isinstance(callback, DispatchObligationTaskWrapper)
        assert callback.callback_kind is callback_kind
