"""Regression tests for queued-message mid-turn notifications."""

from __future__ import annotations

import asyncio
from contextlib import contextmanager
from types import SimpleNamespace
from typing import TYPE_CHECKING, Self
from unittest.mock import AsyncMock, MagicMock, patch

import nio
import pytest
from agno.db.base import SessionType
from agno.media import Image
from agno.models.message import Message
from agno.run.agent import RunCompletedEvent, RunContentEvent, RunOutput
from agno.run.base import RunStatus
from agno.run.team import TeamRunOutput
from agno.session.agent import AgentSession
from agno.session.team import TeamSession

from mindroom.ai import _PreparedAgentRun, ai_response, stream_agent_response
from mindroom.ai_runtime import (
    QUEUED_MESSAGE_NOTICE_TEXT,
    cleanup_queued_notice_state,
    install_queued_message_notice_hook,
    queued_message_signal_context,
)
from mindroom.bot import AgentBot
from mindroom.bot_runtime_view import BotRuntimeState
from mindroom.coalescing import PreparedTextEvent
from mindroom.config.agent import AgentConfig
from mindroom.config.auth import AuthorizationConfig
from mindroom.config.main import Config
from mindroom.config.models import ModelConfig
from mindroom.conversation_resolver import MessageContext
from mindroom.final_delivery import FinalDeliveryOutcome
from mindroom.hooks import MessageEnvelope
from mindroom.inbound_turn_normalizer import DispatchPayload
from mindroom.matrix.client import ResolvedVisibleMessage
from mindroom.matrix.users import AgentMatrixUser
from mindroom.message_target import MessageTarget
from mindroom.post_response_effects import (
    PostResponseEffectsDeps,
    PostResponseEffectsSupport,
    ResponseOutcome,
    apply_post_response_effects,
)
from mindroom.response_runner import PostLockRequestPreparationError, ResponseRequest, ResponseRunner
from mindroom.teams import TeamMode, _create_team_instance
from mindroom.turn_controller import _PrecheckedEvent
from mindroom.turn_policy import DispatchPlan, PreparedDispatch
from tests.conftest import (
    TEST_PASSWORD,
    bind_runtime_paths,
    install_runtime_cache_support,
    make_event_cache_mock,
    make_event_cache_write_coordinator_mock,
    runtime_paths_for,
    test_runtime_paths,
    unwrap_extracted_collaborator,
    wrap_extracted_collaborators,
)

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Coroutine
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
    install_runtime_cache_support(bot)
    wrap_extracted_collaborators(bot)
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


def _prepared_text_event(*, event_id: str = "$event") -> PreparedTextEvent:
    return PreparedTextEvent(
        sender="@user:localhost",
        event_id=event_id,
        body="hello",
        source={"content": {"body": "hello"}},
        server_timestamp=1234,
    )


def _prepared_run(agent: object, *, prompt: str = "prompt") -> _PreparedAgentRun:
    return _PreparedAgentRun(
        agent=agent,
        messages=(Message(role="user", content=prompt),),
        unseen_event_ids=[],
        prepared_history=MagicMock(),
    )


def _message_context() -> MessageContext:
    return MessageContext(
        am_i_mentioned=False,
        is_thread=False,
        thread_id=None,
        thread_history=[],
        mentioned_agents=[],
        has_non_agent_mentions=False,
    )


def _notice_count(messages: list[Message]) -> int:
    return sum(1 for message in messages if message.content == QUEUED_MESSAGE_NOTICE_TEXT)


def _queued_notice_message() -> Message:
    return Message(
        role="user",
        content=QUEUED_MESSAGE_NOTICE_TEXT,
        provider_data={"mindroom_queued_message_notice": True},
    )


class _FakeStorage:
    def __init__(self) -> None:
        self.session: AgentSession | TeamSession | None = None
        self.upserted = False

    def get_session(self, session_id: str, _session_type: object) -> AgentSession | TeamSession | None:
        if self.session is None or self.session.session_id != session_id:
            return None
        return self.session

    def upsert_session(self, session: AgentSession | TeamSession) -> AgentSession | TeamSession:
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

    def _handle_function_call_media(
        self,
        messages: list[Message],
        function_call_results: list[Message],
        send_media_to_model: bool = True,
    ) -> None:
        if not send_media_to_model:
            return
        if any(message.images or message.videos or message.audio or message.files for message in function_call_results):
            messages.append(Message(role="user", content="Take note of the following content"))


