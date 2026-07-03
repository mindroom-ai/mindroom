"""ResponseRunner agent-turn lifecycle: process_and_respond, generate_response_locked, session hooks, and interrupted-history persistence."""

from __future__ import annotations

import asyncio
from contextlib import suppress
from dataclasses import replace
from types import SimpleNamespace
from typing import TYPE_CHECKING, cast
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from agno.db.base import SessionType
from agno.models.message import Message
from agno.models.response import ToolExecution
from agno.run.agent import (
    RunContentEvent,
    RunErrorEvent,
    RunOutput,
    ToolCallCompletedEvent,
    ToolCallStartedEvent,
)
from agno.run.base import RunStatus
from agno.session.agent import AgentSession

from mindroom.ai import (
    _PreparedAgentRun,
)
from mindroom.bot import AgentBot
from mindroom.cancellation import USER_STOP_CANCEL_MSG
from mindroom.config.agent import AgentConfig, AgentPrivateConfig
from mindroom.config.main import Config
from mindroom.config.models import ModelConfig
from mindroom.dispatch_source import MESSAGE_SOURCE_KIND
from mindroom.final_delivery import FinalDeliveryOutcome, StreamTransportOutcome
from mindroom.history import PreparedHistoryState
from mindroom.history.turn_recorder import TurnRecorder
from mindroom.history.types import HistoryScope
from mindroom.hooks import (
    BUILTIN_EVENT_NAMES,
    EVENT_MESSAGE_CANCELLED,
    EVENT_SESSION_STARTED,
    CancelledResponseContext,
    HookRegistry,
    MessageEnvelope,
    SessionHookContext,
    hook,
)
from mindroom.hooks.types import default_timeout_ms_for_event, validate_event_name
from mindroom.media_inputs import MediaInputs
from mindroom.message_target import MessageTarget
from mindroom.response_runner import (
    ResponseRequest,
    ResponseRunner,
    _NonStreamingGeneration,
)
from mindroom.streaming import StreamingDeliveryError, strip_visible_tool_markers
from mindroom.tool_system.events import ToolTraceEntry
from mindroom.tool_system.runtime_context import (
    get_tool_runtime_context,
)
from mindroom.tool_system.worker_routing import (
    private_instance_scope_root_path,
    resolve_worker_key,
)
from tests.ai_user_id_helpers import (
    _build_response_runner,
    _config,
    _config_with_matrix_message,
    _knowledge_access_support,
    _make_bot,
    _open_agent_scope_context,
    _plugin,
    _prepared_prompt_result,
    _response_request,
    _runtime_paths,
    _SessionStorage,
    _set_gateway_method,
    _stream_outcome,
    _visible_response_event_id,
    bind_runtime_paths,
)
from tests.conftest import (
    message_origin,
    request_envelope,
)

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Awaitable, Callable
    from pathlib import Path

    from agno.session.team import TeamSession


def test_persist_interrupted_turn_closes_storage_after_write(tmp_path: Path) -> None:
    """Runner-owned interrupted replay should always close the opened storage handle."""
    runtime_paths = _runtime_paths(tmp_path)
    config = bind_runtime_paths(_config(), runtime_paths)
    bot = _make_bot(tmp_path, config=config, runtime_paths=runtime_paths)
    coordinator = _build_response_runner(
        bot,
        config=config,
        runtime_paths=runtime_paths,
        storage_path=tmp_path,
        requester_id="@alice:localhost",
    )
    storage = MagicMock()
    coordinator.deps.state_writer.create_storage = MagicMock(return_value=storage)
    recorder = TurnRecorder(user_message="Hello")
    recorder.mark_interrupted()

    with patch("mindroom.response_runner.persist_interrupted_replay_snapshot") as mock_persist:
        coordinator._persist_interrupted_turn(
            recorder=recorder,
            session_scope=HistoryScope(kind="agent", scope_id="general"),
            session_id="session1",
            execution_identity=None,
            run_id="run-1",
            is_team=False,
        )

    mock_persist.assert_called_once()
    storage.close.assert_called_once_with()


def test_persist_interrupted_turn_closes_storage_when_write_fails(tmp_path: Path) -> None:
    """Runner-owned interrupted replay should close storage even if persistence raises."""
    runtime_paths = _runtime_paths(tmp_path)
    config = bind_runtime_paths(_config(), runtime_paths)
    bot = _make_bot(tmp_path, config=config, runtime_paths=runtime_paths)
    coordinator = _build_response_runner(
        bot,
        config=config,
        runtime_paths=runtime_paths,
        storage_path=tmp_path,
        requester_id="@alice:localhost",
    )
    storage = MagicMock()
    coordinator.deps.state_writer.create_storage = MagicMock(return_value=storage)
    recorder = TurnRecorder(user_message="Hello")
    recorder.mark_interrupted()

    with (
        patch(
            "mindroom.response_runner.persist_interrupted_replay_snapshot",
            side_effect=RuntimeError("boom"),
        ),
        pytest.raises(RuntimeError, match="boom"),
    ):
        coordinator._persist_interrupted_turn(
            recorder=recorder,
            session_scope=HistoryScope(kind="agent", scope_id="general"),
            session_id="session1",
            execution_identity=None,
            run_id="run-1",
            is_team=False,
        )

    storage.close.assert_called_once_with()


@pytest.mark.asyncio
@pytest.mark.parametrize("use_streaming", [False, True])
async def test_generate_response_emits_cancelled_hook_once_for_empty_prompt(
    tmp_path: Path,
    use_streaming: bool,
) -> None:
    """Blank prompts should emit one canonical message:cancelled hook through lifecycle finalization."""
    runtime_paths = _runtime_paths(tmp_path)
    config = bind_runtime_paths(_config(), runtime_paths)
    bot = _make_bot(tmp_path, config=config, runtime_paths=runtime_paths)

    with (
        patch("mindroom.response_runner.should_use_streaming", new=AsyncMock(return_value=use_streaming)),
        patch("mindroom.response_lifecycle.apply_post_response_effects", new=AsyncMock(return_value=None)),
    ):
        coordinator = _build_response_runner(
            bot,
            config=config,
            runtime_paths=runtime_paths,
            storage_path=tmp_path,
            requester_id="@alice:localhost",
        )
        coordinator.deps.delivery_gateway.deps.response_hooks.emit_cancelled_response.reset_mock()

        response_event_id = await coordinator.generate_response(
            _response_request(prompt="   ", user_id="@alice:localhost"),
        )

    assert response_event_id is None
    coordinator.deps.delivery_gateway.deps.response_hooks.emit_cancelled_response.assert_awaited_once()
    assert (
        coordinator.deps.delivery_gateway.deps.response_hooks.emit_cancelled_response.await_args.kwargs[
            "failure_reason"
        ]
        == "empty_prompt"
    )


@pytest.mark.asyncio
async def test_process_and_respond_propagates_before_response_cancellation_to_runner(
    tmp_path: Path,
) -> None:
    """Pre-send before_response cancellation must reach the runner cancellation handler."""
    runtime_paths = _runtime_paths(tmp_path)
    config = bind_runtime_paths(_config(), runtime_paths)
    bot = _make_bot(tmp_path, config=config, runtime_paths=runtime_paths)

    with patch("mindroom.response_runner.ai_response", new=AsyncMock(return_value="Hello!")):
        coordinator = _build_response_runner(
            bot,
            config=config,
            runtime_paths=runtime_paths,
            storage_path=tmp_path,
            requester_id="@alice:localhost",
        )
        coordinator._persist_interrupted_recorder = MagicMock()
        coordinator.deps.delivery_gateway.deps.response_hooks.apply_before_response = AsyncMock(
            side_effect=asyncio.CancelledError(USER_STOP_CANCEL_MSG),
        )

        with pytest.raises(asyncio.CancelledError, match=USER_STOP_CANCEL_MSG):
            await coordinator.process_and_respond(
                ResponseRequest(
                    thread_history=(),
                    prompt="Hello",
                    response_envelope=request_envelope(
                        room_id="!test:localhost",
                        reply_to_event_id="$user_msg",
                        thread_id="$thread-root",
                        prompt="Hello",
                        user_id="@alice:localhost",
                    ),
                    user_id="@alice:localhost",
                    existing_event_id="$thinking",
                    existing_event_is_placeholder=True,
                ),
                run_id="run-1",
            )

    coordinator._persist_interrupted_recorder.assert_called_once()
    coordinator.deps.delivery_gateway.deps.redact_message_event.assert_awaited_once()


@pytest.mark.asyncio
async def test_process_and_respond_streaming_preserves_user_stop_outcome(
    tmp_path: Path,
) -> None:
    """Explicit user-stop during streamed delivery should finalize once through the locked path."""
    runtime_paths = _runtime_paths(tmp_path)
    config = bind_runtime_paths(_config(), runtime_paths)
    bot = _make_bot(tmp_path, config=config, runtime_paths=runtime_paths)

    with (
        patch("mindroom.response_runner.should_use_streaming", new=AsyncMock(return_value=True)),
        patch("mindroom.response_lifecycle.apply_post_response_effects", new=AsyncMock(return_value=None)),
    ):
        coordinator = _build_response_runner(
            bot,
            config=config,
            runtime_paths=runtime_paths,
            storage_path=tmp_path,
            requester_id="@alice:localhost",
            message_target=MessageTarget.resolve("!test:localhost", "$thread-root", "$user_msg"),
        )
        expected_outcome = FinalDeliveryOutcome(
            terminal_status="cancelled",
            event_id="$streaming",
            is_visible_response=True,
            final_visible_body="partial answer\n\n**[Response cancelled by user]**",
            failure_reason="cancelled_by_user",
        )
        coordinator.generate_streaming_ai_response = AsyncMock(
            side_effect=StreamingDeliveryError(
                asyncio.CancelledError(USER_STOP_CANCEL_MSG),
                event_id="$streaming",
                accumulated_text="partial answer",
                tool_trace=[],
                transport_outcome=_stream_outcome(
                    "$streaming",
                    "partial answer\n\n**[Response cancelled by user]**",
                    terminal_status="cancelled",
                    failure_reason="cancelled_by_user",
                ),
            ),
        )
        _set_gateway_method(
            coordinator.deps.delivery_gateway,
            "finalize_streamed_response",
            AsyncMock(return_value=expected_outcome),
        )
        coordinator.deps.delivery_gateway.deps.response_hooks.emit_cancelled_response.reset_mock()

        response_event_id = await coordinator.generate_response_locked(
            replace(
                _response_request(
                    prompt="Hello",
                    user_id="@alice:localhost",
                    thread_id="$thread-root",
                ),
                existing_event_id="$streaming",
            ),
            resolved_target=MessageTarget.resolve("!test:localhost", "$thread-root", "$user_msg"),
        )

    assert response_event_id == "$streaming"
    coordinator.deps.delivery_gateway.finalize_streamed_response.assert_awaited_once()
    coordinator.deps.delivery_gateway.deps.response_hooks.emit_cancelled_response.assert_awaited_once()
    assert (
        coordinator.deps.delivery_gateway.deps.response_hooks.emit_cancelled_response.await_args.kwargs[
            "failure_reason"
        ]
        == "cancelled_by_user"
    )


