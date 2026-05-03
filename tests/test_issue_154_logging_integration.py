"""Integration coverage for ISSUE-154 logging traceability."""

from __future__ import annotations

import json
import logging
import re
import time
from dataclasses import dataclass
from json import JSONDecodeError
from types import SimpleNamespace
from typing import TYPE_CHECKING
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
import structlog
from agno.models.message import Message
from agno.run.agent import ModelRequestCompletedEvent, RunCompletedEvent, RunContentEvent
from agno.run.base import RunStatus

from mindroom.ai import _PreparedAgentRun, ai_response, stream_agent_response
from mindroom.bot import AgentBot
from mindroom.config.agent import AgentConfig
from mindroom.config.main import Config
from mindroom.config.models import DebugConfig, ModelConfig
from mindroom.constants import tracking_dir
from mindroom.handled_turns import HandledTurnState
from mindroom.history import PreparedHistoryState
from mindroom.hooks import HookRegistry
from mindroom.llm_request_logging import install_llm_request_logging
from mindroom.logging_config import get_logger, setup_logging
from mindroom.matrix.users import AgentMatrixUser
from mindroom.message_target import MessageTarget
from mindroom.tool_system.runtime_context import ToolDispatchContext
from mindroom.tool_system.tool_hooks import build_tool_hook_bridge
from mindroom.tool_system.worker_routing import build_tool_execution_identity
from mindroom.turn_policy import PreparedDispatch, ResponseAction
from tests.conftest import bind_runtime_paths, replace_turn_controller_deps, runtime_paths_for, test_runtime_paths

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Iterator
    from pathlib import Path


@pytest.fixture(autouse=True)
def _reset_logging_after_test() -> Iterator[None]:
    yield
    logging.shutdown()
    logging.getLogger().handlers.clear()
    for logger in logging.root.manager.loggerDict.values():
        if isinstance(logger, logging.Logger):
            logger.handlers.clear()
    structlog.reset_defaults()


@dataclass
class _LoggingModel:
    id: str = "test-model"
    system_prompt: str | None = None
    temperature: float | None = 0.7
    client: object | None = None
    async_client: object | None = None

    async def ainvoke(self, *_args: object, **_kwargs: object) -> dict[str, str]:
        return {"status": "ok"}

    async def ainvoke_stream(
        self,
        *_args: object,
        **_kwargs: object,
    ) -> AsyncIterator[dict[str, str]]:
        yield {"status": "ok"}


class _InvokeAgent:
    def __init__(
        self,
        *,
        model: _LoggingModel,
        bridge: object,
        captured_metadata: list[dict[str, object]],
    ) -> None:
        self.model = model
        self.name = "GeneralAgent"
        self.add_history_to_context = False
        self.db = None
        self.learning = None
        self._bridge = bridge
        self._captured_metadata = captured_metadata

    async def arun(self, prompt: str | list[Message], **kwargs: object) -> SimpleNamespace:
        prompt_messages = prompt if isinstance(prompt, list) else [Message(role="user", content=prompt)]
        prompt_text = "\n\n".join(str(message.content) for message in prompt_messages if message.content)
        self._captured_metadata.append(dict(kwargs["metadata"]))
        await self._bridge(
            "demo_tool",
            _demo_tool,
            {"text": prompt_text, "authorization": "Bearer hidden-token"},
        )
        await self.model.ainvoke(
            messages=prompt_messages,
            assistant_message=Message(role="assistant"),
            tools=[{"name": "demo_tool", "description": "Echo"}],
        )
        return SimpleNamespace(
            content="Done",
            tools=[],
            messages=[],
            run_id="run-invoke",
            session_id=kwargs["session_id"],
            status=RunStatus.completed,
            model=self.model.id,
            model_provider="openai",
        )


class _StreamAgent:
    def __init__(
        self,
        *,
        model: _LoggingModel,
        bridge: object,
        captured_metadata: list[dict[str, object]],
    ) -> None:
        self.model = model
        self.name = "GeneralAgent"
        self.add_history_to_context = False
        self.db = None
        self.learning = None
        self._bridge = bridge
        self._captured_metadata = captured_metadata

    def arun(self, prompt: str | list[Message], **kwargs: object) -> AsyncIterator[object]:
        prompt_messages = prompt if isinstance(prompt, list) else [Message(role="user", content=prompt)]
        prompt_text = "\n\n".join(str(message.content) for message in prompt_messages if message.content)
        self._captured_metadata.append(dict(kwargs["metadata"]))

        async def _stream() -> AsyncIterator[object]:
            await self._bridge(
                "demo_tool",
                _demo_tool,
                {"text": prompt_text, "authorization": "Bearer hidden-token"},
            )
            async for _chunk in self.model.ainvoke_stream(
                messages=prompt_messages,
                assistant_message=Message(role="assistant"),
                tools=[{"name": "demo_tool", "description": "Echo"}],
            ):
                break
            yield ModelRequestCompletedEvent(
                model=self.model.id,
                model_provider="openai",
                input_tokens=4,
                output_tokens=2,
                total_tokens=6,
                time_to_first_token=0.12,
            )
            yield RunContentEvent(content="Done")
            yield RunCompletedEvent(
                run_id="run-stream",
                session_id=kwargs["session_id"],
            )

        return _stream()


