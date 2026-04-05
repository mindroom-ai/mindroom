"""Regression tests for queued-message mid-turn notifications."""

from __future__ import annotations

import asyncio
from contextlib import contextmanager
from types import SimpleNamespace
from typing import TYPE_CHECKING
from unittest.mock import AsyncMock, MagicMock, patch

import nio
import pytest
from agno.models.message import Message
from agno.run.agent import RunCompletedEvent, RunContentEvent, RunOutput
from agno.run.base import RunStatus
from agno.session.agent import AgentSession

from mindroom.ai import (
    QUEUED_MESSAGE_NOTICE_TEXT,
    ai_response,
    install_queued_message_notice_hook,
    queued_message_signal_context,
    stream_agent_response,
)
from mindroom.bot import (
    AgentBot,
    _DispatchPayload,
    _MessageContext,
    _PrecheckedEvent,
    _PreparedDispatch,
    _PreparedTextEvent,
)
from mindroom.config.agent import AgentConfig
from mindroom.config.auth import AuthorizationConfig
from mindroom.config.main import Config
from mindroom.config.models import ModelConfig
from mindroom.hooks import MessageEnvelope
from mindroom.matrix.users import AgentMatrixUser
from mindroom.message_target import MessageTarget
from mindroom.teams import TeamMode, _create_team_instance
from tests.conftest import TEST_PASSWORD, bind_runtime_paths, runtime_paths_for, test_runtime_paths

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
    bot.client = AsyncMock(spec=nio.AsyncClient)
    return bot


def _envelope(*, source_kind: str = "message") -> MessageEnvelope:
    target = MessageTarget.resolve(
        room_id="!room:localhost",
        thread_id=None,
        reply_to_event_id="$event",
    )
    return MessageEnvelope(
        source_event_id="$event",
        room_id="!room:localhost",
        target=target,
        requester_id="@user:localhost",
        sender_id="@user:localhost",
        body="hello",
        attachment_ids=(),
        mentioned_agents=(),
        agent_name="general",
        source_kind=source_kind,
    )


def _prepared_text_event(*, event_id: str = "$event") -> _PreparedTextEvent:
    return _PreparedTextEvent(
        sender="@user:localhost",
        event_id=event_id,
        body="hello",
        source={"content": {"body": "hello"}},
        server_timestamp=1234,
    )


def _message_context() -> _MessageContext:
    return _MessageContext(
        am_i_mentioned=False,
        is_thread=False,
        thread_id=None,
        thread_history=[],
        mentioned_agents=[],
        has_non_agent_mentions=False,
    )


def _notice_count(messages: list[Message]) -> int:
    return sum(1 for message in messages if message.content == QUEUED_MESSAGE_NOTICE_TEXT)


class _FakeStorage:
    def __init__(self) -> None:
        self.session: AgentSession | None = None
        self.upserted = False

    def get_session(self, session_id: str, _session_type: object) -> AgentSession | None:
        if self.session is None or self.session.session_id != session_id:
            return None
        return self.session

    def upsert_session(self, session: AgentSession) -> AgentSession:
        self.session = session
        self.upserted = True
        return session


class _FakeModel:
    def format_function_call_results(
        self,
        messages: list[Message],
        function_call_results: list[Message],
        _compress_tool_results: bool = False,
        **_kwargs: object,
    ) -> None:
        messages.extend(function_call_results)


class _StaticQueuedState:
    def __init__(self, *, pending: bool) -> None:
        self.pending = pending

    def has_pending_human_messages(self) -> bool:
        return self.pending


@contextmanager
def _open_scope(storage: _FakeStorage) -> object:
    yield SimpleNamespace(storage=storage, session=storage.session)


@pytest.mark.asyncio
async def test_generate_response_sets_queued_signal_for_human_ingress(tmp_path: Path) -> None:
    """A waiting human-authored turn should notify the active turn before blocking on the lock."""
    bot = _bot(tmp_path)
    response_envelope = _envelope()
    response_target = response_envelope.target
    lifecycle_lock = bot._response_lifecycle_lock(response_target)
    queued_signal = bot._get_or_create_queued_signal(response_target)
    await lifecycle_lock.acquire()

    try:
        with patch.object(bot, "_generate_response_locked", new=AsyncMock(return_value="$response")) as mock_locked:
            task = asyncio.create_task(
                bot._generate_response(
                    room_id="!room:localhost",
                    prompt="hello",
                    reply_to_event_id="$event",
                    thread_id=None,
                    thread_history=[],
                    user_id="@user:localhost",
                    response_envelope=response_envelope,
                ),
            )
            await asyncio.wait_for(queued_signal.wait(), timeout=0.2)
            lifecycle_lock.release()
            assert await task == "$response"
            mock_locked.assert_awaited_once()
    finally:
        if lifecycle_lock.locked():
            lifecycle_lock.release()

    assert not queued_signal.is_set()


