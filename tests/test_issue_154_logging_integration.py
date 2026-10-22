"""Integration coverage for ISSUE-154 logging traceability."""

from __future__ import annotations

import json
import logging
import re
import time
import uuid
from json import JSONDecodeError
from types import SimpleNamespace
from typing import TYPE_CHECKING
from unittest.mock import AsyncMock, MagicMock

import pytest
import structlog
from agno.tools import Toolkit

from mindroom.ai import ai_response, stream_agent_response
from mindroom.config.agent import AgentConfig
from mindroom.config.main import Config
from mindroom.config.models import DebugConfig, ModelConfig
from mindroom.constants import AI_RUN_METADATA_KEY, tracking_dir
from mindroom.handled_turns import TurnRecord
from mindroom.logging_config import get_logger, setup_logging
from mindroom.matrix.users import AgentMatrixUser
from mindroom.message_target import MessageTarget
from mindroom.providers.pipeline.compose import wrap_provider
from mindroom.providers.pipeline.request_log import RequestLog
from mindroom.response_payload_preparation import DispatchPayloadInputs
from mindroom.tool_system.runtime_context import ToolDispatchContext
from mindroom.tool_system.worker_routing import build_tool_execution_identity
from mindroom.turn_policy import PreparedDispatch, ResponseAction
from tests.bot_helpers import make_test_agent_bot
from tests.conftest import (
    bind_runtime_paths,
    make_turn_context,
    replace_turn_controller_deps,
    request_envelope,
    runtime_paths_for,
    test_runtime_paths,
)
from tests.fakes.scripted_model import ScriptedModel, answer, calls, tool_call, usage
from tests.scripted_agent_helpers import install_agent_model, registered_toolkit, stored_runs, streamed_text

if TYPE_CHECKING:
    from collections.abc import Iterator
    from pathlib import Path

    from mindroom.constants import RuntimePaths


@pytest.fixture(autouse=True)
def _reset_logging_after_test() -> Iterator[None]:
    yield
    logging.shutdown()
    logging.getLogger().handlers.clear()
    for logger in logging.root.manager.loggerDict.values():
        if isinstance(logger, logging.Logger):
            logger.handlers.clear()
    structlog.reset_defaults()


DEMO_TOOL = "issue_154_demo"


class _DemoTools(Toolkit):
    """One tool whose call log the tests read: it echoes its text and a credential the log must redact."""

    def __init__(self) -> None:
        super().__init__(name="demo", tools=[self.demo_tool])

    async def demo_tool(self, text: str, authorization: str) -> str:
        """Echo the text.

        Args:
            text: What to echo.
            authorization: A credential the call log redacts.

        """
        return json.dumps({"echo": text, "authorization": authorization})


def _install_logged_model(
    monkeypatch: pytest.MonkeyPatch,
    llm_log_dir: Path,
    *,
    stream_text: str = "Done",
) -> ScriptedModel:
    """Answer the agent with one demo tool call and then ``stream_text``, behind the request log."""
    model = ScriptedModel(
        [
            calls(tool_call("demo_tool", "call-1", text="hello", authorization="Bearer hidden-token")),
            answer(stream_text, usage(4, 2, time_to_first_token=0.12)),
        ],
    )
    install_agent_model(
        monkeypatch,
        wrap_provider(
            model,
            idle_timeout_seconds=None,
            media_fallback_prompt="",
            request_log=RequestLog(
                model_name="default",
                configured_provider="openai",
                debug=DebugConfig(log_llm_requests=True, llm_request_log_dir=str(llm_log_dir)),
                default_log_dir=llm_log_dir / "unused",
            ),
        ),
    )
    return model


@pytest.fixture
def demo_tool() -> Iterator[None]:
    """Register the demo toolkit for the test."""
    with registered_toolkit(DEMO_TOOL, _DemoTools):
        yield


def _config(runtime_paths: RuntimePaths) -> Config:
    config = bind_runtime_paths(
        Config(
            agents={"general": AgentConfig(display_name="General", tools=[DEMO_TOOL])},
            models={"default": ModelConfig(provider="openai", id="test-model")},
            debug=DebugConfig(log_llm_requests=True),
        ),
        runtime_paths,
    )
    config.memory.backend = "none"
    return config