async def _demo_tool(*, text: str, authorization: str) -> dict[str, str]:
    return {"echo": text, "authorization": authorization}


def _config(runtime_paths: object) -> Config:
    return bind_runtime_paths(
        Config(
            agents={"general": AgentConfig(display_name="General")},
            models={"default": ModelConfig(provider="openai", id="test-model")},
            debug=DebugConfig(log_llm_requests=True),
        ),
        runtime_paths,
    )


def _read_jsonl(path: Path) -> list[dict[str, object]]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line]


def _read_llm_entries(log_dir: Path) -> list[dict[str, object]]:
    log_files = list(log_dir.glob("llm-requests-*.jsonl"))
    assert len(log_files) == 1
    return _read_jsonl(log_files[0])


def _json_log_payloads(stderr_text: str) -> list[dict[str, object]]:
    payloads: list[dict[str, object]] = []
    for line in stderr_text.splitlines():
        try:
            payload = json.loads(line)
        except JSONDecodeError:
            continue
        if isinstance(payload, dict):
            payloads.append(payload)
    return payloads


def _tool_calls_path(runtime_paths: object) -> Path:
    return tracking_dir(runtime_paths) / "tool_calls.jsonl"


def _prepared_prompt_result(agent: object, *, prompt: str = "expanded prompt") -> _PreparedAgentRun:
    return _PreparedAgentRun(
        agent=agent,
        messages=(Message(role="user", content=prompt),),
        unseen_event_ids=[],
        prepared_history=PreparedHistoryState(),
    )