def test_session_started_event_is_registered() -> None:
    """session:started should be a built-in event with the expected default timeout."""
    assert EVENT_SESSION_STARTED in BUILTIN_EVENT_NAMES
    assert validate_event_name(EVENT_SESSION_STARTED) == EVENT_SESSION_STARTED
    with pytest.raises(ValueError, match="reserved namespace"):
        validate_event_name("session:custom")
    assert default_timeout_ms_for_event(EVENT_SESSION_STARTED) == 5000


@pytest.mark.asyncio
async def test_process_and_respond_emits_session_started_after_first_persisted_thread_response(
    tmp_path: Path,
) -> None:
    """The first persisted thread response should emit session:started before delivery finalization."""
    runtime_paths = _runtime_paths(tmp_path)
    config = bind_runtime_paths(_config(), runtime_paths)
    bot = MagicMock(spec=AgentBot)
    bot.logger = MagicMock()
    bot.stop_manager = MagicMock()
    bot.stop_manager.remove_stop_button = AsyncMock()
    bot.client = AsyncMock()
    bot.agent_name = "general"
    bot.storage_path = tmp_path
    bot.config = config
    bot.runtime_paths = runtime_paths
    bot._knowledge_access_support = _knowledge_access_support()

    storage = _SessionStorage()
    sequence: list[tuple[str, str | None, str | None, str | None]] = []
    saw_matrix_admin: list[bool] = []

    @hook(EVENT_SESSION_STARTED, priority=10)
    async def first(ctx: SessionHookContext) -> None:
        saw_matrix_admin.append(ctx.matrix_admin is not None)
        sequence.append(("first", ctx.scope.key, ctx.session_id, ctx.thread_id))

    @hook(EVENT_SESSION_STARTED, priority=20)
    async def second(ctx: SessionHookContext) -> None:
        sequence.append(("second", ctx.scope.key, None, None))

    registry = HookRegistry.from_plugins([_plugin("session-hooks", [first, second])])

    with (
        patch("mindroom.response_runner.should_use_streaming", new=AsyncMock(return_value=False)),
        patch("mindroom.response_runner.ai_response", new_callable=AsyncMock) as mock_ai,
    ):
        coordinator = _build_response_runner(
            bot,
            config=config,
            runtime_paths=runtime_paths,
            storage_path=tmp_path,
            requester_id="@alice:localhost",
            hook_registry=registry,
            history_storage=storage,
            message_target=MessageTarget.resolve("!test:localhost", "$thread-root", "$user_msg"),
            orchestrator=SimpleNamespace(
                hook_matrix_admin=MagicMock(return_value=object()),
                hook_room_state_querier=MagicMock(return_value=None),
                hook_room_state_putter=MagicMock(return_value=None),
                knowledge_refresh_scheduler=SimpleNamespace(
                    schedule_refresh=lambda _base_id: None,
                    is_refreshing=lambda _base_id: False,
                ),
            ),
        )

        async def fake_ai_response(*_args: object, **_kwargs: object) -> str:
            context = get_tool_runtime_context()
            assert context is not None
            storage.session = AgentSession(
                session_id=context.session_id or "",
                agent_id="general",
                created_at=1,
                updated_at=1,
            )
            sequence.append(("ai", context.session_id, None, None))
            return "Hello!"

        mock_ai.side_effect = fake_ai_response
        _set_gateway_method(
            coordinator.deps.delivery_gateway,
            "deliver_final",
            AsyncMock(
                side_effect=lambda *_args, **_kwargs: (
                    sequence.append(("deliver", None, None, None))
                    or MagicMock(
                        event_id="$response_id",
                        response_text="Hello!",
                        delivery_kind="sent",
                    )
                ),
            ),
        )

        await coordinator.process_and_respond(
            _response_request(prompt="Hello", user_id="@alice:localhost", thread_id="$thread-root"),
        )
        await coordinator.process_and_respond(
            _response_request(prompt="Hello again", user_id="@alice:localhost", thread_id="$thread-root"),
        )

    assert sequence == [
        ("ai", "!test:localhost:$thread-root", None, None),
        ("first", "agent:general", "!test:localhost:$thread-root", "$thread-root"),
        ("second", "agent:general", None, None),
        ("deliver", None, None, None),
        ("ai", "!test:localhost:$thread-root", None, None),
        ("deliver", None, None, None),
    ]
    assert saw_matrix_admin == [True]


@pytest.mark.asyncio
async def test_process_and_respond_applies_session_started_agent_and_room_scopes(tmp_path: Path) -> None:
    """session:started hooks should respect agent and room decorator scopes."""
    runtime_paths = _runtime_paths(tmp_path)
    config = bind_runtime_paths(_config(), runtime_paths)
    bot = MagicMock(spec=AgentBot)
    bot.logger = MagicMock()
    bot.stop_manager = MagicMock()
    bot.stop_manager.remove_stop_button = AsyncMock()
    bot.client = AsyncMock()
    bot.agent_name = "general"
    bot.storage_path = tmp_path
    bot.config = config
    bot.runtime_paths = runtime_paths
    bot._knowledge_access_support = _knowledge_access_support()

    storage = _SessionStorage()
    sequence: list[str] = []

    @hook(EVENT_SESSION_STARTED, agents=["general"], rooms=["!test:localhost"])
    async def matching(ctx: SessionHookContext) -> None:
        sequence.append(f"{ctx.scope.key}:{ctx.agent_name}:{ctx.room_id}:{ctx.thread_id}")

    @hook(EVENT_SESSION_STARTED, agents=["other"], rooms=["!test:localhost"])
    async def wrong_agent(ctx: SessionHookContext) -> None:
        sequence.append(f"wrong-agent:{ctx.agent_name}")

    @hook(EVENT_SESSION_STARTED, agents=["general"], rooms=["!elsewhere:localhost"])
    async def wrong_room(ctx: SessionHookContext) -> None:
        sequence.append(f"wrong-room:{ctx.room_id}")

    registry = HookRegistry.from_plugins([_plugin("session-hooks", [matching, wrong_agent, wrong_room])])

    with (
        patch("mindroom.response_runner.should_use_streaming", new=AsyncMock(return_value=False)),
        patch("mindroom.response_runner.ai_response", new_callable=AsyncMock) as mock_ai,
    ):
        coordinator = _build_response_runner(
            bot,
            config=config,
            runtime_paths=runtime_paths,
            storage_path=tmp_path,
            requester_id="@alice:localhost",
            hook_registry=registry,
            history_storage=storage,
            message_target=MessageTarget.resolve("!test:localhost", "$thread-root", "$user_msg"),
        )

        async def fake_ai_response(*_args: object, **_kwargs: object) -> str:
            context = get_tool_runtime_context()
            assert context is not None
            storage.session = AgentSession(
                session_id=context.session_id or "",
                agent_id="general",
                created_at=1,
                updated_at=1,
            )
            sequence.append("ai")
            return "Hello!"

        mock_ai.side_effect = fake_ai_response

        await coordinator.process_and_respond(
            _response_request(prompt="Hello", user_id="@alice:localhost", thread_id="$thread-root"),
        )

    assert sequence == ["ai", "agent:general:general:!test:localhost:$thread-root"]


@pytest.mark.asyncio
async def test_process_and_respond_does_not_emit_session_started_without_persisted_session(tmp_path: Path) -> None:
    """session:started should not fire when the run never creates a persisted session."""
    runtime_paths = _runtime_paths(tmp_path)
    config = bind_runtime_paths(_config(), runtime_paths)
    bot = MagicMock(spec=AgentBot)
    bot.logger = MagicMock()
    bot.stop_manager = MagicMock()
    bot.stop_manager.remove_stop_button = AsyncMock()
    bot.client = AsyncMock()
    bot.agent_name = "general"
    bot.storage_path = tmp_path
    bot.config = config
    bot.runtime_paths = runtime_paths
    bot._knowledge_access_support = _knowledge_access_support()

    storage = _SessionStorage()
    sequence: list[str] = []

    @hook(EVENT_SESSION_STARTED)
    async def started(_ctx: SessionHookContext) -> None:
        sequence.append("started")

    registry = HookRegistry.from_plugins([_plugin("session-hooks", [started])])

    with (
        patch("mindroom.response_runner.should_use_streaming", new=AsyncMock(return_value=False)),
        patch("mindroom.response_runner.ai_response", new_callable=AsyncMock) as mock_ai,
    ):
        coordinator = _build_response_runner(
            bot,
            config=config,
            runtime_paths=runtime_paths,
            storage_path=tmp_path,
            requester_id="@alice:localhost",
            hook_registry=registry,
            history_storage=storage,
            message_target=MessageTarget.resolve("!test:localhost", "$thread-root", "$user_msg"),
        )

        async def fake_ai_response(*_args: object, **_kwargs: object) -> str:
            sequence.append("ai")
            return "Hello!"

        mock_ai.side_effect = fake_ai_response

        await coordinator.process_and_respond(
            _response_request(prompt="Hello", user_id="@alice:localhost", thread_id="$thread-root"),
        )

    assert sequence == ["ai"]


@pytest.mark.asyncio
async def test_should_watch_session_started_returns_false_when_storage_probe_fails(
    tmp_path: Path,
) -> None:
    """session:started eligibility should degrade to False when the session probe fails."""
    runtime_paths = _runtime_paths(tmp_path)
    config = bind_runtime_paths(_config(), runtime_paths)
    bot = _make_bot(tmp_path, config=config, runtime_paths=runtime_paths)

    @hook(EVENT_SESSION_STARTED)
    async def started(_ctx: SessionHookContext) -> None:
        return None

    class BrokenStorage:
        def get_session(self, _session_id: str, _session_type: object) -> AgentSession | TeamSession | None:
            msg = "probe boom"
            raise RuntimeError(msg)

        def close(self) -> None:
            return None

    registry = HookRegistry.from_plugins([_plugin("session-hooks", [started])])
    coordinator = _build_response_runner(
        bot,
        config=config,
        runtime_paths=runtime_paths,
        storage_path=tmp_path,
        requester_id="@alice:localhost",
        hook_registry=registry,
        message_target=MessageTarget.resolve("!test:localhost", "$thread-root", "$user_msg"),
    )
    target = MessageTarget.resolve("!test:localhost", "$thread-root", "$user_msg")
    tool_context = coordinator.deps.tool_runtime.build_context(
        target,
        user_id="@alice:localhost",
        session_id=target.session_id,
    )

    watch_request = _response_request(prompt="Hello", user_id="@alice:localhost", thread_id="$thread-root")
    lifecycle = coordinator._build_lifecycle(
        identity=coordinator._response_identity(watch_request, response_kind="ai"),
        request=watch_request,
    )
    watch = lifecycle.setup_session_watch(
        tool_context=tool_context,
        session_id=target.session_id,
        session_type=SessionType.AGENT,
        scope=HistoryScope(kind="agent", scope_id="general"),
        room_id=target.room_id,
        thread_id=target.resolved_thread_id,
        create_storage=BrokenStorage,
    )

    assert watch.should_watch is False
    coordinator.deps.logger.exception.assert_called_once()
    assert coordinator.deps.logger.exception.call_args.kwargs["session_id"] == target.session_id
    assert coordinator.deps.logger.exception.call_args.kwargs["failure_reason"] == "probe boom"