@pytest.mark.asyncio
async def test_generate_response_skips_signal_for_automation_ingress(tmp_path: Path) -> None:
    """Scheduled or hook-originated turns should not interrupt the active turn."""
    bot = _bot(tmp_path)
    response_envelope = _envelope(source_kind="scheduled")
    response_target = response_envelope.target
    lifecycle_lock = bot._response_lifecycle_lock(response_target)
    queued_signal = bot._get_or_create_queued_signal(response_target)
    await lifecycle_lock.acquire()

    try:
        with patch.object(bot, "_generate_response_locked", new=AsyncMock(return_value="$response")):
            task = asyncio.create_task(
                bot._generate_response(
                    room_id="!room:localhost",
                    prompt="hello",
                    reply_to_event_id="$event",
                    thread_id=None,
                    thread_history=[],
                    user_id="@user:localhost",
                    response_envelope=response_envelope,
                ),
            )
            with pytest.raises(asyncio.TimeoutError):
                await asyncio.wait_for(queued_signal.wait(), timeout=0.05)
            lifecycle_lock.release()
            await task
    finally:
        if lifecycle_lock.locked():
            lifecycle_lock.release()

    assert not queued_signal.is_set()


@pytest.mark.asyncio
async def test_generate_team_response_helper_sets_queued_signal(tmp_path: Path) -> None:
    """Team responses should raise the same queued-message signal before waiting on the lock."""
    bot = _bot(tmp_path)
    response_envelope = _envelope()
    response_target = response_envelope.target
    lifecycle_lock = bot._response_lifecycle_lock(response_target)
    queued_signal = bot._get_or_create_queued_signal(response_target)
    await lifecycle_lock.acquire()

    try:
        with patch.object(
            bot,
            "_generate_team_response_helper_locked",
            new=AsyncMock(return_value="$team-response"),
        ) as mock_locked:
            task = asyncio.create_task(
                bot._generate_team_response_helper(
                    room_id="!room:localhost",
                    reply_to_event_id="$event",
                    thread_id=None,
                    team_agents=[],
                    team_mode="coordinate",
                    thread_history=[],
                    requester_user_id="@user:localhost",
                    payload=_DispatchPayload(prompt="hello"),
                    response_envelope=response_envelope,
                ),
            )
            await asyncio.wait_for(queued_signal.wait(), timeout=0.2)
            lifecycle_lock.release()
            assert await task == "$team-response"
            mock_locked.assert_awaited_once()
    finally:
        if lifecycle_lock.locked():
            lifecycle_lock.release()

    assert not queued_signal.is_set()


@pytest.mark.asyncio
async def test_generate_response_preserves_later_queued_human_message(tmp_path: Path) -> None:
    """The next active turn must still see any later human messages that were already queued."""
    bot = _bot(tmp_path)
    response_envelope = _envelope()
    response_target = response_envelope.target
    lifecycle_lock = bot._response_lifecycle_lock(response_target)
    queued_signal = bot._get_or_create_queued_signal(response_target)
    observed_pending: list[bool] = []
    second_turn_started = asyncio.Event()
    allow_turns_to_finish = asyncio.Event()

    async def fake_locked(*_args: object, **_kwargs: object) -> str:
        observed_pending.append(queued_signal.has_pending_human_messages())
        if len(observed_pending) == 1:
            second_turn_started.set()
        await allow_turns_to_finish.wait()
        return f"$response-{len(observed_pending)}"

    await lifecycle_lock.acquire()
    try:
        with patch.object(bot, "_generate_response_locked", new=fake_locked):
            task_b = asyncio.create_task(
                bot._generate_response(
                    room_id="!room:localhost",
                    prompt="hello",
                    reply_to_event_id="$event",
                    thread_id=None,
                    thread_history=[],
                    user_id="@user:localhost",
                    response_envelope=response_envelope,
                ),
            )
            await asyncio.wait_for(queued_signal.wait(), timeout=0.2)
            task_c = asyncio.create_task(
                bot._generate_response(
                    room_id="!room:localhost",
                    prompt="hello again",
                    reply_to_event_id="$event",
                    thread_id=None,
                    thread_history=[],
                    user_id="@user:localhost",
                    response_envelope=response_envelope,
                ),
            )
            for _ in range(20):
                if queued_signal.pending_human_messages == 2:
                    break
                await asyncio.sleep(0)
            assert queued_signal.pending_human_messages == 2

            lifecycle_lock.release()
            await asyncio.wait_for(second_turn_started.wait(), timeout=0.2)
            assert observed_pending == [True]

            allow_turns_to_finish.set()
            assert await task_b == "$response-1"
            assert await task_c == "$response-2"
    finally:
        if lifecycle_lock.locked():
            lifecycle_lock.release()

    assert observed_pending == [True, False]
    assert not queued_signal.is_set()