@pytest.mark.asyncio
async def test_cross_sink_correlation_invariant_for_matrix_turn_processing_log(  # noqa: PLR0915
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """One Matrix turn should share a single correlation id across request, tool, turn, and dispatch logs."""
    runtime_paths = test_runtime_paths(tmp_path)
    config = _config(runtime_paths)
    llm_log_dir = tmp_path / "llm"
    monkeypatch.setenv("MINDROOM_LOG_FORMAT", "json")
    setup_logging(level="INFO", runtime_paths=runtime_paths)
    capsys.readouterr()

    agent_user = AgentMatrixUser(
        agent_name="general",
        user_id="@mindroom_general:localhost",
        display_name="GeneralAgent",
        password="test_password",  # noqa: S106
    )
    bot = AgentBot(
        agent_user=agent_user,
        storage_path=tmp_path,
        config=config,
        runtime_paths=runtime_paths_for(config),
        rooms=["!test:localhost"],
    )
    bot.client = AsyncMock()
    capsys.readouterr()

    target = MessageTarget.resolve(
        room_id="!test:localhost",
        thread_id="$thread-root:localhost",
        reply_to_event_id="$event:localhost",
    )
    dispatch_context = ToolDispatchContext.from_target(
        agent_name="general",
        runtime_paths=runtime_paths,
        requester_user_id="@user:localhost",
        target=target,
    )
    model = _LoggingModel()
    install_llm_request_logging(
        model,
        agent_name="general",
        debug_config=DebugConfig(log_llm_requests=True, llm_request_log_dir=str(llm_log_dir)),
        default_log_dir=tmp_path / "unused",
    )
    captured_metadata: list[dict[str, object]] = []
    bridge = build_tool_hook_bridge(
        HookRegistry.empty(),
        agent_name="general",
        dispatch_context=dispatch_context,
        config=config,
        runtime_paths=runtime_paths,
    )
    agent = _InvokeAgent(model=model, bridge=bridge, captured_metadata=captured_metadata)

    async def generate_response(request: PreparedDispatch) -> str | None:
        with (
            patch(
                "mindroom.ai._prepare_agent_and_prompt",
                new=AsyncMock(return_value=_prepared_prompt_result(agent)),
            ),
            patch(
                "mindroom.ai.agent_tool_definition_payloads_for_logging",
                return_value=[{"name": "demo_tool", "description": "Echo"}],
            ),
        ):
            response = await ai_response(
                agent_name="general",
                prompt=request.prompt,
                model_prompt="model prompt",
                session_id=target.session_id or "session-1",
                runtime_paths=runtime_paths,
                config=config,
                room_id=request.room_id,
                thread_id=request.thread_id,
                reply_to_event_id=request.reply_to_event_id,
                user_id=request.user_id,
                execution_identity=dispatch_context.execution_identity,
                matrix_run_metadata=request.matrix_run_metadata,
            )
        assert response == "Done"
        return "$response:localhost"

    response_runner = SimpleNamespace(
        generate_response=AsyncMock(side_effect=generate_response),
        generate_team_response_helper=AsyncMock(),
    )
    controller = replace_turn_controller_deps(
        bot,
        logger=get_logger("tests.issue_154.turn"),
        response_runner=response_runner,
    )

    room = MagicMock()
    room.room_id = "!test:localhost"
    event = SimpleNamespace(
        event_id="$event:localhost",
        body="hello",
        source={},
    )
    dispatch = PreparedDispatch(
        requester_user_id="@user:localhost",
        context=SimpleNamespace(
            am_i_mentioned=False,
            thread_id=target.resolved_thread_id,
            thread_history=(),
            requires_full_thread_history=False,
        ),
        target=target,
        correlation_id="$event:localhost",
        envelope=MagicMock(),
    )

    await controller._execute_response_action(
        room,
        event,
        dispatch,
        ResponseAction(kind="individual"),
        AsyncMock(),
        processing_log="Processing",
        dispatch_started_at=time.monotonic(),
        handled_turn=HandledTurnState.from_source_event_id(
            event.event_id,
            requester_id="@user:localhost",
            correlation_id="$event:localhost",
        ),
    )

    llm_entry = _read_llm_entries(llm_log_dir)[0]
    tool_entry = _read_jsonl(_tool_calls_path(runtime_paths))[0]
    log_payload = next(
        payload
        for payload in _json_log_payloads(capsys.readouterr().err)
        if payload.get("event") == "Processing" and payload.get("correlation_id") == "$event:localhost"
    )
    turn_record = bot._turn_store.get_turn_record("$event:localhost")
    assert turn_record is not None
    metadata = captured_metadata[0]

    assert llm_entry["correlation_id"] == "$event:localhost"
    assert tool_entry["correlation_id"] == "$event:localhost"
    assert log_payload["correlation_id"] == "$event:localhost"
    assert metadata["correlation_id"] == "$event:localhost"
    assert turn_record.correlation_id == "$event:localhost"

    assert llm_entry["agent_id"] == "general"
    assert llm_entry["model_id"] == "test-model"
    assert llm_entry["requester_id"] == "@user:localhost"
    assert llm_entry["thread_id"] == "$thread-root:localhost"
    assert llm_entry["full_prompt"] == "expanded prompt"
    assert tool_entry["success"] is True
    assert tool_entry["reply_to_event_id"] == "$event:localhost"
    assert tool_entry["requester_id"] == "@user:localhost"
    assert log_payload["agent_id"] == "general"
    assert log_payload["requester_id"] == "@user:localhost"
    assert log_payload["room_id"] == "!test:localhost"
    assert log_payload["thread_id"] == "$thread-root:localhost"
    assert log_payload["session_id"] == target.session_id
    assert log_payload["reply_to_event_id"] == "$event:localhost"
    assert metadata == {
        "room_id": "!test:localhost",
        "thread_id": "$thread-root:localhost",
        "reply_to_event_id": "$event:localhost",
        "requester_id": "@user:localhost",
        "correlation_id": "$event:localhost",
        "tools_schema": [{"name": "demo_tool", "description": "Echo"}],
        "model_params": {"temperature": 0.7},
        "matrix_event_id": "$event:localhost",
        "matrix_seen_event_ids": ["$event:localhost"],
    }
    assert turn_record.requester_id == "@user:localhost"


@pytest.mark.asyncio
async def test_streaming_tool_call_shares_correlation_id_across_streaming_sinks(tmp_path: Path) -> None:
    """Streaming responses should keep tool-call and request logs correlated."""
    runtime_paths = test_runtime_paths(tmp_path)
    config = _config(runtime_paths)
    llm_log_dir = tmp_path / "llm"
    execution_identity = build_tool_execution_identity(
        channel="matrix",
        agent_name="general",
        runtime_paths=runtime_paths,
        requester_id="@user:localhost",
        room_id="!room:localhost",
        thread_id="$thread:localhost",
        resolved_thread_id="$thread:localhost",
        session_id="!room:localhost:$thread:localhost",
    )
    dispatch_context = ToolDispatchContext(execution_identity=execution_identity)
    model = _LoggingModel()
    install_llm_request_logging(
        model,
        agent_name="general",
        debug_config=DebugConfig(log_llm_requests=True, llm_request_log_dir=str(llm_log_dir)),
        default_log_dir=tmp_path / "unused",
    )
    captured_metadata: list[dict[str, object]] = []
    bridge = build_tool_hook_bridge(
        HookRegistry.empty(),
        agent_name="general",
        dispatch_context=dispatch_context,
        config=config,
        runtime_paths=runtime_paths,
    )
    agent = _StreamAgent(model=model, bridge=bridge, captured_metadata=captured_metadata)

    with (
        patch(
            "mindroom.ai._prepare_agent_and_prompt",
            new=AsyncMock(return_value=_prepared_prompt_result(agent)),
        ),
        patch(
            "mindroom.ai.agent_tool_definition_payloads_for_logging",
            return_value=[{"name": "demo_tool", "description": "Echo"}],
        ),
    ):
        chunks = [
            chunk
            async for chunk in stream_agent_response(
                agent_name="general",
                prompt="hello",
                model_prompt="model prompt",
                session_id="!room:localhost:$thread:localhost",
                runtime_paths=runtime_paths,
                config=config,
                room_id="!room:localhost",
                thread_id="$thread:localhost",
                reply_to_event_id="$event:localhost",
                user_id="@user:localhost",
                execution_identity=execution_identity,
            )
        ]

    assert [chunk.content for chunk in chunks if isinstance(chunk, RunContentEvent)] == ["Done"]
    llm_entry = _read_llm_entries(llm_log_dir)[0]
    tool_entry = _read_jsonl(_tool_calls_path(runtime_paths))[0]
    metadata = captured_metadata[0]

    assert llm_entry["correlation_id"] == "$event:localhost"
    assert tool_entry["correlation_id"] == "$event:localhost"
    assert metadata["correlation_id"] == "$event:localhost"
    assert llm_entry["full_prompt"] == "expanded prompt"
    assert metadata["reply_to_event_id"] == "$event:localhost"
    assert metadata["thread_id"] == "$thread:localhost"
    assert metadata["requester_id"] == "@user:localhost"
    assert metadata["tools_schema"] == [{"name": "demo_tool", "description": "Echo"}]


@pytest.mark.asyncio
async def test_non_matrix_request_mints_uuid_correlation_id_across_sinks(tmp_path: Path) -> None:
    """Non-Matrix requests should mint one UUID correlation id and reuse it everywhere."""
    runtime_paths = test_runtime_paths(tmp_path)
    config = _config(runtime_paths)
    llm_log_dir = tmp_path / "llm"
    execution_identity = build_tool_execution_identity(
        channel="openai_compat",
        agent_name="general",
        runtime_paths=runtime_paths,
        requester_id="@api-user:localhost",
        room_id=None,
        thread_id=None,
        resolved_thread_id=None,
        session_id="openai-session",
    )
    dispatch_context = ToolDispatchContext(execution_identity=execution_identity)
    model = _LoggingModel()
    install_llm_request_logging(
        model,
        agent_name="general",
        debug_config=DebugConfig(log_llm_requests=True, llm_request_log_dir=str(llm_log_dir)),
        default_log_dir=tmp_path / "unused",
    )
    captured_metadata: list[dict[str, object]] = []
    bridge = build_tool_hook_bridge(
        HookRegistry.empty(),
        agent_name="general",
        dispatch_context=dispatch_context,
        config=config,
        runtime_paths=runtime_paths,
    )
    agent = _InvokeAgent(model=model, bridge=bridge, captured_metadata=captured_metadata)

    with (
        patch(
            "mindroom.ai._prepare_agent_and_prompt",
            new=AsyncMock(return_value=_prepared_prompt_result(agent)),
        ),
        patch(
            "mindroom.ai.agent_tool_definition_payloads_for_logging",
            return_value=[{"name": "demo_tool", "description": "Echo"}],
        ),
    ):
        response = await ai_response(
            agent_name="general",
            prompt="hello",
            model_prompt="model prompt",
            session_id="openai-session",
            runtime_paths=runtime_paths,
            config=config,
            user_id="@api-user:localhost",
            execution_identity=execution_identity,
        )

    assert response == "Done"
    llm_entry = _read_llm_entries(llm_log_dir)[0]
    tool_entry = _read_jsonl(_tool_calls_path(runtime_paths))[0]
    metadata = captured_metadata[0]
    correlation_id = llm_entry["correlation_id"]

    assert isinstance(correlation_id, str)
    assert re.fullmatch(r"[0-9a-f]{32}", correlation_id)
    assert tool_entry["correlation_id"] == correlation_id
    assert metadata["correlation_id"] == correlation_id
    assert "reply_to_event_id" not in metadata
    assert "room_id" not in metadata
    assert "thread_id" not in metadata