@pytest.mark.asyncio
async def test_session_started_hooks_continue_after_timeout(tmp_path: Path) -> None:
    """A timed-out session hook should not block later session hooks or the response itself."""
    runtime_paths = _runtime_paths(tmp_path)
    config = bind_runtime_paths(_config(), runtime_paths)
    bot = MagicMock(spec=AgentBot)
    bot.logger = MagicMock()
    bot.stop_manager = MagicMock()
    bot.stop_manager.remove_stop_button = AsyncMock()
    bot.client = AsyncMock()
    bot.agent_name = "general"
    bot.storage_path = tmp_path
    bot.config = config
    bot.runtime_paths = runtime_paths
    bot._knowledge_access_support = _knowledge_access_support()

    storage = _SessionStorage()
    sequence: list[str] = []

    @hook(EVENT_SESSION_STARTED, priority=10, timeout_ms=10)
    async def slow(_ctx: SessionHookContext) -> None:
        sequence.append("slow")
        await asyncio.sleep(0.05)

    @hook(EVENT_SESSION_STARTED, priority=20)
    async def fast(ctx: SessionHookContext) -> None:
        sequence.append(f"fast:{ctx.thread_id}")

    registry = HookRegistry.from_plugins([_plugin("session-hooks", [slow, fast])])

    with (
        patch("mindroom.response_runner.should_use_streaming", new=AsyncMock(return_value=False)),
        patch("mindroom.response_runner.ai_response", new_callable=AsyncMock) as mock_ai,
    ):
        coordinator = _build_response_runner(
            bot,
            config=config,
            runtime_paths=runtime_paths,
            storage_path=tmp_path,
            requester_id="@alice:localhost",
            hook_registry=registry,
            history_storage=storage,
            message_target=MessageTarget.resolve("!test:localhost", "$thread-root", "$user_msg"),
        )

        async def fake_ai_response(*_args: object, **_kwargs: object) -> str:
            context = get_tool_runtime_context()
            assert context is not None
            storage.session = AgentSession(
                session_id=context.session_id or "",
                agent_id="general",
                created_at=1,
                updated_at=1,
            )
            sequence.append("ai")
            return "Hello!"

        mock_ai.side_effect = fake_ai_response

        await coordinator.process_and_respond(
            _response_request(prompt="Hello", user_id="@alice:localhost", thread_id="$thread-root"),
        )

    assert sequence == ["ai", "slow", "fast:$thread-root"]


@pytest.mark.asyncio
async def test_session_started_hooks_continue_after_runtime_error(tmp_path: Path) -> None:
    """A failed session hook should fail open and let later hooks and delivery finish."""
    runtime_paths = _runtime_paths(tmp_path)
    config = bind_runtime_paths(_config(), runtime_paths)
    bot = _make_bot(tmp_path, config=config, runtime_paths=runtime_paths)

    storage = _SessionStorage()
    sequence: list[str] = []

    @hook(EVENT_SESSION_STARTED, priority=10)
    async def failing(_ctx: SessionHookContext) -> None:
        sequence.append("failed")
        msg = "hook failed"
        raise RuntimeError(msg)

    @hook(EVENT_SESSION_STARTED, priority=20)
    async def fast(ctx: SessionHookContext) -> None:
        sequence.append(f"fast:{ctx.thread_id}")

    registry = HookRegistry.from_plugins([_plugin("session-hooks", [failing, fast])])

    with (
        patch("mindroom.response_runner.should_use_streaming", new=AsyncMock(return_value=False)),
        patch("mindroom.response_runner.ai_response", new_callable=AsyncMock) as mock_ai,
    ):
        coordinator = _build_response_runner(
            bot,
            config=config,
            runtime_paths=runtime_paths,
            storage_path=tmp_path,
            requester_id="@alice:localhost",
            hook_registry=registry,
            history_storage=storage,
            message_target=MessageTarget.resolve("!test:localhost", "$thread-root", "$user_msg"),
        )
        _set_gateway_method(
            coordinator.deps.delivery_gateway,
            "deliver_final",
            AsyncMock(
                side_effect=lambda *_args, **_kwargs: (
                    sequence.append("deliver")
                    or MagicMock(
                        event_id="$response_id",
                        response_text="Hello!",
                        delivery_kind="sent",
                    )
                ),
            ),
        )

        async def fake_ai_response(*_args: object, **_kwargs: object) -> str:
            context = get_tool_runtime_context()
            assert context is not None
            storage.session = AgentSession(
                session_id=context.session_id or "",
                agent_id="general",
                created_at=1,
                updated_at=1,
            )
            sequence.append("ai")
            return "Hello!"

        mock_ai.side_effect = fake_ai_response

        await coordinator.process_and_respond(
            _response_request(prompt="Hello", user_id="@alice:localhost", thread_id="$thread-root"),
        )

    assert sequence == ["ai", "failed", "fast:$thread-root", "deliver"]


@pytest.mark.asyncio
async def test_process_and_respond_streaming_emits_session_started_after_persisted_delivery_error(
    tmp_path: Path,
) -> None:
    """session:started should still fire when streaming delivery fails after the session is persisted."""
    runtime_paths = _runtime_paths(tmp_path)
    config = bind_runtime_paths(_config(), runtime_paths)
    bot = MagicMock(spec=AgentBot)
    bot.logger = MagicMock()
    bot.stop_manager = MagicMock()
    bot.stop_manager.remove_stop_button = AsyncMock()
    bot.client = AsyncMock()
    bot.agent_name = "general"
    bot.storage_path = tmp_path
    bot.config = config
    bot.runtime_paths = runtime_paths
    bot._knowledge_access_support = _knowledge_access_support()

    storage = _SessionStorage()
    sequence: list[str] = []

    @hook(EVENT_SESSION_STARTED)
    async def started(ctx: SessionHookContext) -> None:
        sequence.append(f"started:{ctx.session_id}:{ctx.thread_id}")

    registry = HookRegistry.from_plugins([_plugin("session-hooks", [started])])

    with patch("mindroom.response_runner.stream_agent_response") as mock_stream:
        coordinator = _build_response_runner(
            bot,
            config=config,
            runtime_paths=runtime_paths,
            storage_path=tmp_path,
            requester_id="@bob:localhost",
            hook_registry=registry,
            history_storage=storage,
            message_target=MessageTarget.resolve("!test:localhost", "$thread-root", "$user_msg"),
        )

        async def consume_delivery_and_fail(request: object) -> StreamTransportOutcome:
            chunks = [chunk async for chunk in request.response_stream]
            accumulated = "".join(chunks)
            sequence.append(f"deliver:{accumulated}")
            raise StreamingDeliveryError(
                RuntimeError("boom"),
                event_id="$terminal",
                accumulated_text=accumulated,
                tool_trace=[],
                transport_outcome=_stream_outcome(
                    "$terminal",
                    accumulated,
                    terminal_status="error",
                    failure_reason="boom",
                ),
            )

        coordinator.deps.delivery_gateway.deliver_stream.side_effect = consume_delivery_and_fail

        def fake_stream_agent_response(*_args: object, **_kwargs: object) -> AsyncIterator[str]:
            async def fake_stream() -> AsyncIterator[str]:
                context = get_tool_runtime_context()
                assert context is not None
                storage.session = AgentSession(
                    session_id=context.session_id or "",
                    agent_id="general",
                    created_at=1,
                    updated_at=1,
                )
                sequence.append("stream")
                yield "Hello!"

            return fake_stream()

        mock_stream.side_effect = fake_stream_agent_response

        generation = await coordinator.process_and_respond_streaming(
            _response_request(prompt="Hello", user_id="@bob:localhost", thread_id="$thread-root"),
        )

    assert generation.delivery.event_id == "$terminal"
    assert generation.delivery.response_text == "Hello!"
    assert sequence == [
        "stream",
        "deliver:Hello!",
        "started:!test:localhost:$thread-root:$thread-root",
    ]


@pytest.mark.asyncio
async def test_process_and_respond_streaming_persists_interrupted_history_when_delivery_fails(
    tmp_path: Path,
) -> None:
    """Stream delivery errors should persist canonical interrupted replay from the partial text."""
    runtime_paths = _runtime_paths(tmp_path)
    config = bind_runtime_paths(_config(), runtime_paths)
    bot = _make_bot(tmp_path, config=config, runtime_paths=runtime_paths)
    storage = _SessionStorage()

    with patch("mindroom.response_runner.stream_agent_response") as mock_stream:
        coordinator = _build_response_runner(
            bot,
            config=config,
            runtime_paths=runtime_paths,
            storage_path=tmp_path,
            requester_id="@bob:localhost",
            history_storage=storage,
            message_target=MessageTarget.resolve("!test:localhost", "$thread-root", "$user_msg"),
        )

        async def consume_delivery_and_fail(request: object) -> StreamTransportOutcome:
            accumulated = ""
            async for chunk in request.response_stream:
                accumulated += str(chunk)
            raise StreamingDeliveryError(
                RuntimeError("boom"),
                event_id="$terminal",
                accumulated_text="Partial answer\n\n**[Response interrupted by an error: boom]**",
                tool_trace=[],
                transport_outcome=_stream_outcome(
                    "$terminal",
                    "Partial answer\n\n**[Response interrupted by an error: boom]**",
                    terminal_status="error",
                    failure_reason="boom",
                ),
            )

        coordinator.deps.delivery_gateway.deliver_stream.side_effect = consume_delivery_and_fail

        def fake_stream_agent_response(*_args: object, **_kwargs: object) -> AsyncIterator[str]:
            async def fake_stream() -> AsyncIterator[str]:
                yield "Partial answer"

            return fake_stream()

        mock_stream.side_effect = fake_stream_agent_response

        generation = await coordinator.process_and_respond_streaming(
            _response_request(prompt="Hello", user_id="@bob:localhost", thread_id="$thread-root"),
        )

    assert generation.delivery.event_id == "$terminal"
    assert generation.delivery.failure_reason == "boom"
    persisted_session = cast("AgentSession", storage.session)
    assert persisted_session is not None
    assert persisted_session.runs is not None
    persisted_run = cast("RunOutput", persisted_session.runs[0])
    assert persisted_run.messages is not None
    assert [(message.role, message.content) for message in persisted_run.messages] == [
        ("user", "Hello"),
        ("assistant", "Partial answer\n\n(turn interrupted by the user before completion)"),
    ]