class _FakeModelWithoutFunctionCallMedia:
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


class _PrelockBarrierLock:
    def __init__(self) -> None:
        self._locked = False
        self.first_waiting = asyncio.Event()
        self._allow_first_entry = asyncio.Event()
        self._first_entered = asyncio.Event()
        self._released = asyncio.Event()
        self._released.set()

    def locked(self) -> bool:
        return self._locked

    async def acquire(self) -> None:
        if not self.first_waiting.is_set():
            self.first_waiting.set()
            await self._allow_first_entry.wait()
        else:
            await self._first_entered.wait()
        await self._released.wait()
        self._locked = True
        self._released.clear()
        self._first_entered.set()

    def release(self) -> None:
        self._locked = False
        self._released.set()

    async def __aenter__(self) -> Self:
        await self.acquire()
        return self

    async def __aexit__(self, *_args: object) -> None:
        self.release()


@pytest.mark.asyncio
async def test_post_response_effects_skip_thread_summary_for_suppressed_delivery() -> None:
    """Suppressed deliveries must not enqueue a thread summary."""
    queue_thread_summary = MagicMock()

    await apply_post_response_effects(
        FinalDeliveryOutcome(
            terminal_status="cancelled",
            event_id=None,
            suppressed=True,
        ),
        ResponseOutcome(
            interactive_target=MessageTarget.resolve(
                room_id="!room:localhost",
                thread_id="$thread",
                reply_to_event_id="$event",
            ),
            thread_summary_room_id="!room:localhost",
            thread_summary_thread_id="$thread",
            thread_summary_message_count_hint=3,
        ),
        PostResponseEffectsDeps(
            logger=MagicMock(),
            queue_thread_summary=queue_thread_summary,
        ),
    )

    queue_thread_summary.assert_not_called()


@pytest.mark.asyncio
async def test_post_response_effects_register_interactive_follow_up_for_preserved_stream_failure() -> None:
    """Preserved visible streamed replies should still register interactive follow-up."""
    register_interactive = AsyncMock()
    target = MessageTarget.resolve(
        room_id="!room:localhost",
        thread_id="$thread",
        reply_to_event_id="$event",
    )

    await apply_post_response_effects(
        FinalDeliveryOutcome(
            terminal_status="completed",
            event_id="$stream",
            is_visible_response=True,
            final_visible_body="Choose",
            delivery_kind="sent",
            option_map={"1": "yes"},
            options_list=({"emoji": "1", "label": "Yes", "value": "yes"},),
        ),
        ResponseOutcome(
            interactive_target=target,
        ),
        PostResponseEffectsDeps(
            logger=MagicMock(),
            register_interactive=register_interactive,
        ),
    )

    register_interactive.assert_awaited_once_with(
        "$stream",
        target,
        {"1": "yes"},
        [{"emoji": "1", "label": "Yes", "value": "yes"}],
    )


@pytest.mark.asyncio
async def test_post_response_effects_skip_interactive_follow_up_for_preserved_stream_error() -> None:
    """Failed preserved stream outcomes must not register interactive follow-up on a failed reply."""
    register_interactive = AsyncMock()
    target = MessageTarget.resolve(
        room_id="!room:localhost",
        thread_id="$thread",
        reply_to_event_id="$event",
    )

    await apply_post_response_effects(
        FinalDeliveryOutcome(
            terminal_status="error",
            event_id="$stream",
            is_visible_response=True,
            final_visible_body="Choose",
            option_map={"1": "yes"},
            options_list=({"emoji": "1", "label": "Yes", "value": "yes"},),
        ),
        ResponseOutcome(
            interactive_target=target,
        ),
        PostResponseEffectsDeps(
            logger=MagicMock(),
            register_interactive=register_interactive,
        ),
    )

    register_interactive.assert_not_awaited()