@pytest.mark.asyncio
async def test_generate_team_response_preserves_later_queued_human_message(tmp_path: Path) -> None:
    """Team queue notices must also survive when more than one human turn is waiting."""
    bot = _bot(tmp_path)
    response_envelope = _envelope()
    response_target = response_envelope.target
    lifecycle_lock = bot._response_lifecycle_lock(response_target)
    queued_signal = bot._get_or_create_queued_signal(response_target)
    observed_pending: list[bool] = []
    second_turn_started = asyncio.Event()
    allow_turns_to_finish = asyncio.Event()

    async def fake_locked(*_args: object, **_kwargs: object) -> str:
        observed_pending.append(queued_signal.has_pending_human_messages())
        if len(observed_pending) == 1:
            second_turn_started.set()
        await allow_turns_to_finish.wait()
        return f"$team-response-{len(observed_pending)}"

    await lifecycle_lock.acquire()
    try:
        with patch.object(bot, "_generate_team_response_helper_locked", new=fake_locked):
            task_b = asyncio.create_task(
                bot._generate_team_response_helper(
                    room_id="!room:localhost",
                    reply_to_event_id="$event",
                    thread_id=None,
                    team_agents=[],
                    team_mode="coordinate",
                    thread_history=[],
                    requester_user_id="@user:localhost",
                    payload=_DispatchPayload(prompt="hello"),
                    response_envelope=response_envelope,
                ),
            )
            await asyncio.wait_for(queued_signal.wait(), timeout=0.2)
            task_c = asyncio.create_task(
                bot._generate_team_response_helper(
                    room_id="!room:localhost",
                    reply_to_event_id="$event",
                    thread_id=None,
                    team_agents=[],
                    team_mode="coordinate",
                    thread_history=[],
                    requester_user_id="@user:localhost",
                    payload=_DispatchPayload(prompt="hello again"),
                    response_envelope=response_envelope,
                ),
            )
            for _ in range(20):
                if queued_signal.pending_human_messages == 2:
                    break
                await asyncio.sleep(0)
            assert queued_signal.pending_human_messages == 2

            lifecycle_lock.release()
            await asyncio.wait_for(second_turn_started.wait(), timeout=0.2)
            assert observed_pending == [True]

            allow_turns_to_finish.set()
            assert await task_b == "$team-response-1"
            assert await task_c == "$team-response-2"
    finally:
        if lifecycle_lock.locked():
            lifecycle_lock.release()

    assert observed_pending == [True, False]
    assert not queued_signal.is_set()


@pytest.mark.asyncio
async def test_coalesced_dispatch_never_creates_queued_signal(tmp_path: Path) -> None:
    """Messages dropped by coalescing should not create false mid-turn notifications."""
    bot = _bot(tmp_path)
    room = MagicMock(spec=nio.MatrixRoom)
    room.room_id = "!room:localhost"
    event = _prepared_text_event(event_id="$older")
    envelope = _envelope()
    dispatch = _PreparedDispatch(
        requester_user_id="@user:localhost",
        context=_message_context(),
        target=envelope.target,
        correlation_id="corr",
        envelope=envelope,
    )

    with (
        patch.object(bot, "_resolve_text_dispatch_event", new=AsyncMock(return_value=event)),
        patch.object(bot, "_prepare_dispatch", new=AsyncMock(return_value=dispatch)),
        patch.object(bot, "_hydrate_dispatch_context", new=AsyncMock()),
        patch.object(bot, "_has_newer_unresponded_in_scope", return_value=True),
        patch.object(bot, "_resolve_dispatch_action", new=AsyncMock()) as mock_resolve_action,
    ):
        await bot._dispatch_text_message(
            room,
            _PrecheckedEvent(event=event, requester_user_id="@user:localhost"),
        )

    assert bot.response_tracker.has_responded("$older")
    mock_resolve_action.assert_not_awaited()
    assert bot._thread_queued_signals == {}