@pytest.mark.asyncio
async def test_process_and_respond_streaming_persists_interrupted_history_when_model_stream_errors(
    tmp_path: Path,
) -> None:
    """Model stream errors returned as text should still persist interrupted replay."""
    runtime_paths = _runtime_paths(tmp_path)
    config = bind_runtime_paths(_config(), runtime_paths)
    bot = _make_bot(tmp_path, config=config, runtime_paths=runtime_paths)
    storage = _SessionStorage()
    mock_agent = MagicMock()
    mock_agent.model = MagicMock()
    mock_agent.model.__class__.__name__ = "OpenAIChat"
    mock_agent.model.id = "test-model"
    mock_agent.name = "GeneralAgent"
    mock_agent.add_history_to_context = False

    completed_tool = ToolExecution(
        tool_call_id="call-1",
        tool_name="run_shell_command",
        tool_args={"cmd": "pwd"},
        result="/app",
    )

    async def errored_agent_stream() -> AsyncIterator[object]:
        yield RunContentEvent(content="Partial answer")
        yield ToolCallStartedEvent(tool=completed_tool)
        yield ToolCallCompletedEvent(tool=completed_tool)
        yield RunErrorEvent(content="Error code: 500 - provider exploded")

    mock_agent.arun = MagicMock(return_value=errored_agent_stream())

    with patch("mindroom.ai._prepare_agent_and_prompt", new_callable=AsyncMock) as mock_prepare:
        mock_prepare.return_value = _prepared_prompt_result(mock_agent)
        coordinator = _build_response_runner(
            bot,
            config=config,
            runtime_paths=runtime_paths,
            storage_path=tmp_path,
            requester_id="@bob:localhost",
            history_storage=storage,
            message_target=MessageTarget.resolve("!test:localhost", "$thread-root", "$user_msg"),
        )

        async def consume_delivery(request: object) -> StreamTransportOutcome:
            rendered = "".join([str(chunk) async for chunk in request.response_stream])
            request.visible_event_id_callback("$streamed")
            return _stream_outcome("$streamed", rendered)

        coordinator.deps.delivery_gateway.deliver_stream.side_effect = consume_delivery

        generation = await coordinator.process_and_respond_streaming(
            _response_request(prompt="Hello", user_id="@bob:localhost", thread_id="$thread-root"),
            run_id="run-1",
        )

    assert generation.delivery.event_id == "$streamed"
    persisted_session = cast("AgentSession", storage.session)
    assert persisted_session is not None
    assert persisted_session.runs is not None
    persisted_run = cast("RunOutput", persisted_session.runs[0])
    assert persisted_run.run_id == "run-1"
    assert persisted_run.metadata is not None
    assert persisted_run.metadata["matrix_response_event_id"] == "$streamed"
    assert persisted_run.messages is not None
    assert [(message.role, message.content) for message in persisted_run.messages] == [
        ("user", "Hello"),
        (
            "assistant",
            "Partial answer\n\n(turn interrupted by the user before completion; "
            "1 tool call(s) had completed: run_shell_command)",
        ),
    ]


def test_strip_visible_tool_markers_handles_blank_lined_markers() -> None:
    """The tool-marker stripper should leave bodies intact when markers are followed by blank lines."""
    text = "Intro\n\n🔧 `run_shell_command` [1]\n\n---\n\nBody"
    assert strip_visible_tool_markers(text) == "Intro\n\n\nBody"


def test_strip_visible_tool_markers_preserves_marker_free_text_byte_for_byte() -> None:
    """Marker-free text should not be normalized while checking for display chrome."""
    text = "Intro\r\n---\r\nBody with trailing spaces  \r\n\r\n"
    assert strip_visible_tool_markers(text) == text


@pytest.mark.asyncio
async def test_process_and_respond_streaming_delivery_failure_with_visible_tools_replays_tool_trace_once(
    tmp_path: Path,
) -> None:
    """Visible streamed tool markers should be normalized out before interrupted replay persistence."""
    runtime_paths = _runtime_paths(tmp_path)
    config = bind_runtime_paths(_config(), runtime_paths)
    bot = _make_bot(tmp_path, config=config, runtime_paths=runtime_paths)
    storage = _SessionStorage()

    with (
        patch("mindroom.response_runner.stream_agent_response") as mock_stream,
        patch.object(ResponseRunner, "_show_tool_calls", return_value=True),
    ):
        coordinator = _build_response_runner(
            bot,
            config=config,
            runtime_paths=runtime_paths,
            storage_path=tmp_path,
            requester_id="@bob:localhost",
            history_storage=storage,
            message_target=MessageTarget.resolve("!test:localhost", "$thread-root", "$user_msg"),
        )

        async def consume_delivery_and_fail(_request: object) -> StreamTransportOutcome:
            raise StreamingDeliveryError(
                RuntimeError("boom"),
                event_id="$terminal",
                accumulated_text="Partial answer\n\n🔧 `run_shell_command` [1]\n\n**[Response interrupted by an error: boom]**",
                tool_trace=[
                    ToolTraceEntry(
                        type="tool_call_completed",
                        tool_name="run_shell_command",
                        args_preview="cmd=pwd",
                        result_preview="/app",
                    ),
                ],
                transport_outcome=_stream_outcome(
                    "$terminal",
                    "Partial answer\n\n🔧 `run_shell_command` [1]\n\n**[Response interrupted by an error: boom]**",
                    terminal_status="error",
                    failure_reason="boom",
                ),
            )

        coordinator.deps.delivery_gateway.deliver_stream.side_effect = consume_delivery_and_fail

        def fake_stream_agent_response(*_args: object, **_kwargs: object) -> AsyncIterator[str]:
            async def fake_stream() -> AsyncIterator[str]:
                yield "Partial answer"

            return fake_stream()

        mock_stream.side_effect = fake_stream_agent_response

        generation = await coordinator.process_and_respond_streaming(
            _response_request(prompt="Hello", user_id="@bob:localhost", thread_id="$thread-root"),
        )

    assert generation.delivery.event_id == "$terminal"
    persisted_session = cast("AgentSession", storage.session)
    assert persisted_session is not None
    assert persisted_session.runs is not None
    persisted_run = cast("RunOutput", persisted_session.runs[0])
    assistant_text = cast("str", persisted_run.messages[1].content)
    assert "🔧 `run_shell_command` [1]" not in assistant_text
    assert assistant_text.count("run_shell_command") == 1
    assert assistant_text == (
        "Partial answer\n\n(turn interrupted by the user before completion; "
        "1 tool call(s) had completed: run_shell_command)"
    )


@pytest.mark.asyncio
async def test_process_and_respond_emits_session_started_after_persisted_cancellation(
    tmp_path: Path,
) -> None:
    """session:started should still fire when a cancelled run has already persisted the session."""
    runtime_paths = _runtime_paths(tmp_path)
    config = bind_runtime_paths(_config(), runtime_paths)
    bot = MagicMock(spec=AgentBot)
    bot.logger = MagicMock()
    bot.stop_manager = MagicMock()
    bot.stop_manager.remove_stop_button = AsyncMock()
    bot.client = AsyncMock()
    bot.agent_name = "general"
    bot.storage_path = tmp_path
    bot.config = config
    bot.runtime_paths = runtime_paths
    bot._knowledge_access_support = _knowledge_access_support()

    storage = _SessionStorage()
    sequence: list[str] = []

    @hook(EVENT_SESSION_STARTED)
    async def started(ctx: SessionHookContext) -> None:
        sequence.append(f"started:{ctx.session_id}:{ctx.thread_id}")

    registry = HookRegistry.from_plugins([_plugin("session-hooks", [started])])

    with (
        patch("mindroom.response_runner.should_use_streaming", new=AsyncMock(return_value=False)),
        patch("mindroom.response_runner.ai_response", new_callable=AsyncMock) as mock_ai,
    ):
        coordinator = _build_response_runner(
            bot,
            config=config,
            runtime_paths=runtime_paths,
            storage_path=tmp_path,
            requester_id="@alice:localhost",
            hook_registry=registry,
            history_storage=storage,
            message_target=MessageTarget.resolve("!test:localhost", "$thread-root", "$user_msg"),
        )
        _set_gateway_method(coordinator.deps.delivery_gateway, "edit_text", AsyncMock())

        async def fake_ai_response(*_args: object, **_kwargs: object) -> str:
            cancel_message = "cancel"
            context = get_tool_runtime_context()
            assert context is not None
            storage.session = AgentSession(
                session_id=context.session_id or "",
                agent_id="general",
                created_at=1,
                updated_at=1,
            )
            sequence.append("ai")
            raise asyncio.CancelledError(cancel_message)

        mock_ai.side_effect = fake_ai_response

        generation = await coordinator.process_and_respond(
            replace(
                _response_request(prompt="Hello", user_id="@alice:localhost", thread_id="$thread-root"),
                existing_event_id="$thinking",
            ),
        )

    assert generation.delivery.terminal_status == "cancelled"
    assert _visible_response_event_id(generation.delivery) == "$thinking"
    assert sequence == [
        "ai",
        "started:!test:localhost:$thread-root:$thread-root",
    ]