def _read_jsonl(path: Path) -> list[dict[str, object]]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line]


def _read_llm_entries(log_dir: Path) -> list[dict[str, object]]:
    """Return the logged requests, without the usage lines that follow them."""
    log_files = list(log_dir.glob("llm-requests-*.jsonl"))
    assert len(log_files) == 1
    return [entry for entry in _read_jsonl(log_files[0]) if "messages" in entry]


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


def _stored_metadata(config: Config, runtime_paths: RuntimePaths, session_id: str) -> dict[str, object]:
    """Return the run metadata the reply stored with its run."""
    [run] = stored_runs(config, runtime_paths, session_id)
    assert run.metadata is not None
    return run.metadata


@pytest.mark.asyncio
@pytest.mark.usefixtures("demo_tool")
async def test_cross_sink_correlation_invariant_for_matrix_turn_processing_log(
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
    bot = make_test_agent_bot(
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
    dispatch_context = ToolDispatchContext(
        execution_identity=build_tool_execution_identity(
            channel="matrix",
            agent_name="general",
            runtime_paths=runtime_paths,
            requester_id="@user:localhost",
            room_id=target.room_id,
            thread_id=target.resolved_thread_id,
            resolved_thread_id=target.resolved_thread_id,
            session_id=target.session_id,
        ),
    )
    model = _install_logged_model(monkeypatch, llm_log_dir)

    async def generate_response(request: PreparedDispatch) -> str | None:
        response = await ai_response(
            make_turn_context(
                "general",
                session_id=target.session_id or "session-1",
                correlation_id=request.reply_to_event_id or "corr-test",
                reply_to_event_id=request.reply_to_event_id,
                room_id=request.room_id,
                thread_id=request.thread_id,
                requester_id=request.user_id,
                matrix_run_metadata=request.matrix_run_metadata,
            ),
            prompt=request.prompt,
            model_prompt="model prompt",
            runtime_paths=runtime_paths,
            config=config,
            execution_identity=dispatch_context.execution_identity,
            show_tool_calls=False,
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
        server_timestamp=1234567890,
        source={},
    )
    dispatch = PreparedDispatch(
        requester_user_id="@user:localhost",
        context=SimpleNamespace(
            am_i_mentioned=False,
            thread_id=target.resolved_thread_id,
            thread_history=(),
            planning_thread_history=(),
            requires_model_history_refresh=False,
        ),
        target=target,
        correlation_id="$event:localhost",
        envelope=request_envelope(
            room_id=target.room_id,
            reply_to_event_id=target.reply_to_event_id or "$event:localhost",
            target=target,
            prompt=event.body,
            user_id="@user:localhost",
            agent_name="general",
        ),
    )

    await controller._execute_response_action(
        room,
        event,
        dispatch,
        ResponseAction(kind="individual"),
        DispatchPayloadInputs((), (), ()),
        processing_log="Processing",
        dispatch_started_at=time.monotonic(),
        handled_turn=TurnRecord.create(
            [event.event_id],
            requester_id="@user:localhost",
        ),
    )

    llm_entry = _read_llm_entries(llm_log_dir)[0]
    tool_entry = _read_jsonl(_tool_calls_path(runtime_paths))[0]
    log_payload = next(
        payload
        for payload in _json_log_payloads(capsys.readouterr().err)
        if payload.get("event") == "Processing" and payload.get("correlation_id") == "$event:localhost"
    )
    metadata = _stored_metadata(config, runtime_paths, target.session_id or "session-1")

    assert llm_entry["correlation_id"] == "$event:localhost"
    assert tool_entry["correlation_id"] == "$event:localhost"
    assert log_payload["correlation_id"] == "$event:localhost"
    assert metadata["correlation_id"] == "$event:localhost"

    assert llm_entry["agent_id"] == "general"
    assert llm_entry["model_id"] == "scripted-model"
    assert llm_entry["requester_id"] == "@user:localhost"
    assert llm_entry["thread_id"] == "$thread-root:localhost"
    assert llm_entry["full_prompt"] == model.requests[0].messages[-1].content
    assert "model prompt" in str(llm_entry["full_prompt"])
    assert tool_entry["success"] is True
    assert tool_entry["reply_to_event_id"] == "$event:localhost"
    assert tool_entry["requester_id"] == "@user:localhost"
    assert log_payload["agent_id"] == "general"
    assert log_payload["requester_id"] == "@user:localhost"
    assert log_payload["room_id"] == "!test:localhost"
    assert log_payload["thread_id"] == "$thread-root:localhost"
    assert log_payload["session_id"] == target.session_id
    assert log_payload["reply_to_event_id"] == "$event:localhost"
    assert {key: value for key, value in metadata.items() if key != AI_RUN_METADATA_KEY} == {
        "room_id": "!test:localhost",
        "thread_id": "$thread-root:localhost",
        "reply_to_event_id": "$event:localhost",
        "requester_id": "@user:localhost",
        "correlation_id": "$event:localhost",
        "model_params": {},
        "matrix_event_id": "$event:localhost",
        "matrix_seen_event_ids": ["$event:localhost"],
    }


@pytest.mark.asyncio
@pytest.mark.usefixtures("demo_tool")
async def test_streaming_tool_call_shares_correlation_id_across_streaming_sinks(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
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
    model = _install_logged_model(monkeypatch, llm_log_dir)

    chunks = [
        chunk
        async for chunk in stream_agent_response(
            make_turn_context(
                "general",
                session_id="!room:localhost:$thread:localhost",
                correlation_id="$event:localhost",
                reply_to_event_id="$event:localhost",
                room_id="!room:localhost",
                thread_id="$thread:localhost",
                requester_id="@user:localhost",
            ),
            prompt="hello",
            model_prompt="model prompt",
            runtime_paths=runtime_paths,
            config=config,
            execution_identity=execution_identity,
        )
    ]

    assert "".join(streamed_text(chunks)) == "Done"
    llm_entry = _read_llm_entries(llm_log_dir)[0]
    tool_entry = _read_jsonl(_tool_calls_path(runtime_paths))[0]
    metadata = _stored_metadata(config, runtime_paths, "!room:localhost:$thread:localhost")

    assert llm_entry["correlation_id"] == "$event:localhost"
    assert tool_entry["correlation_id"] == "$event:localhost"
    assert metadata["correlation_id"] == "$event:localhost"
    assert llm_entry["full_prompt"] == model.requests[0].messages[-1].content
    assert metadata["reply_to_event_id"] == "$event:localhost"
    assert metadata["thread_id"] == "$thread:localhost"
    assert metadata["requester_id"] == "@user:localhost"


@pytest.mark.asyncio
@pytest.mark.usefixtures("demo_tool")
async def test_non_matrix_request_mints_uuid_correlation_id_across_sinks(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
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
    _install_logged_model(monkeypatch, llm_log_dir)

    expected_correlation_id = uuid.uuid4().hex
    response = await ai_response(
        make_turn_context(
            "general",
            session_id="openai-session",
            correlation_id=expected_correlation_id,
            requester_id="@api-user:localhost",
        ),
        prompt="hello",
        model_prompt="model prompt",
        runtime_paths=runtime_paths,
        config=config,
        execution_identity=execution_identity,
        show_tool_calls=False,
    )

    assert response == "Done"
    llm_entry = _read_llm_entries(llm_log_dir)[0]
    tool_entry = _read_jsonl(_tool_calls_path(runtime_paths))[0]
    metadata = _stored_metadata(config, runtime_paths, "openai-session")
    correlation_id = llm_entry["correlation_id"]

    assert isinstance(correlation_id, str)
    assert re.fullmatch(r"[0-9a-f]{32}", correlation_id)
    assert correlation_id == expected_correlation_id
    assert tool_entry["correlation_id"] == correlation_id
    assert metadata["correlation_id"] == correlation_id
    assert "reply_to_event_id" not in metadata
    assert "room_id" not in metadata
    assert "thread_id" not in metadata