def test_notice_hook_injects_once_per_turn_and_skips_stop_after_tool_call() -> None:
    """The injected notice should be once per turn, avoid double wrapping, and skip stop-after-tool-call results."""
    model = _FakeModel()
    install_queued_message_notice_hook(model)
    install_queued_message_notice_hook(model)

    plain_messages = [Message(role="user", content="hello")]
    model.format_function_call_results(
        messages=plain_messages,
        function_call_results=[Message(role="tool", content="result")],
    )
    assert _notice_count(plain_messages) == 0

    with queued_message_signal_context(_StaticQueuedState(pending=True)):
        queued_messages = [Message(role="user", content="hello")]
        model.format_function_call_results(
            messages=queued_messages,
            function_call_results=[Message(role="tool", content="result")],
        )
        model.format_function_call_results(
            messages=queued_messages,
            function_call_results=[Message(role="tool", content="another result")],
        )

        stop_after_messages = [Message(role="user", content="hello")]
        stop_after_model = _FakeModel()
        install_queued_message_notice_hook(stop_after_model)
        stop_after_model.format_function_call_results(
            messages=stop_after_messages,
            function_call_results=[Message(role="tool", content="done", stop_after_tool_call=True)],
        )

    with queued_message_signal_context(_StaticQueuedState(pending=True)):
        next_turn_messages = [Message(role="user", content="hello")]
        model.format_function_call_results(
            messages=next_turn_messages,
            function_call_results=[Message(role="tool", content="result")],
        )

    assert _notice_count(queued_messages) == 1
    assert _notice_count(next_turn_messages) == 1
    assert _notice_count(stop_after_messages) == 0


@pytest.mark.asyncio
async def test_ai_response_scrubs_stale_notice_before_prepare(tmp_path: Path) -> None:
    """Loaded session history should be scrubbed before replay planning sees it."""
    config = _config(tmp_path)
    storage = _FakeStorage()
    storage.session = AgentSession(
        session_id="session-1",
        runs=[
            RunOutput(
                run_id="run-0",
                session_id="session-1",
                messages=[Message(role="user", content=QUEUED_MESSAGE_NOTICE_TEXT)],
            ),
        ],
    )
    observed_notice_counts: list[int] = []

    async def fake_prepare(
        _agent_name: str,
        _prompt: str,
        _runtime_paths: object,
        _config: object,
        _session_id: str | None = None,
        scope_context: object | None = None,
        *_args: object,
        **_kwargs: object,
    ) -> tuple[object, str, list[str], object]:
        assert scope_context is not None
        session = scope_context.session
        assert session is not None
        observed_notice_counts.append(_notice_count(session.runs[0].messages or []))
        agent = MagicMock()
        agent.model = None
        agent.arun = AsyncMock(
            return_value=RunOutput(
                run_id="run-1",
                session_id="session-1",
                content="final answer",
                model="test-model",
                model_provider="openai",
                messages=[],
                status=RunStatus.completed,
                tools=[],
            ),
        )
        return agent, "prompt", [], MagicMock()

    with (
        patch("mindroom.ai.open_resolved_scope_session_context", side_effect=lambda **_kwargs: _open_scope(storage)),
        patch("mindroom.ai._prepare_agent_and_prompt", new=AsyncMock(side_effect=fake_prepare)),
        patch("mindroom.ai.close_agent_runtime_sqlite_dbs"),
    ):
        response = await ai_response(
            agent_name="general",
            prompt="hello",
            session_id="session-1",
            runtime_paths=runtime_paths_for(config),
            config=config,
        )

    assert response == "final answer"
    assert observed_notice_counts == [0]
    assert storage.upserted is True
    assert storage.session is not None
    assert _notice_count(storage.session.runs[0].messages or []) == 0