@pytest.mark.asyncio
async def test_process_and_respond_streaming_emits_session_started_after_persisted_cancellation(
    tmp_path: Path,
) -> None:
    """session:started should still fire when streamed delivery is cancelled after persistence."""
    runtime_paths = _runtime_paths(tmp_path)
    config = bind_runtime_paths(_config(), runtime_paths)
    bot = MagicMock(spec=AgentBot)
    bot.logger = MagicMock()
    bot.stop_manager = MagicMock()
    bot.stop_manager.remove_stop_button = AsyncMock()
    bot.client = AsyncMock()
    bot.agent_name = "general"
    bot.storage_path = tmp_path
    bot.config = config
    bot.runtime_paths = runtime_paths
    bot._knowledge_access_support = _knowledge_access_support()

    storage = _SessionStorage()
    sequence: list[str] = []

    @hook(EVENT_SESSION_STARTED)
    async def started(ctx: SessionHookContext) -> None:
        sequence.append(f"started:{ctx.session_id}:{ctx.thread_id}")

    registry = HookRegistry.from_plugins([_plugin("session-hooks", [started])])

    with patch("mindroom.response_runner.stream_agent_response") as mock_stream:
        coordinator = _build_response_runner(
            bot,
            config=config,
            runtime_paths=runtime_paths,
            storage_path=tmp_path,
            requester_id="@bob:localhost",
            hook_registry=registry,
            history_storage=storage,
            message_target=MessageTarget.resolve("!test:localhost", "$thread-root", "$user_msg"),
        )

        async def consume_delivery_and_cancel(request: object) -> StreamTransportOutcome:
            accumulated = ""
            async for chunk in request.response_stream:
                accumulated += str(chunk)
                sequence.append(f"deliver:{accumulated}")
            return _stream_outcome("$msg_id", accumulated)

        coordinator.deps.delivery_gateway.deliver_stream.side_effect = consume_delivery_and_cancel

        def fake_stream_agent_response(*_args: object, **_kwargs: object) -> AsyncIterator[str]:
            async def fake_stream() -> AsyncIterator[str]:
                cancel_message = "cancel"
                context = get_tool_runtime_context()
                assert context is not None
                storage.session = AgentSession(
                    session_id=context.session_id or "",
                    agent_id="general",
                    created_at=1,
                    updated_at=1,
                )
                sequence.append("stream")
                yield "Hello!"
                raise asyncio.CancelledError(cancel_message)

            return fake_stream()

        mock_stream.side_effect = fake_stream_agent_response

        with pytest.raises(asyncio.CancelledError, match="cancel"):
            await coordinator.process_and_respond_streaming(
                replace(
                    _response_request(prompt="Hello", user_id="@bob:localhost", thread_id="$thread-root"),
                    existing_event_id="$thinking",
                ),
            )

    assert sequence == [
        "stream",
        "deliver:Hello!",
        "started:!test:localhost:$thread-root:$thread-root",
    ]


@pytest.mark.asyncio
async def test_generate_response_locked_persists_minimal_interrupted_history_after_task_cancel(
    tmp_path: Path,
) -> None:
    """Lifecycle-owned agent cancellation should persist one minimal interrupted turn."""
    runtime_paths = _runtime_paths(tmp_path)
    config = bind_runtime_paths(_config(), runtime_paths)
    bot = _make_bot(tmp_path, config=config, runtime_paths=runtime_paths)
    storage = _SessionStorage()
    started = asyncio.Event()

    async def fake_run_cancellable_response(**kwargs: object) -> str:
        response_function = cast("Callable[[str | None], Awaitable[object]]", kwargs["response_function"])
        task = asyncio.create_task(response_function("$thinking"))
        await started.wait()
        task.cancel()
        with suppress(asyncio.CancelledError):
            await task
        return "$thinking"

    with (
        patch.object(
            ResponseRunner,
            "run_cancellable_response",
            new=AsyncMock(side_effect=fake_run_cancellable_response),
        ),
        patch("mindroom.response_runner.should_use_streaming", new=AsyncMock(return_value=False)),
        patch("mindroom.response_lifecycle.apply_post_response_effects", new=AsyncMock(return_value=None)),
        patch("mindroom.response_runner.ai_response", new_callable=AsyncMock) as mock_ai,
    ):
        coordinator = _build_response_runner(
            bot,
            config=config,
            runtime_paths=runtime_paths,
            storage_path=tmp_path,
            requester_id="@alice:localhost",
            history_storage=storage,
            message_target=MessageTarget.resolve("!test:localhost", "$thread-root", "$user_msg"),
        )
        _set_gateway_method(coordinator.deps.delivery_gateway, "edit_text", AsyncMock())

        async def fake_ai_response(*_args: object, **kwargs: object) -> str:
            run_id_callback = cast("Callable[[str], None]", kwargs["run_id_callback"])
            run_id_callback("run-retry")
            started.set()
            await asyncio.sleep(60)
            return "unreachable"

        mock_ai.side_effect = fake_ai_response

        resolution = await coordinator.generate_response_locked(
            _response_request(prompt="Hello", user_id="@alice:localhost", thread_id="$thread-root"),
            resolved_target=MessageTarget.resolve("!test:localhost", "$thread-root", "$user_msg"),
        )

    assert resolution == "$thinking"
    persisted_session = cast("AgentSession", storage.session)
    assert persisted_session is not None
    assert persisted_session.runs is not None
    persisted_run = cast("RunOutput", persisted_session.runs[0])
    assert persisted_run.run_id == "run-retry"
    assert persisted_run.metadata is not None
    assert persisted_run.metadata["matrix_response_event_id"] == "$thinking"
    assert persisted_run.messages is not None
    assert persisted_run.messages[0].role == "user"
    assert "Hello" in cast("str", persisted_run.messages[0].content)
    assert [(message.role, message.content) for message in persisted_run.messages[-1:]] == [
        ("assistant", "(turn interrupted by the user before completion)"),
    ]


@pytest.mark.asyncio
async def test_private_agent_response_runner_builds_execution_identity_from_requester(
    tmp_path: Path,
) -> None:
    """Private agent execution identity should use the request owner, not the transport sender."""
    runtime_paths = _runtime_paths(tmp_path)
    config = bind_runtime_paths(
        Config(
            agents={
                "general": AgentConfig(
                    display_name="General",
                    private=AgentPrivateConfig(per="user", root="general_data"),
                ),
            },
            models={"default": ModelConfig(provider="openai", id="test-model")},
        ),
        runtime_paths,
    )
    bot = _make_bot(tmp_path, config=config, runtime_paths=runtime_paths)
    target = MessageTarget.resolve("!test:localhost", None, "$external-trigger", room_mode=True)

    with (
        patch("mindroom.response_runner.ai_response", new=AsyncMock(return_value="done")) as mock_ai_response,
        patch("mindroom.response_runner.should_use_streaming", new=AsyncMock(return_value=False)),
        patch("mindroom.response_lifecycle.apply_post_response_effects", new=AsyncMock(return_value=None)),
    ):
        coordinator = _build_response_runner(
            bot,
            config=config,
            runtime_paths=runtime_paths,
            storage_path=tmp_path,
            requester_id="@owner:localhost",
            message_target=target,
        )
        build_calls: list[dict[str, object]] = []
        original_build_execution_identity = coordinator.deps.tool_runtime.build_execution_identity

        def spy_build_execution_identity(
            *,
            target: MessageTarget,
            user_id: str | None,
            session_id: str,
            agent_name: str | None = None,
        ) -> object:
            build_calls.append(
                {
                    "target": target,
                    "user_id": user_id,
                    "session_id": session_id,
                    "agent_name": agent_name,
                },
            )
            return original_build_execution_identity(
                target=target,
                user_id=user_id,
                session_id=session_id,
                agent_name=agent_name,
            )

        with patch.object(
            coordinator.deps.tool_runtime,
            "build_execution_identity",
            side_effect=spy_build_execution_identity,
        ):
            response_event_id = await coordinator.generate_response_locked(
                _response_request(prompt="Campground opened", user_id="@owner:localhost"),
                resolved_target=target,
            )

    assert response_event_id == "$thinking"
    assert build_calls[0]["user_id"] == "@owner:localhost"
    execution_identity = mock_ai_response.await_args.kwargs["execution_identity"]
    assert execution_identity.agent_name == "general"
    assert execution_identity.requester_id == "@owner:localhost"
    assert execution_identity.room_id == "!test:localhost"
    worker_key = resolve_worker_key("user_agent", execution_identity, agent_name="general")
    assert worker_key == "v1:default:user_agent:@owner:localhost:general"
    assert worker_key != resolve_worker_key(
        "user_agent",
        replace(execution_identity, requester_id=bot.matrix_id.full_id),
        agent_name="general",
    )
    private_workspace = (
        private_instance_scope_root_path(runtime_paths.storage_root, worker_key) / "general" / "general_data"
    )
    assert private_workspace.name == "general_data"
    assert private_workspace.parent.name == "general"
    assert private_workspace.parent.parent.parent.name == "private_instances"


@pytest.mark.asyncio
async def test_generate_response_locked_hard_cancel_does_not_seed_seen_ids_with_active_response_events(
    tmp_path: Path,
) -> None:
    """Lifecycle-owned minimal interruption must not treat active bot replies as consumed user events."""
    runtime_paths = _runtime_paths(tmp_path)
    config = bind_runtime_paths(_config(), runtime_paths)
    bot = _make_bot(tmp_path, config=config, runtime_paths=runtime_paths)
    storage = _SessionStorage()
    started = asyncio.Event()

    async def fake_run_cancellable_response(**kwargs: object) -> str:
        response_function = cast("Callable[[str | None], Awaitable[object]]", kwargs["response_function"])
        task = asyncio.create_task(response_function("$thinking"))
        await started.wait()
        task.cancel()
        with suppress(asyncio.CancelledError):
            await task
        return "$thinking"

    with (
        patch.object(
            ResponseRunner,
            "run_cancellable_response",
            new=AsyncMock(side_effect=fake_run_cancellable_response),
        ),
        patch.object(ResponseRunner, "_active_response_event_ids", return_value={"$other-response"}),
        patch("mindroom.response_runner.should_use_streaming", new=AsyncMock(return_value=False)),
        patch("mindroom.response_lifecycle.apply_post_response_effects", new=AsyncMock(return_value=None)),
        patch("mindroom.response_runner.ai_response", new_callable=AsyncMock) as mock_ai,
    ):
        coordinator = _build_response_runner(
            bot,
            config=config,
            runtime_paths=runtime_paths,
            storage_path=tmp_path,
            requester_id="@alice:localhost",
            history_storage=storage,
            message_target=MessageTarget.resolve("!test:localhost", "$thread-root", "$user_msg"),
        )
        _set_gateway_method(coordinator.deps.delivery_gateway, "edit_text", AsyncMock())

        async def fake_ai_response(*_args: object, **kwargs: object) -> str:
            run_id_callback = cast("Callable[[str], None]", kwargs["run_id_callback"])
            run_id_callback("run-retry")
            started.set()
            await asyncio.sleep(60)
            return "unreachable"

        mock_ai.side_effect = fake_ai_response

        resolution = await coordinator.generate_response_locked(
            _response_request(prompt="Hello", user_id="@alice:localhost", thread_id="$thread-root"),
            resolved_target=MessageTarget.resolve("!test:localhost", "$thread-root", "$user_msg"),
        )

    assert resolution == "$thinking"
    persisted_session = cast("AgentSession", storage.session)
    assert persisted_session is not None
    assert persisted_session.runs is not None
    persisted_run = cast("RunOutput", persisted_session.runs[0])
    assert persisted_run.metadata is not None
    assert persisted_run.metadata["matrix_seen_event_ids"] == ["$user_msg"]
    assert "$other-response" not in persisted_run.metadata["matrix_seen_event_ids"]


