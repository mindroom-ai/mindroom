"""Shared bot/request scaffolding for the focused ResponseRunner test modules."""

from __future__ import annotations

import threading
from contextlib import asynccontextmanager
from typing import TYPE_CHECKING

from mindroom.config.agent import AgentConfig
from mindroom.config.auth import AuthorizationConfig
from mindroom.config.main import Config
from mindroom.config.models import ModelConfig
from mindroom.hooks import MessageEnvelope
from mindroom.matrix.users import AgentMatrixUser
from mindroom.message_target import MessageTarget
from mindroom.response_runner import ResponseRequest
from tests.bot_helpers import make_test_agent_bot
from tests.conftest import (
    TEST_PASSWORD,
    bind_runtime_paths,
    install_runtime_journal_support,
    make_matrix_client_mock,
    message_origin,
    runtime_paths_for,
    test_runtime_paths,
    wrap_extracted_collaborators,
)

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Callable
    from pathlib import Path

    from agno.db.base import BaseDb

    from mindroom.bot import AgentBot


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
    bot = make_test_agent_bot(agent_user, tmp_path, config, runtime_paths_for(config), rooms=["!room:localhost"])
    bot.client = make_matrix_client_mock(user_id="@mindroom_general:localhost")
    install_runtime_journal_support(bot)
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
        target=target,
        body="hello",
        attachment_ids=(),
        mentioned_agents=(),
        agent_name="general",
        origin=message_origin(sender_id="@user:localhost", requester_id="@user:localhost", source_kind="message"),
    )


def _plain_request(target: MessageTarget, *, source_event_id: str = "$event") -> ResponseRequest:
    return ResponseRequest(
        thread_history=[],
        prompt="hello",
        user_id="@user:localhost",
        response_envelope=_envelope(target, source_event_id=source_event_id),
    )


class _PersistenceSeamProbe:
    """Observe one real queued save followed by one runner-owned linkage handle."""

    def __init__(
        self,
        save_storage: BaseDb,
        create_storage: Callable[..., BaseDb],
    ) -> None:
        self.save_started = threading.Event()
        self.release_save = threading.Event()
        self.link_storage_created = threading.Event()
        self.link_storage_closed = threading.Event()
        self._save_upsert = save_storage.upsert_session
        self._create_storage = create_storage
        self._state_lock = threading.Lock()
        self._track_next_storage = False

    def blocked_upsert(self, session: object, deserialize: bool | None = True) -> object:
        """Hold the accepted save in its real persistence lane."""
        self.save_started.set()
        assert self.release_save.wait(timeout=5)
        return self._save_upsert(session, deserialize=deserialize)  # type: ignore[arg-type]

    def arm_link_storage(self) -> None:
        """Mark the next real factory result as the response-link handle."""
        with self._state_lock:
            self._track_next_storage = True

    def create_storage(self, *args: object, **kwargs: object) -> BaseDb:
        """Delegate to the real factory and observe exactly one real close."""
        storage = self._create_storage(*args, **kwargs)
        with self._state_lock:
            track_storage = self._track_next_storage
            self._track_next_storage = False
        if not track_storage:
            return storage

        original_close = storage.close

        def close() -> None:
            try:
                original_close()
            finally:
                self.link_storage_closed.set()

        storage.close = close  # type: ignore[method-assign]
        self.link_storage_created.set()
        return storage


@asynccontextmanager
async def _noop_typing(*_args: object, **_kwargs: object) -> AsyncIterator[None]:
    yield