@pytest.mark.asyncio
async def test_post_response_effects_queues_summary_with_stale_hint_inside_margin(tmp_path: Path) -> None:
    """A stale hint just below threshold should still reach the live summary check."""
    config = _config(tmp_path)
    runtime_paths = runtime_paths_for(config)
    client = AsyncMock(spec=nio.AsyncClient)
    runtime = BotRuntimeState(
        client=client,
        config=config,
        runtime_paths=runtime_paths,
        enable_streaming=False,
        orchestrator=None,
        event_cache=make_event_cache_mock(),
        event_cache_write_coordinator=make_event_cache_write_coordinator_mock(),
    )
    conversation_cache = MagicMock()
    support = PostResponseEffectsSupport(
        runtime=runtime,
        logger=MagicMock(),
        runtime_paths=runtime_paths,
        delivery_gateway=MagicMock(),
        conversation_cache=conversation_cache,
    )
    deps = support.build_deps(
        room_id="!room:localhost",
        thread_id="$thread",
        interactive_agent_name="general",
    )
    thread_history = [
        ResolvedVisibleMessage.synthetic(
            sender=f"@user{i}:localhost",
            body=f"Message {i}",
            timestamp=i,
            event_id=f"$message{i}",
        )
        for i in range(5)
    ]
    scheduled_tasks: list[asyncio.Task[None]] = []

    def schedule_background_task(
        coro: Coroutine[object, object, None],
        *,
        name: str,
        error_handler: object | None = None,  # noqa: ARG001
        owner: object | None = None,  # noqa: ARG001
    ) -> asyncio.Task[None]:
        task = asyncio.create_task(coro, name=name)
        scheduled_tasks.append(task)
        return task

    with (
        patch("mindroom.post_response_effects.create_background_task", side_effect=schedule_background_task),
        patch("mindroom.thread_summary._load_thread_history", new=AsyncMock(return_value=thread_history)) as mock_fetch,
        patch("mindroom.thread_summary._generate_summary", new=AsyncMock(return_value="Summary")) as mock_generate,
        patch("mindroom.thread_summary.send_thread_summary_event", new=AsyncMock(return_value="$summary")) as mock_send,
        patch("mindroom.thread_summary._recover_last_summary_count", new=AsyncMock(return_value=0)),
    ):
        await apply_post_response_effects(
            FinalDeliveryOutcome(
                terminal_status="completed",
                event_id="$response",
                is_visible_response=True,
                final_visible_body="response",
                delivery_kind="sent",
            ),
            ResponseOutcome(
                thread_summary_room_id="!room:localhost",
                thread_summary_thread_id="$thread",
                thread_summary_message_count_hint=4,
            ),
            deps,
        )

        assert scheduled_tasks
        await asyncio.gather(*scheduled_tasks)

    mock_fetch.assert_awaited_once_with(conversation_cache, "!room:localhost", "$thread")
    mock_generate.assert_awaited_once_with(thread_history, config, runtime_paths)
    mock_send.assert_awaited_once_with(
        client,
        "!room:localhost",
        "$thread",
        "Summary",
        5,
        "default",
        conversation_cache,
    )


@pytest.mark.asyncio
async def test_generate_response_sets_queued_signal_for_human_ingress(tmp_path: Path) -> None:
    """A waiting human-authored turn should notify the active turn before blocking on the lock."""
    bot = _bot(tmp_path)
    response_envelope = _envelope()
    response_target = response_envelope.target
    coordinator = unwrap_extracted_collaborator(bot._response_runner)
    lifecycle_lock = coordinator._response_lifecycle_lock(response_target)
    queued_signal = coordinator._get_or_create_queued_signal(response_target)
    await lifecycle_lock.acquire()

    try:
        with patch.object(
            ResponseRunner,
            "generate_response_locked",
            new=AsyncMock(return_value="$response"),
        ) as mock_locked:
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
    coordinator = unwrap_extracted_collaborator(bot._response_runner)
    lifecycle_lock = coordinator._response_lifecycle_lock(response_target)
    queued_signal = coordinator._get_or_create_queued_signal(response_target)
    await lifecycle_lock.acquire()

    try:
        with patch.object(
            ResponseRunner,
            "generate_response_locked",
            new=AsyncMock(return_value="$response"),
        ):
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
async def test_generate_response_detects_active_turn_before_lock_is_held(tmp_path: Path) -> None:
    """A second human turn should queue even before the first acquires the lifecycle lock."""
    bot = _bot(tmp_path)
    coordinator = unwrap_extracted_collaborator(bot._response_runner)
    lock = _PrelockBarrierLock()
    response_envelope = _envelope()
    response_target = response_envelope.target
    observed_pending: dict[str, int] = {}

    async def fake_generate_response_locked(
        _self: ResponseRunner,
        request: ResponseRequest,
        *,
        resolved_target: MessageTarget,
    ) -> str:
        del resolved_target
        user_id = str(request.user_id)
        queued_signal = coordinator._get_or_create_queued_signal(response_target)
        observed_pending[user_id] = queued_signal.pending_human_messages
        return user_id

    with (
        patch.object(coordinator, "_response_lifecycle_lock", return_value=lock),
        patch.object(ResponseRunner, "generate_response_locked", new=fake_generate_response_locked),
    ):
        first_task = asyncio.create_task(
            bot._generate_response(
                room_id="!room:localhost",
                prompt="hello",
                reply_to_event_id="$event",
                thread_id=None,
                thread_history=[],
                user_id="first",
                response_envelope=response_envelope,
            ),
        )
        await lock.first_waiting.wait()

        second_task = asyncio.create_task(
            bot._generate_response(
                room_id="!room:localhost",
                prompt="stop",
                reply_to_event_id="$event",
                thread_id=None,
                thread_history=[],
                user_id="second",
                response_envelope=response_envelope,
            ),
        )

        lock._allow_first_entry.set()
        second_result = await second_task
        first_result = await first_task

    assert second_result == "second"
    assert first_result == "first"
    assert observed_pending["first"] == 1