@pytest.mark.asyncio
async def test_generate_response_locked_finalizes_cancelled_task_before_delivery(
    tmp_path: Path,
) -> None:
    """Task cancellation before delivery should still emit the canonical cancelled lifecycle."""
    runtime_paths = _runtime_paths(tmp_path)
    config = bind_runtime_paths(_config(), runtime_paths)
    bot = _make_bot(tmp_path, config=config, runtime_paths=runtime_paths)
    cancelled_seen: list[str | None] = []

    @hook(EVENT_MESSAGE_CANCELLED)
    async def on_cancelled(ctx: CancelledResponseContext) -> None:
        cancelled_seen.append(ctx.info.failure_reason)

    registry = HookRegistry.from_plugins([_plugin("cancelled-hooks", [on_cancelled])])

    async def fake_run_cancellable_response(**kwargs: object) -> str | None:
        on_task_cancelled = cast("Callable[[str], None]", kwargs["on_cancelled"])
        on_task_cancelled("sync_restart_cancelled")
        return None

    with (
        patch.object(
            ResponseRunner,
            "run_cancellable_response",
            new=AsyncMock(side_effect=fake_run_cancellable_response),
        ),
        patch("mindroom.response_runner.should_use_streaming", new=AsyncMock(return_value=False)),
        patch("mindroom.response_lifecycle.apply_post_response_effects", new=AsyncMock(return_value=None)),
    ):
        coordinator = _build_response_runner(
            bot,
            config=config,
            runtime_paths=runtime_paths,
            storage_path=tmp_path,
            requester_id="@alice:localhost",
            hook_registry=registry,
            message_target=MessageTarget.resolve("!test:localhost", "$thread-root", "$user_msg"),
        )

        resolution = await coordinator.generate_response_locked(
            _response_request(prompt="Hello", user_id="@alice:localhost", thread_id="$thread-root"),
            resolved_target=MessageTarget.resolve("!test:localhost", "$thread-root", "$user_msg"),
        )

    assert resolution is None
    assert cancelled_seen == ["sync_restart_cancelled"]


@pytest.mark.asyncio
async def test_early_cancellation_redacts_thinking_placeholder(
    tmp_path: Path,
) -> None:
    """Cancellation after Thinking... but before delivery starts should redact the placeholder."""
    runtime_paths = _runtime_paths(tmp_path)
    config = bind_runtime_paths(_config(), runtime_paths)
    bot = _make_bot(tmp_path, config=config, runtime_paths=runtime_paths)
    cancelled_seen: list[str | None] = []

    @hook(EVENT_MESSAGE_CANCELLED)
    async def on_cancelled(ctx: CancelledResponseContext) -> None:
        cancelled_seen.append(ctx.info.failure_reason)

    registry = HookRegistry.from_plugins([_plugin("early-cancel-cleanup", [on_cancelled])])

    async def fake_run_cancellable_response(**kwargs: object) -> str:
        on_task_cancelled = cast("Callable[[str], None]", kwargs["on_cancelled"])
        on_task_cancelled("cancelled_by_user")
        return "$thinking"

    async def redact_message_event(*, room_id: str, event_id: str, reason: str) -> bool:
        assert room_id == "!test:localhost"
        assert event_id == "$thinking"
        assert reason == "Completed placeholder-only streamed response"
        assert cancelled_seen == []
        return True

    with (
        patch.object(
            ResponseRunner,
            "run_cancellable_response",
            new=AsyncMock(side_effect=fake_run_cancellable_response),
        ),
        patch("mindroom.response_runner.should_use_streaming", new=AsyncMock(return_value=False)),
        patch("mindroom.response_lifecycle.apply_post_response_effects", new=AsyncMock(return_value=None)),
    ):
        coordinator = _build_response_runner(
            bot,
            config=config,
            runtime_paths=runtime_paths,
            storage_path=tmp_path,
            requester_id="@alice:localhost",
            hook_registry=registry,
            message_target=MessageTarget.resolve("!test:localhost", "$thread-root", "$user_msg"),
        )
        redact_mock = AsyncMock(side_effect=redact_message_event)
        object.__setattr__(coordinator.deps.delivery_gateway.deps, "redact_message_event", redact_mock)

        resolution = await coordinator.generate_response_locked(
            _response_request(prompt="Hello", user_id="@alice:localhost", thread_id="$thread-root"),
            resolved_target=MessageTarget.resolve("!test:localhost", "$thread-root", "$user_msg"),
        )

    assert resolution is None
    redact_mock.assert_awaited_once()
    assert cancelled_seen == ["cancelled_by_user"]


@pytest.mark.asyncio
async def test_generate_response_locked_returns_none_when_final_delivery_is_unhandled(
    tmp_path: Path,
) -> None:
    """A terminal unhandled delivery outcome should not mark the turn handled."""
    runtime_paths = _runtime_paths(tmp_path)
    config = bind_runtime_paths(_config(), runtime_paths)
    bot = _make_bot(tmp_path, config=config, runtime_paths=runtime_paths)
    storage = _SessionStorage()

    with (
        patch("mindroom.response_runner.should_use_streaming", new=AsyncMock(return_value=False)),
        patch("mindroom.response_lifecycle.apply_post_response_effects", new=AsyncMock(return_value=None)),
    ):
        coordinator = _build_response_runner(
            bot,
            config=config,
            runtime_paths=runtime_paths,
            storage_path=tmp_path,
            requester_id="@alice:localhost",
            history_storage=storage,
            message_target=MessageTarget.resolve("!test:localhost", "$thread-root", "$user_msg"),
        )

        async def fake_generate_non_streaming(
            *_args: object,
            **kwargs: object,
        ) -> _NonStreamingGeneration:
            turn_recorder = cast("TurnRecorder", kwargs["turn_recorder"])
            turn_recorder.set_run_id("run-delivery-cancel")
            turn_recorder.record_completed(
                run_metadata={},
                assistant_text="Hello!",
                completed_tools=[],
            )
            return _NonStreamingGeneration(
                response_text="Hello!",
                tool_trace=[],
                run_metadata_content={},
            )

        _set_gateway_method(
            coordinator.deps.delivery_gateway,
            "deliver_final",
            AsyncMock(
                return_value=FinalDeliveryOutcome(
                    terminal_status="cancelled",
                    event_id=None,
                    failure_reason="delivery_cancelled",
                ),
            ),
        )

        with patch.object(
            ResponseRunner,
            "generate_non_streaming_ai_response",
            new=AsyncMock(side_effect=fake_generate_non_streaming),
        ):
            resolution = await coordinator.generate_response_locked(
                _response_request(prompt="Hello", user_id="@alice:localhost", thread_id="$thread-root"),
                resolved_target=MessageTarget.resolve("!test:localhost", "$thread-root", "$user_msg"),
            )

    assert resolution is None
    assert storage.session is None


@pytest.mark.asyncio
async def test_generate_response_locked_unhandled_delivery_outcome_does_not_persist_tool_replay(
    tmp_path: Path,
) -> None:
    """An unhandled delivery outcome should not synthesize interrupted replay from visible tools."""
    runtime_paths = _runtime_paths(tmp_path)
    config = bind_runtime_paths(_config(), runtime_paths)
    bot = _make_bot(tmp_path, config=config, runtime_paths=runtime_paths)
    history_storage = _SessionStorage()
    ai_scope_storage = _SessionStorage()
    completed_run = RunOutput(
        run_id="run-visible-tools",
        agent_id="general",
        session_id="session1",
        content="Half done",
        messages=[Message(role="assistant", content="Half done")],
        tools=[
            ToolExecution(
                tool_name="run_shell_command",
                tool_args={"cmd": "pwd"},
                result="/app",
            ),
        ],
        status=RunStatus.completed,
    )

    with (
        patch("mindroom.response_runner.should_use_streaming", new=AsyncMock(return_value=False)),
        patch("mindroom.response_lifecycle.apply_post_response_effects", new=AsyncMock(return_value=None)),
        patch.object(ResponseRunner, "_show_tool_calls", return_value=True),
        patch(
            "mindroom.ai.open_resolved_scope_session_context",
            new=lambda **_: _open_agent_scope_context(ai_scope_storage),
        ),
        patch("mindroom.ai._prepare_agent_and_prompt", new_callable=AsyncMock) as mock_prepare,
        patch("mindroom.ai_runtime.cached_agent_run", new_callable=AsyncMock, return_value=completed_run),
    ):
        mock_prepare.return_value = _prepared_prompt_result(MagicMock(), prompt="Hello")
        coordinator = _build_response_runner(
            bot,
            config=config,
            runtime_paths=runtime_paths,
            storage_path=tmp_path,
            requester_id="@alice:localhost",
            history_storage=history_storage,
            message_target=MessageTarget.resolve("!test:localhost", "$thread-root", "$user_msg"),
        )
        _set_gateway_method(
            coordinator.deps.delivery_gateway,
            "deliver_final",
            AsyncMock(
                return_value=FinalDeliveryOutcome(
                    terminal_status="cancelled",
                    event_id=None,
                    failure_reason="delivery_cancelled",
                ),
            ),
        )

        resolution = await coordinator.generate_response_locked(
            _response_request(prompt="Hello", user_id="@alice:localhost", thread_id="$thread-root"),
            resolved_target=MessageTarget.resolve("!test:localhost", "$thread-root", "$user_msg"),
        )

    assert resolution is None
    assert history_storage.session is None