@pytest.mark.asyncio
async def test_ai_response_strips_notice_from_run_output_and_session(tmp_path: Path) -> None:
    """Non-streaming runs should scrub the hidden notice from both return state and persisted history."""
    config = _config(tmp_path)
    storage = _FakeStorage()
    model = _FakeModel()
    run_output_holder: dict[str, RunOutput] = {}

    async def fake_arun(
        _prompt: str,
        *,
        session_id: str,
        **_kwargs: object,
    ) -> RunOutput:
        messages = [Message(role="user", content="hello")]
        model.format_function_call_results(
            messages=messages,
            function_call_results=[Message(role="tool", content="tool result")],
        )
        stored_messages = [message.model_copy(deep=True) for message in messages]
        storage.session = AgentSession(
            session_id=session_id,
            runs=[RunOutput(run_id="run-1", session_id=session_id, messages=stored_messages)],
        )
        run_output = RunOutput(
            run_id="run-1",
            session_id=session_id,
            content="final answer",
            model="test-model",
            model_provider="openai",
            messages=messages,
            status=RunStatus.completed,
            tools=[],
        )
        run_output_holder["run"] = run_output
        return run_output

    agent = MagicMock()
    agent.model = model
    agent.arun = AsyncMock(side_effect=fake_arun)

    with (
        patch("mindroom.ai.open_resolved_scope_session_context", side_effect=lambda **_kwargs: _open_scope(storage)),
        patch("mindroom.ai._prepare_agent_and_prompt", new=AsyncMock(return_value=(agent, "prompt", [], MagicMock()))),
        patch("mindroom.ai.close_agent_runtime_sqlite_dbs"),
        queued_message_signal_context(_StaticQueuedState(pending=True)),
    ):
        response = await ai_response(
            agent_name="general",
            prompt="hello",
            session_id="session-1",
            runtime_paths=runtime_paths_for(config),
            config=config,
        )

    assert response == "final answer"
    assert storage.upserted is True
    assert _notice_count(run_output_holder["run"].messages or []) == 0
    assert storage.session is not None
    assert _notice_count(storage.session.runs[0].messages or []) == 0


@pytest.mark.asyncio
async def test_stream_agent_response_strips_notice_from_session(tmp_path: Path) -> None:
    """Streaming runs should also scrub the hidden notice from persisted history."""
    config = _config(tmp_path)
    storage = _FakeStorage()
    model = _FakeModel()

    async def fake_stream(
        _prompt: str,
        *,
        session_id: str,
        **_kwargs: object,
    ) -> AsyncIterator[object]:
        messages = [Message(role="user", content="hello")]
        model.format_function_call_results(
            messages=messages,
            function_call_results=[Message(role="tool", content="tool result")],
        )
        stored_messages = [message.model_copy(deep=True) for message in messages]
        storage.session = AgentSession(
            session_id=session_id,
            runs=[RunOutput(run_id="run-1", session_id=session_id, messages=stored_messages)],
        )
        yield RunContentEvent(content="chunk")
        yield RunCompletedEvent(run_id="run-1", session_id=session_id)

    agent = MagicMock()
    agent.model = model
    agent.arun = fake_stream

    with (
        patch("mindroom.ai.open_resolved_scope_session_context", side_effect=lambda **_kwargs: _open_scope(storage)),
        patch("mindroom.ai._prepare_agent_and_prompt", new=AsyncMock(return_value=(agent, "prompt", [], MagicMock()))),
        patch("mindroom.ai.close_agent_runtime_sqlite_dbs"),
        queued_message_signal_context(_StaticQueuedState(pending=True)),
    ):
        chunks = [
            chunk
            async for chunk in stream_agent_response(
                agent_name="general",
                prompt="hello",
                session_id="session-1",
                runtime_paths=runtime_paths_for(config),
                config=config,
            )
        ]

    assert any(isinstance(chunk, RunContentEvent) and chunk.content == "chunk" for chunk in chunks)
    assert storage.upserted is True
    assert storage.session is not None
    assert _notice_count(storage.session.runs[0].messages or []) == 0


def test_create_team_instance_installs_notice_hook_on_team_model(tmp_path: Path) -> None:
    """Team coordinator models should receive the same queued-message notice hook."""
    config = _config(tmp_path)
    runtime_paths = runtime_paths_for(config)
    model = _FakeModel()

    with (
        patch("mindroom.teams.get_model_instance", return_value=model),
        patch("mindroom.teams.Team", side_effect=lambda **kwargs: SimpleNamespace(model=kwargs["model"])),
        queued_message_signal_context(_StaticQueuedState(pending=True)),
    ):
        team = _create_team_instance(
            agents=[],
            mode=TeamMode.COORDINATE,
            config=config,
            runtime_paths=runtime_paths,
            team_display_name="Queued Notice Team",
            fallback_team_id="queued-notice-team",
        )
        messages = [Message(role="user", content="hello")]
        team.model.format_function_call_results(
            messages=messages,
            function_call_results=[Message(role="tool", content="tool result")],
        )
        assert _notice_count(messages) == 1