@pytest.mark.asyncio
async def test_generate_response_waits_for_lock_before_starting_placeholder_lifecycle(tmp_path: Path) -> None:
    """A queued scheduled turn should not start the placeholder lifecycle until it owns the lock."""
    bot = _bot(tmp_path)
    response_envelope = _envelope(source_kind="scheduled")
    response_target = response_envelope.target
    coordinator = unwrap_extracted_collaborator(bot._response_runner)
    lifecycle_lock = coordinator._response_lifecycle_lock(response_target)
    await lifecycle_lock.acquire()
    lifecycle_started = asyncio.Event()

    async def fake_run_cancellable_response(*_args: object, **kwargs: object) -> str:
        lifecycle_started.set()
        response_function = kwargs["response_function"]
        await response_function(None)
        return "$response"

    try:
        with (
            patch.object(
                ResponseRunner,
                "process_and_respond",
                new=AsyncMock(
                    return_value=FinalDeliveryOutcome(
                        terminal_status="completed",
                        event_id="$response",
                        is_visible_response=True,
                        final_visible_body="ok",
                        delivery_kind="sent",
                    ),
                ),
            ),
            patch.object(
                ResponseRunner,
                "run_cancellable_response",
                new=AsyncMock(side_effect=fake_run_cancellable_response),
            ) as mock_run_cancellable_response,
            patch("mindroom.response_runner.should_use_streaming", new_callable=AsyncMock, return_value=False),
            patch("mindroom.response_runner.reprioritize_auto_flush_sessions", new=MagicMock()),
            patch("mindroom.response_lifecycle.apply_post_response_effects", new=AsyncMock()),
        ):
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
            await asyncio.sleep(0.05)
            mock_run_cancellable_response.assert_not_awaited()

            lifecycle_lock.release()
            await asyncio.wait_for(lifecycle_started.wait(), timeout=0.2)
            resolution = await task
            assert resolution == "$response"
    finally:
        if lifecycle_lock.locked():
            lifecycle_lock.release()


@pytest.mark.asyncio
async def test_refresh_thread_history_after_lock_refreshes_empty_thread_history(tmp_path: Path) -> None:
    """Threaded turns with an empty cached history should still refresh after lock handoff."""
    bot = _bot(tmp_path)
    coordinator = unwrap_extracted_collaborator(bot._response_runner)
    resolver = unwrap_extracted_collaborator(coordinator.deps.resolver)
    fresh_history = [SimpleNamespace(event_id="$reply", body="updated")]

    with patch.object(
        resolver,
        "fetch_thread_history",
        new=AsyncMock(return_value=fresh_history),
    ) as mock_fetch_thread_history:
        request = await coordinator._refresh_thread_history_after_lock(
            ResponseRequest(
                room_id="!room:localhost",
                reply_to_event_id="$event",
                thread_id="$thread",
                thread_history=[],
                prompt="hello",
                user_id="@user:localhost",
            ),
        )

    mock_fetch_thread_history.assert_awaited_once_with(bot.client, "!room:localhost", "$thread")
    assert request.thread_history == fresh_history