@pytest.mark.asyncio
async def test_generate_response_locked_preserves_visible_stream_when_finalize_returns_cancelled(
    tmp_path: Path,
) -> None:
    """Delivery-stage cancellation after streaming completes should still persist replay."""
    runtime_paths = _runtime_paths(tmp_path)
    config = bind_runtime_paths(_config(), runtime_paths)
    bot = _make_bot(tmp_path, config=config, runtime_paths=runtime_paths)
    storage = _SessionStorage()
    request = replace(
        _response_request(prompt="Hello", user_id="@alice:localhost", thread_id="$thread-root"),
        response_envelope=MessageEnvelope(
            source_event_id="$user_msg",
            room_id="!test:localhost",
            target=MessageTarget.resolve("!test:localhost", "$thread-root", "$user_msg"),
            requester_id="@alice:localhost",
            sender_id="@alice:localhost",
            body="Hello",
            attachment_ids=(),
            mentioned_agents=(),
            agent_name="general",
            source_kind=MESSAGE_SOURCE_KIND,
            origin=message_origin(
                sender_id="@alice:localhost",
                requester_id="@alice:localhost",
                source_kind=MESSAGE_SOURCE_KIND,
            ),
        ),
        correlation_id="corr-stream-cancel",
    )

    with (
        patch("mindroom.response_runner.should_use_streaming", new=AsyncMock(return_value=True)),
        patch("mindroom.response_lifecycle.apply_post_response_effects", new=AsyncMock(return_value=None)),
    ):
        coordinator = _build_response_runner(
            bot,
            config=config,
            runtime_paths=runtime_paths,
            storage_path=tmp_path,
            requester_id="@alice:localhost",
            history_storage=storage,
            message_target=MessageTarget.resolve("!test:localhost", "$thread-root", "$user_msg"),
        )

        async def fake_generate_streaming(
            *_args: object,
            **kwargs: object,
        ) -> StreamTransportOutcome:
            turn_recorder = cast("TurnRecorder", kwargs["turn_recorder"])
            turn_recorder.set_run_id("run-stream-delivery-cancel")
            turn_recorder.record_completed(
                run_metadata={},
                assistant_text="Hello!",
                completed_tools=[],
            )
            return StreamTransportOutcome(
                last_physical_stream_event_id="$stream-msg",
                terminal_status="completed",
                rendered_body="Hello!",
                visible_body_state="visible_body",
            )

        _set_gateway_method(
            coordinator.deps.delivery_gateway,
            "finalize_streamed_response",
            AsyncMock(
                return_value=FinalDeliveryOutcome(
                    terminal_status="cancelled",
                    event_id="$stream-msg",
                    is_visible_response=True,
                    final_visible_body="Hello!",
                    delivery_kind="sent",
                    failure_reason="delivery_cancelled",
                ),
            ),
        )

        with patch.object(
            ResponseRunner,
            "generate_streaming_ai_response",
            new=AsyncMock(side_effect=fake_generate_streaming),
        ):
            resolution = await coordinator.generate_response_locked(
                request,
                resolved_target=MessageTarget.resolve("!test:localhost", "$thread-root", "$user_msg"),
            )

    assert resolution == "$stream-msg"
    assert storage.session is None


@pytest.mark.asyncio
async def test_generate_response_locked_preserves_visible_stream_on_late_finalize_error(
    tmp_path: Path,
) -> None:
    """Late streamed finalization errors should preserve the visible stream as an error outcome."""
    runtime_paths = _runtime_paths(tmp_path)
    config = bind_runtime_paths(_config(), runtime_paths)
    bot = _make_bot(tmp_path, config=config, runtime_paths=runtime_paths)
    storage = _SessionStorage()
    request = replace(
        _response_request(prompt="Hello", user_id="@alice:localhost", thread_id="$thread-root"),
        response_envelope=MessageEnvelope(
            source_event_id="$user_msg",
            room_id="!test:localhost",
            target=MessageTarget.resolve("!test:localhost", "$thread-root", "$user_msg"),
            requester_id="@alice:localhost",
            sender_id="@alice:localhost",
            body="Hello",
            attachment_ids=(),
            mentioned_agents=(),
            agent_name="general",
            source_kind=MESSAGE_SOURCE_KIND,
            origin=message_origin(
                sender_id="@alice:localhost",
                requester_id="@alice:localhost",
                source_kind=MESSAGE_SOURCE_KIND,
            ),
        ),
        correlation_id="corr-stream-error",
    )

    with (
        patch("mindroom.response_runner.should_use_streaming", new=AsyncMock(return_value=True)),
        patch("mindroom.response_lifecycle.apply_post_response_effects", new=AsyncMock(return_value=None)),
    ):
        coordinator = _build_response_runner(
            bot,
            config=config,
            runtime_paths=runtime_paths,
            storage_path=tmp_path,
            requester_id="@alice:localhost",
            history_storage=storage,
            message_target=MessageTarget.resolve("!test:localhost", "$thread-root", "$user_msg"),
        )

        async def fake_generate_streaming(
            *_args: object,
            **kwargs: object,
        ) -> StreamTransportOutcome:
            turn_recorder = cast("TurnRecorder", kwargs["turn_recorder"])
            turn_recorder.set_run_id("run-stream-delivery-error")
            turn_recorder.record_completed(
                run_metadata={},
                assistant_text="Hello!",
                completed_tools=[],
            )
            return StreamTransportOutcome(
                last_physical_stream_event_id="$stream-msg",
                terminal_status="completed",
                rendered_body="Hello!",
                visible_body_state="visible_body",
            )

        _set_gateway_method(
            coordinator.deps.delivery_gateway,
            "finalize_streamed_response",
            AsyncMock(
                return_value=FinalDeliveryOutcome(
                    terminal_status="error",
                    event_id="$stream-msg",
                    is_visible_response=True,
                    final_visible_body="Hello!",
                    delivery_kind="sent",
                    failure_reason="delivery crash",
                ),
            ),
        )

        with patch.object(
            ResponseRunner,
            "generate_streaming_ai_response",
            new=AsyncMock(side_effect=fake_generate_streaming),
        ):
            resolution = await coordinator.generate_response_locked(
                request,
                resolved_target=MessageTarget.resolve("!test:localhost", "$thread-root", "$user_msg"),
            )

    assert resolution == "$stream-msg"


@pytest.mark.asyncio
async def test_process_and_respond_uses_resolved_thread_id_for_ai_logging_context(
    tmp_path: Path,
) -> None:
    """Non-streaming AI calls should receive the resolved thread root, not the raw request thread id."""
    runtime_paths = _runtime_paths(tmp_path)
    config = bind_runtime_paths(_config(), runtime_paths)
    bot = _make_bot(tmp_path, config=config, runtime_paths=runtime_paths)

    with (
        patch("mindroom.response_runner.should_use_streaming", new=AsyncMock(return_value=False)),
        patch("mindroom.response_runner.ai_response", new_callable=AsyncMock) as mock_ai,
    ):
        coordinator = _build_response_runner(
            bot,
            config=config,
            runtime_paths=runtime_paths,
            storage_path=tmp_path,
            requester_id="@alice:localhost",
        )

        async def fake_ai_response(*args: object, **_kwargs: object) -> str:
            assert args[0].thread_id == "$resolved-thread"
            return "Hello!"

        mock_ai.side_effect = fake_ai_response

        target = MessageTarget.resolve("!test:localhost", "$resolved-thread", "$user_msg")
        base_request = _response_request(prompt="Hello", user_id="@alice:localhost", thread_id="$raw-thread")
        request = replace(
            base_request,
            response_envelope=replace(base_request.response_envelope, target=target),
        )
        await coordinator.process_and_respond(request)


@pytest.mark.asyncio
async def test_process_and_respond_streaming_uses_resolved_thread_id_for_ai_logging_context(
    tmp_path: Path,
) -> None:
    """Streaming AI calls should receive the resolved thread root, not the raw request thread id."""
    runtime_paths = _runtime_paths(tmp_path)
    config = bind_runtime_paths(_config(), runtime_paths)
    bot = _make_bot(tmp_path, config=config, runtime_paths=runtime_paths)

    with patch("mindroom.response_runner.stream_agent_response") as mock_stream:
        coordinator = _build_response_runner(
            bot,
            config=config,
            runtime_paths=runtime_paths,
            storage_path=tmp_path,
            requester_id="@alice:localhost",
        )

        def fake_stream_agent_response(*args: object, **_kwargs: object) -> AsyncIterator[str]:
            assert args[0].thread_id == "$resolved-thread"

            async def fake_stream() -> AsyncIterator[str]:
                yield "Hello!"

            return fake_stream()

        mock_stream.side_effect = fake_stream_agent_response

        target = MessageTarget.resolve("!test:localhost", "$resolved-thread", "$user_msg")
        base_request = _response_request(prompt="Hello", user_id="@alice:localhost", thread_id="$raw-thread")
        request = replace(
            base_request,
            response_envelope=replace(base_request.response_envelope, target=target),
        )
        await coordinator.process_and_respond_streaming(request)


@pytest.mark.asyncio
async def test_process_and_respond_passes_current_and_model_prompt_to_ai(
    tmp_path: Path,
) -> None:
    """Non-streaming AI calls should preserve raw and expanded prompt layers separately."""
    runtime_paths = _runtime_paths(tmp_path)
    config = bind_runtime_paths(_config(), runtime_paths)
    bot = _make_bot(tmp_path, config=config, runtime_paths=runtime_paths)

    with (
        patch("mindroom.response_runner.should_use_streaming", new=AsyncMock(return_value=False)),
        patch("mindroom.response_runner.ai_response", new_callable=AsyncMock) as mock_ai,
    ):
        coordinator = _build_response_runner(
            bot,
            config=config,
            runtime_paths=runtime_paths,
            storage_path=tmp_path,
            requester_id="@alice:localhost",
        )

        async def fake_ai_response(*_args: object, **kwargs: object) -> str:
            assert kwargs["prompt"] == "Hello"
            assert kwargs["model_prompt"] == "Hello with context"
            return "Hello!"

        mock_ai.side_effect = fake_ai_response

        await coordinator.process_and_respond(
            _response_request(
                prompt="Hello",
                model_prompt="Hello with context",
                user_id="@alice:localhost",
                thread_id="$thread-root",
            ),
        )


@pytest.mark.asyncio
async def test_process_and_respond_streaming_passes_current_and_model_prompt_to_ai(
    tmp_path: Path,
) -> None:
    """Streaming AI calls should preserve raw and expanded prompt layers separately."""
    runtime_paths = _runtime_paths(tmp_path)
    config = bind_runtime_paths(_config(), runtime_paths)
    bot = _make_bot(tmp_path, config=config, runtime_paths=runtime_paths)

    with patch("mindroom.response_runner.stream_agent_response") as mock_stream:
        coordinator = _build_response_runner(
            bot,
            config=config,
            runtime_paths=runtime_paths,
            storage_path=tmp_path,
            requester_id="@alice:localhost",
        )

        def fake_stream_agent_response(*_args: object, **kwargs: object) -> AsyncIterator[str]:
            assert kwargs["prompt"] == "Hello"
            assert kwargs["model_prompt"] == "Hello with context"

            async def fake_stream() -> AsyncIterator[str]:
                yield "Hello!"

            return fake_stream()

        mock_stream.side_effect = fake_stream_agent_response

        await coordinator.process_and_respond_streaming(
            _response_request(
                prompt="Hello",
                model_prompt="Hello with context",
                user_id="@alice:localhost",
                thread_id="$thread-root",
            ),
        )


