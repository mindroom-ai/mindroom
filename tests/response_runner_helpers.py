"""Shared bot/request scaffolding for the focused ResponseRunner test modules."""

from __future__ import annotations

from contextlib import asynccontextmanager
from typing import TYPE_CHECKING

from mindroom.bot import AgentBot
from mindroom.config.agent import AgentConfig
from mindroom.config.auth import AuthorizationConfig
from mindroom.config.main import Config
from mindroom.config.models import ModelConfig
from mindroom.hooks import MessageEnvelope
from mindroom.matrix.users import AgentMatrixUser
from mindroom.message_target import MessageTarget
from mindroom.response_runner import ResponseRequest
from tests.conftest import (
    TEST_PASSWORD,
    bind_runtime_paths,
    install_runtime_cache_support,
    make_matrix_client_mock,
    message_origin,
    runtime_paths_for,
    test_runtime_paths,
    wrap_extracted_collaborators,
)

if TYPE_CHECKING:
    from collections.abc import AsyncIterator
    from pathlib import Path


def _config(tmp_path: Path) -> Config:
    return bind_runtime_paths(
        Config(
            agents={"general": AgentConfig(display_name="General", rooms=["!room:localhost"])},
            teams={},
            models={"default": ModelConfig(provider="openai", id="test-model")},
            authorization=AuthorizationConfig(default_room_access=True),
        ),
        test_runtime_paths(tmp_path),
    )


def _bot(tmp_path: Path) -> AgentBot:
    config = _config(tmp_path)
    agent_user = AgentMatrixUser(
        agent_name="general",
        password=TEST_PASSWORD,
        display_name="General",
        user_id="@mindroom_general:localhost",
    )
    bot = AgentBot(agent_user, tmp_path, config, runtime_paths_for(config), rooms=["!room:localhost"])
    bot.client = make_matrix_client_mock(user_id="@mindroom_general:localhost")
    install_runtime_cache_support(bot)
    wrap_extracted_collaborators(bot)
    return bot


def _target(*, thread_id: str | None = None, reply_to_event_id: str = "$event") -> MessageTarget:
    return MessageTarget.resolve(
        room_id="!room:localhost",
        thread_id=thread_id,
        reply_to_event_id=reply_to_event_id,
        room_mode=thread_id is None,
    )


def _envelope(target: MessageTarget, *, source_event_id: str = "$event") -> MessageEnvelope:
    return MessageEnvelope(
        source_event_id=source_event_id,
        room_id="!room:localhost",
        target=target,
        requester_id="@user:localhost",
        sender_id="@user:localhost",
        body="hello",
        attachment_ids=(),
        mentioned_agents=(),
        agent_name="general",
        source_kind="message",
        origin=message_origin(sender_id="@user:localhost", requester_id="@user:localhost", source_kind="message"),
    )


def _plain_request(target: MessageTarget, *, source_event_id: str = "$event") -> ResponseRequest:
    return ResponseRequest(
        thread_history=[],
        prompt="hello",
        user_id="@user:localhost",
        response_envelope=_envelope(target, source_event_id=source_event_id),
    )


@asynccontextmanager
async def _noop_typing(*_args: object, **_kwargs: object) -> AsyncIterator[None]:
    yield