@pytest.mark.asyncio
async def test_prepare_request_after_lock_wraps_refresh_failures(tmp_path: Path) -> None:
    """Post-lock refresh failures should route through the normalized preparation error boundary."""
    bot = _bot(tmp_path)
    coordinator = unwrap_extracted_collaborator(bot._response_runner)
    resolver = unwrap_extracted_collaborator(coordinator.deps.resolver)

    with (
        patch.object(
            resolver,
            "fetch_thread_history",
            new=AsyncMock(side_effect=RuntimeError("repair required")),
        ),
        pytest.raises(PostLockRequestPreparationError) as excinfo,
    ):
        await coordinator._prepare_request_after_lock(
            ResponseRequest(
                room_id="!room:localhost",
                reply_to_event_id="$event",
                thread_id="$thread",
                thread_history=[],
                prompt="hello",
                user_id="@user:localhost",
                requires_full_thread_history=True,
            ),
        )

    assert isinstance(excinfo.value.__cause__, RuntimeError)


@pytest.mark.asyncio
async def test_generate_team_response_helper_sets_queued_signal(tmp_path: Path) -> None:
    """Team responses should raise the same queued-message signal before waiting on the lock."""
    bot = _bot(tmp_path)
    response_envelope = _envelope()
    response_target = response_envelope.target
    coordinator = unwrap_extracted_collaborator(bot._response_runner)
    lifecycle_lock = coordinator._response_lifecycle_lock(response_target)
    queued_signal = coordinator._get_or_create_queued_signal(response_target)
    await lifecycle_lock.acquire()

    try:
        with patch.object(
            ResponseRunner,
            "generate_team_response_helper_locked",
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
                    payload=DispatchPayload(prompt="hello"),
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
    coordinator = unwrap_extracted_collaborator(bot._response_runner)
    lifecycle_lock = coordinator._response_lifecycle_lock(response_target)
    queued_signal = coordinator._get_or_create_queued_signal(response_target)
    observed_pending: list[bool] = []
    second_turn_started = asyncio.Event()
    allow_turns_to_finish = asyncio.Event()

    async def fake_locked(_self: ResponseRunner, *_args: object, **_kwargs: object) -> str:
        observed_pending.append(queued_signal.has_pending_human_messages())
        if len(observed_pending) == 1:
            second_turn_started.set()
        await allow_turns_to_finish.wait()
        return f"$response-{len(observed_pending)}"

    await lifecycle_lock.acquire()
    try:
        with patch.object(ResponseRunner, "generate_response_locked", new=fake_locked):
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
    coordinator = unwrap_extracted_collaborator(bot._response_runner)
    lifecycle_lock = coordinator._response_lifecycle_lock(response_target)
    queued_signal = coordinator._get_or_create_queued_signal(response_target)
    observed_pending: list[bool] = []
    second_turn_started = asyncio.Event()
    allow_turns_to_finish = asyncio.Event()

    async def fake_locked(_self: ResponseRunner, *_args: object, **_kwargs: object) -> str:
        observed_pending.append(queued_signal.has_pending_human_messages())
        if len(observed_pending) == 1:
            second_turn_started.set()
        await allow_turns_to_finish.wait()
        return f"$team-response-{len(observed_pending)}"

    await lifecycle_lock.acquire()
    try:
        with patch.object(ResponseRunner, "generate_team_response_helper_locked", new=fake_locked):
            task_b = asyncio.create_task(
                bot._generate_team_response_helper(
                    room_id="!room:localhost",
                    reply_to_event_id="$event",
                    thread_id=None,
                    team_agents=[],
                    team_mode="coordinate",
                    thread_history=[],
                    requester_user_id="@user:localhost",
                    payload=DispatchPayload(prompt="hello"),
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
                    payload=DispatchPayload(prompt="hello again"),
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
    dispatch = PreparedDispatch(
        requester_user_id="@user:localhost",
        context=_message_context(),
        target=envelope.target,
        correlation_id="corr",
        envelope=envelope,
    )

    with (
        patch.object(bot._inbound_turn_normalizer, "resolve_text_event", new=AsyncMock(return_value=event)),
        patch.object(bot._turn_controller, "_prepare_dispatch", new=AsyncMock(return_value=dispatch)),
        patch.object(bot._conversation_resolver, "hydrate_dispatch_context", new=AsyncMock()),
        patch.object(bot._turn_controller, "_has_newer_unresponded_in_thread", return_value=True),
        patch.object(
            bot._turn_policy,
            "plan_turn",
            new=AsyncMock(return_value=DispatchPlan(kind="ignore")),
        ) as mock_plan,
    ):
        await bot._turn_controller._dispatch_text_message(
            room,
            _PrecheckedEvent(event=event, requester_user_id="@user:localhost"),
        )

    assert bot._turn_store.is_handled("$older")
    mock_plan.assert_not_awaited()
    coordinator = unwrap_extracted_collaborator(bot._response_runner)
    assert coordinator._thread_queued_signals == {}


def test_notice_hook_keeps_single_notice_at_end_and_skips_stop_after_tool_call() -> None:
    """The injected notice should stay unique, remain last, avoid double wrapping, and skip stop-after-tool-call results."""
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
    assert queued_messages[-1].content == QUEUED_MESSAGE_NOTICE_TEXT
    assert _notice_count(next_turn_messages) == 1
    assert next_turn_messages[-1].content == QUEUED_MESSAGE_NOTICE_TEXT
    assert _notice_count(stop_after_messages) == 0


def test_notice_reinjects_at_end_across_multiple_tool_rounds() -> None:
    """Repeated tool rounds should keep exactly one queued notice at the end of the prompt."""
    model = _FakeModel()
    install_queued_message_notice_hook(model)

    with queued_message_signal_context(_StaticQueuedState(pending=True)):
        messages = [Message(role="user", content="hello")]
        for index in range(5):
            model.format_function_call_results(
                messages=messages,
                function_call_results=[Message(role="tool", content=f"result {index}")],
            )

            assert _notice_count(messages) == 1
            assert messages[-1].content == QUEUED_MESSAGE_NOTICE_TEXT


def test_stop_after_tool_call_strips_stale_notice_without_readding() -> None:
    """A stop-after-tool-call round should remove any stale queued notice and not append a new one."""
    model = _FakeModel()
    install_queued_message_notice_hook(model)

    messages = [Message(role="user", content="hello")]
    with queued_message_signal_context(_StaticQueuedState(pending=True)):
        model.format_function_call_results(
            messages=messages,
            function_call_results=[Message(role="tool", content="result")],
        )
        model.format_function_call_results(
            messages=messages,
            function_call_results=[Message(role="tool", content="done", stop_after_tool_call=True)],
        )

    assert _notice_count(messages) == 0
    assert messages[-1].content == "done"


def test_notice_reinjects_after_media_follow_up_message() -> None:
    """Agno appends media follow-up messages after tool formatting, so the queued notice must be reappended."""
    model = _FakeModel()
    install_queued_message_notice_hook(model)

    with queued_message_signal_context(_StaticQueuedState(pending=True)):
        messages = [Message(role="user", content="hello")]
        function_call_results = [
            Message(
                role="tool",
                content="generated image",
                images=[Image(url="https://example.com/image.png")],
            ),
        ]
        model.format_function_call_results(
            messages=messages,
            function_call_results=function_call_results,
        )
        model._handle_function_call_media(
            messages=messages,
            function_call_results=function_call_results,
        )

    assert _notice_count(messages) == 1
    assert messages[-2].content == "Take note of the following content"
    assert messages[-1].content == QUEUED_MESSAGE_NOTICE_TEXT


def test_notice_hook_still_installs_when_media_handler_is_missing() -> None:
    """Missing media support must not disable queued notices for formatted tool results."""
    model = _FakeModelWithoutFunctionCallMedia()
    install_queued_message_notice_hook(model)

    with queued_message_signal_context(_StaticQueuedState(pending=True)):
        messages = [Message(role="user", content="hello")]
        model.format_function_call_results(
            messages=messages,
            function_call_results=[Message(role="tool", content="result")],
        )

    assert _notice_count(messages) == 1
    assert messages[-1].content == QUEUED_MESSAGE_NOTICE_TEXT


@pytest.mark.asyncio
async def test_ai_response_preserves_stale_notice_before_prepare(tmp_path: Path) -> None:
    """Loaded session history should strip stale queued notices before replay."""
    config = _config(tmp_path)
    storage = _FakeStorage()
    storage.session = AgentSession(
        session_id="session-1",
        runs=[
            RunOutput(
                run_id="run-0",
                session_id="session-1",
                messages=[_queued_notice_message()],
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
    ) -> _PreparedAgentRun:
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
        return _prepared_run(agent)

    with (
        patch(
            "mindroom.ai.open_resolved_scope_session_context",
            side_effect=lambda **_kwargs: _open_scope(storage),
        ),
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
async def test_ai_response_preserves_notice_in_run_output_and_session(tmp_path: Path) -> None:
    """Non-streaming runs should strip the hidden notice from returned and persisted history."""
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
        patch(
            "mindroom.ai.open_resolved_scope_session_context",
            side_effect=lambda **_kwargs: _open_scope(storage),
        ),
        patch("mindroom.ai._prepare_agent_and_prompt", new=AsyncMock(return_value=_prepared_run(agent))),
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
async def test_ai_response_preserves_notice_in_session_after_exception(tmp_path: Path) -> None:
    """Non-streaming failures should still scrub persisted notices."""
    config = _config(tmp_path)
    storage = _FakeStorage()
    model = _FakeModel()

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
        storage.session = AgentSession(
            session_id=session_id,
            runs=[RunOutput(run_id="run-1", session_id=session_id, messages=messages)],
        )
        error_message = "boom"
        raise RuntimeError(error_message)

    agent = MagicMock()
    agent.model = model
    agent.arun = AsyncMock(side_effect=fake_arun)

    with (
        patch(
            "mindroom.ai.open_resolved_scope_session_context",
            side_effect=lambda **_kwargs: _open_scope(storage),
        ),
        patch("mindroom.ai._prepare_agent_and_prompt", new=AsyncMock(return_value=_prepared_run(agent))),
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

    assert isinstance(response, str)
    assert storage.upserted is True
    assert storage.session is not None
    assert _notice_count(storage.session.runs[0].messages or []) == 0


@pytest.mark.asyncio
async def test_stream_agent_response_preserves_notice_in_session(tmp_path: Path) -> None:
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
        patch(
            "mindroom.ai.open_resolved_scope_session_context",
            side_effect=lambda **_kwargs: _open_scope(storage),
        ),
        patch("mindroom.ai._prepare_agent_and_prompt", new=AsyncMock(return_value=_prepared_run(agent))),
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
        patch("mindroom.model_loading.get_model_instance", return_value=model),
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


def test_cleanup_queued_notice_state_strips_nested_team_member_responses() -> None:
    """Team cleanup should recurse into nested member responses."""
    run_output = TeamRunOutput(
        run_id="run-1",
        session_id="session-1",
        messages=[_queued_notice_message()],
        member_responses=[
            RunOutput(
                run_id="member-run-1",
                session_id="session-1",
                messages=[_queued_notice_message()],
            ),
        ],
        status=RunStatus.completed,
    )
    storage = _FakeStorage()
    storage.session = TeamSession(
        session_id="session-1",
        runs=[
            TeamRunOutput(
                run_id="run-1",
                session_id="session-1",
                messages=[_queued_notice_message()],
                member_responses=[
                    RunOutput(
                        run_id="member-run-1",
                        session_id="session-1",
                        messages=[_queued_notice_message()],
                    ),
                ],
                status=RunStatus.completed,
            ),
        ],
    )

    cleanup_queued_notice_state(
        run_output=run_output,
        storage=storage,
        session_id="session-1",
        session_type=SessionType.TEAM,
        entity_name="queued-notice-team",
    )

    assert _notice_count(run_output.messages or []) == 0
    assert run_output.member_responses is not None
    nested_member_run = run_output.member_responses[0]
    assert isinstance(nested_member_run, RunOutput)
    assert _notice_count(nested_member_run.messages or []) == 0
    assert storage.upserted is True
    assert storage.session is not None
    stored_team_run = storage.session.runs[0]
    assert isinstance(stored_team_run, TeamRunOutput)
    assert _notice_count(stored_team_run.messages or []) == 0
    assert stored_team_run.member_responses is not None
    stored_member_run = stored_team_run.member_responses[0]
    assert isinstance(stored_member_run, RunOutput)
    assert _notice_count(stored_member_run.messages or []) == 0