@pytest.mark.asyncio
async def test_generate_response_locked_sets_failure_reason_for_plain_streaming_exception(
    tmp_path: Path,
) -> None:
    """Plain streaming exceptions should propagate their text to the typed error outcome."""
    runtime_paths = _runtime_paths(tmp_path)
    config = bind_runtime_paths(_config(), runtime_paths)
    bot = _make_bot(tmp_path, config=config, runtime_paths=runtime_paths)

    with (
        patch("mindroom.response_runner.should_use_streaming", new=AsyncMock(return_value=True)),
        patch("mindroom.response_lifecycle.apply_post_response_effects", new=AsyncMock(return_value=None)),
    ):
        coordinator = _build_response_runner(
            bot,
            config=config,
            runtime_paths=runtime_paths,
            storage_path=tmp_path,
            requester_id="@bob:localhost",
            hook_registry=HookRegistry.empty(),
            message_target=MessageTarget.resolve("!test:localhost", "$thread-root", "$user_msg"),
        )
        coordinator.generate_streaming_ai_response = AsyncMock(side_effect=RuntimeError("plain boom"))

        resolution = await coordinator.generate_response_locked(
            _response_request(prompt="Hello", user_id="@bob:localhost", thread_id="$thread-root"),
            resolved_target=MessageTarget.resolve("!test:localhost", "$thread-root", "$user_msg"),
        )

    assert resolution is None
    coordinator.deps.delivery_gateway.deps.response_hooks.emit_cancelled_response.assert_awaited_once()
    assert (
        coordinator.deps.delivery_gateway.deps.response_hooks.emit_cancelled_response.await_args.kwargs[
            "visible_response_event_id"
        ]
        is None
    )
    assert (
        coordinator.deps.delivery_gateway.deps.response_hooks.emit_cancelled_response.await_args.kwargs[
            "failure_reason"
        ]
        == "plain boom"
    )


@pytest.mark.asyncio
async def test_generate_response_preserves_model_prompt_in_persisted_session(
    tmp_path: Path,
) -> None:
    """Persisted model prompts should stay intact so later turns can reuse provider cache prefixes."""
    runtime_paths = _runtime_paths(tmp_path)
    config = bind_runtime_paths(_config(), runtime_paths)
    config.agents["general"].show_tool_calls = False
    bot = _make_bot(tmp_path, config=config, runtime_paths=runtime_paths)
    storage = _SessionStorage()

    async def fake_prepare_agent_and_prompt(
        _ctx: object,
        *_args: object,
        prompt: str,
        model_prompt: str | None = None,
        **_kwargs: object,
    ) -> _PreparedAgentRun:
        model_facing_prompt = model_prompt if model_prompt is not None else prompt
        return _PreparedAgentRun(
            agent=MagicMock(),
            messages=(
                Message(role="user", content="Earlier context"),
                Message(role="assistant", content="Earlier answer"),
                Message(role="user", content=model_facing_prompt),
            ),
            unseen_event_ids=[],
            prepared_history=PreparedHistoryState(),
            runtime_model_name="default",
        )

    async def fake_cached_agent_run(
        _agent: object,
        run_input: tuple[Message, ...],
        session_id: str,
        **kwargs: object,
    ) -> RunOutput:
        run = RunOutput(
            run_id=cast("str | None", kwargs.get("run_id")),
            content="Hello",
            status=RunStatus.completed,
            messages=[*run_input, Message(role="assistant", content="Hello")],
        )
        storage.session = AgentSession(
            session_id=session_id,
            agent_id="general",
            created_at=1,
            updated_at=1,
            runs=[run],
        )
        return run

    with (
        patch("mindroom.response_runner.should_use_streaming", new=AsyncMock(return_value=False)),
        patch("mindroom.ai._prepare_agent_and_prompt", new=AsyncMock(side_effect=fake_prepare_agent_and_prompt)),
        patch("mindroom.ai_runtime.cached_agent_run", new=AsyncMock(side_effect=fake_cached_agent_run)),
    ):
        coordinator = _build_response_runner(
            bot,
            config=config,
            runtime_paths=runtime_paths,
            storage_path=tmp_path,
            requester_id="@alice:localhost",
            history_storage=storage,
            message_target=MessageTarget.resolve("!test:localhost", "$thread-root", "$user_msg"),
        )

        coordinator.deps.delivery_gateway.send_text.return_value = "$msg"

        await coordinator.generate_response(
            replace(
                _response_request(prompt="Describe this image", user_id="@alice:localhost", thread_id="$thread-root"),
                model_prompt="Available attachment IDs: att_1. Use tool calls to inspect or process them.",
            ),
        )

    persisted_session = cast("AgentSession", storage.session)
    assert persisted_session.runs is not None
    persisted_run = cast("RunOutput", persisted_session.runs[0])
    assert persisted_run.messages is not None
    assert persisted_run.messages[0].content == "Earlier context"
    assert "Describe this image" in cast("str", persisted_run.messages[2].content)
    assert "Available attachment IDs: att_1" in cast("str", persisted_run.messages[2].content)


@pytest.mark.asyncio
async def test_generate_response_appends_matrix_tool_prompt_context(tmp_path: Path) -> None:
    """Matrix tool metadata appended during runtime preparation should reach the model."""
    runtime_paths = _runtime_paths(tmp_path)
    config = bind_runtime_paths(_config_with_matrix_message(), runtime_paths)
    bot = _make_bot(tmp_path, config=config, runtime_paths=runtime_paths)
    model_prompts: list[str] = []

    async def fake_ai_response(*_args: object, **kwargs: object) -> str:
        model_prompts.append(cast("str", kwargs["model_prompt"]))
        return "Hello"

    with (
        patch("mindroom.response_runner.should_use_streaming", new=AsyncMock(return_value=False)),
        patch("mindroom.response_runner.ai_response", new=AsyncMock(side_effect=fake_ai_response)),
        patch("mindroom.response_lifecycle.apply_post_response_effects", new=AsyncMock(return_value=None)),
    ):
        coordinator = _build_response_runner(
            bot,
            config=config,
            runtime_paths=runtime_paths,
            storage_path=tmp_path,
            requester_id="@alice:localhost",
            message_target=MessageTarget.resolve("!test:localhost", "$thread-root", "$user_msg"),
        )

        await coordinator.generate_response(
            _response_request(prompt="Hello", user_id="@alice:localhost", thread_id="$thread-root"),
        )

    assert model_prompts
    assert "[Matrix metadata for tool calls]" in model_prompts[0]


@pytest.mark.asyncio
async def test_generate_response_passes_resolved_correlation_id_to_ai_response(tmp_path: Path) -> None:
    """Edit regeneration can correlate on a different event than the reply anchor."""
    runtime_paths = _runtime_paths(tmp_path)
    config = bind_runtime_paths(_config(), runtime_paths)
    bot = _make_bot(tmp_path, config=config, runtime_paths=runtime_paths)
    seen_ctx: list[object] = []

    async def fake_ai_response(*args: object, **_kwargs: object) -> str:
        seen_ctx.append(args[0])
        return "Hello"

    with (
        patch("mindroom.response_runner.should_use_streaming", new=AsyncMock(return_value=False)),
        patch("mindroom.response_runner.ai_response", new=AsyncMock(side_effect=fake_ai_response)),
        patch("mindroom.response_lifecycle.apply_post_response_effects", new=AsyncMock(return_value=None)),
    ):
        coordinator = _build_response_runner(
            bot,
            config=config,
            runtime_paths=runtime_paths,
            storage_path=tmp_path,
            requester_id="@alice:localhost",
            message_target=MessageTarget.resolve("!test:localhost", "$thread-root", "$original"),
        )

        await coordinator.generate_response(
            _response_request(
                prompt="Regenerate this edit",
                user_id="@alice:localhost",
                thread_id="$thread-root",
                reply_to_event_id="$original",
                correlation_id="$edit",
            ),
        )

    assert seen_ctx
    ctx = seen_ctx[-1]
    assert ctx.reply_to_event_id == "$original"
    assert ctx.correlation_id == "$edit"


@pytest.mark.asyncio
async def test_generate_response_preserves_retry_model_prompt(tmp_path: Path) -> None:
    """Retry runs should keep the model-facing prompt that Agno persisted."""
    runtime_paths = _runtime_paths(tmp_path)
    config = bind_runtime_paths(_config(), runtime_paths)
    config.agents["general"].show_tool_calls = False
    bot = _make_bot(tmp_path, config=config, runtime_paths=runtime_paths)
    storage = _SessionStorage()
    seen_run_ids: list[str | None] = []

    async def fake_prepare_agent_and_prompt(
        _ctx: object,
        *_args: object,
        prompt: str,
        model_prompt: str | None = None,
        **_kwargs: object,
    ) -> _PreparedAgentRun:
        model_facing_prompt = model_prompt if model_prompt is not None else prompt
        return _prepared_prompt_result(MagicMock(), prompt=model_facing_prompt)

    async def fake_cached_agent_run(
        _agent: object,
        run_input: tuple[Message, ...],
        session_id: str,
        **kwargs: object,
    ) -> RunOutput:
        run_id = cast("str | None", kwargs.get("run_id"))
        seen_run_ids.append(run_id)
        if len(seen_run_ids) == 1:
            error_message = "audio input is not supported"
            raise ValueError(error_message)
        run = RunOutput(
            run_id=run_id,
            content="Hello",
            status=RunStatus.completed,
            messages=[*run_input, Message(role="assistant", content="Hello")],
        )
        storage.session = AgentSession(
            session_id=session_id,
            agent_id="general",
            created_at=1,
            updated_at=1,
            runs=[run],
        )
        return run

    with (
        patch("mindroom.response_runner.should_use_streaming", new=AsyncMock(return_value=False)),
        patch("mindroom.ai._prepare_agent_and_prompt", new=AsyncMock(side_effect=fake_prepare_agent_and_prompt)),
        patch("mindroom.ai_runtime.cached_agent_run", new=AsyncMock(side_effect=fake_cached_agent_run)),
    ):
        coordinator = _build_response_runner(
            bot,
            config=config,
            runtime_paths=runtime_paths,
            storage_path=tmp_path,
            requester_id="@alice:localhost",
            history_storage=storage,
            message_target=MessageTarget.resolve("!test:localhost", "$thread-root", "$user_msg"),
        )

        coordinator.deps.delivery_gateway.send_text.return_value = "$msg"

        await coordinator.generate_response(
            replace(
                _response_request(
                    prompt="Describe this image",
                    user_id="@alice:localhost",
                    thread_id="$thread-root",
                    media=MediaInputs(audio=[MagicMock(name="audio_input")]),
                ),
                model_prompt="Available attachment IDs: att_1. Use tool calls to inspect or process them.",
            ),
        )

    persisted_session = cast("AgentSession", storage.session)
    assert persisted_session.runs is not None
    persisted_run = cast("RunOutput", persisted_session.runs[0])
    assert len(seen_run_ids) == 2
    assert seen_run_ids[0] is not None
    assert seen_run_ids[1] is not None
    assert seen_run_ids[1] != seen_run_ids[0]
    assert persisted_run.run_id == seen_run_ids[1]
    assert persisted_run.messages is not None
    assert "Describe this image" in cast("str", persisted_run.messages[0].content)
    assert "Available attachment IDs: att_1" in cast("str", persisted_run.messages[0].content)
