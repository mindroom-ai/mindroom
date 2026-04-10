"""Tests for structured LLM request assembly logging."""

from __future__ import annotations

import json
from contextlib import contextmanager
from datetime import UTC, datetime
from types import SimpleNamespace
from typing import TYPE_CHECKING
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from agno.models.message import Message
from agno.run.agent import RunContentEvent, RunOutput
from agno.run.base import RunStatus
from agno.run.team import TeamRunOutput

from mindroom.ai import ai_response, stream_agent_response
from mindroom.config.agent import AgentConfig
from mindroom.config.auth import AuthorizationConfig
from mindroom.config.main import Config
from mindroom.config.models import DebugConfig, ModelConfig
from mindroom.llm_request_logging import (
    _REDACTED,
    LLMRequestRecord,
    _json_safe_value,
    log_llm_request,
    resolve_llm_request_log_dir,
)
from mindroom.team_runtime_resolution import ResolvedExactTeamMembers
from mindroom.teams import TeamMode, team_response
from tests.conftest import bind_runtime_paths, runtime_paths_for, test_runtime_paths

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Iterator
    from pathlib import Path

    from mindroom.constants import RuntimePaths


def _runtime_paths(tmp_path: Path) -> RuntimePaths:
    return test_runtime_paths(tmp_path)


def _config(tmp_path: Path, *, log_llm_requests: bool = False) -> Config:
    return bind_runtime_paths(
        Config(
            agents={"general": AgentConfig(display_name="General", role="Help", rooms=["!room:localhost"])},
            teams={},
            models={"default": ModelConfig(provider="openai", id="test-model")},
            authorization=AuthorizationConfig(default_room_access=True),
            debug=DebugConfig(log_llm_requests=log_llm_requests),
        ),
        _runtime_paths(tmp_path),
    )


class _NonJsonValue:
    def __repr__(self) -> str:
        return "<non-json-value>"


def _request_messages() -> list[Message]:
    return [
        Message(role="system", content="system prompt"),
        Message(role="user", content="hello"),
    ]


def _tool_definition() -> list[dict[str, object]]:
    return [
        {
            "type": "function",
            "function": {
                "name": "lookup",
                "description": "Look up data",
                "parameters": {"type": "object", "properties": {}},
            },
        },
    ]


def _build_completed_run_output(model: _FakeRequestLoggingModel) -> RunOutput:
    return RunOutput(
        run_id="run-1",
        session_id="session-1",
        content="final answer",
        model=model.id,
        model_provider=model.provider,
        messages=[],
        status=RunStatus.completed,
        tools=[],
    )


def _build_completed_team_run_output() -> TeamRunOutput:
    return TeamRunOutput(content="team answer", status=RunStatus.completed)


def _read_log_payloads(log_dir: Path) -> list[dict[str, object]]:
    log_files = list(log_dir.glob("llm_requests_*.jsonl"))
    payloads: list[dict[str, object]] = []
    for log_file in log_files:
        payloads.extend(json.loads(line) for line in log_file.read_text(encoding="utf-8").splitlines() if line.strip())
    return payloads


@contextmanager
def _open_scope() -> Iterator[SimpleNamespace]:
    yield SimpleNamespace(session=None, storage=None, scope=SimpleNamespace(kind="test"))


class _FakeRequestLoggingModel:
    id = "test-model"
    provider = "FakeProvider"

    def _prepare_request_kwargs(
        self,
        system_message: str,
        *,
        tools: list[dict[str, object]] | None = None,
        response_format: object | None = None,
        messages: list[Message] | None = None,
    ) -> dict[str, object]:
        del system_message, response_format, messages
        return {
            "temperature": 0.25,
            "api_key": "super-secret",
            "cache_control": {"type": "ephemeral"},
            "tool_count": 0 if tools is None else len(tools),
        }

    async def ainvoke(
        self,
        messages: list[Message],
        assistant_message: Message,
        response_format: object | None = None,
        tools: list[dict[str, object]] | None = None,
        tool_choice: object | None = None,
        run_response: object | None = None,
        compress_tool_results: bool = False,
    ) -> object:
        del assistant_message, tool_choice, run_response, compress_tool_results
        self._prepare_request_kwargs(
            "system prompt",
            tools=tools,
            response_format=response_format,
            messages=messages,
        )
        return object()

    async def ainvoke_stream(
        self,
        messages: list[Message],
        assistant_message: Message,
        response_format: object | None = None,
        tools: list[dict[str, object]] | None = None,
        tool_choice: object | None = None,
        run_response: object | None = None,
        compress_tool_results: bool = False,
    ) -> AsyncIterator[object]:
        del assistant_message, tool_choice, run_response, compress_tool_results
        self._prepare_request_kwargs(
            "system prompt",
            tools=tools,
            response_format=response_format,
            messages=messages,
        )
        yield object()


def test_debug_config_defaults_and_null_normalization(tmp_path: Path) -> None:
    """`debug: null` should still normalize back to the default debug settings."""
    runtime_paths = _runtime_paths(tmp_path)

    default_config = Config.validate_with_runtime(
        {
            "models": {"default": {"provider": "openai", "id": "test-model"}},
        },
        runtime_paths,
    )
    assert default_config.debug.log_llm_requests is False

    null_debug_config = Config.validate_with_runtime(
        {
            "models": {"default": {"provider": "openai", "id": "test-model"}},
            "debug": None,
        },
        runtime_paths,
    )
    assert null_debug_config.debug.log_llm_requests is False
    assert "debug" not in null_debug_config.authored_model_dump()


def test_resolve_llm_request_log_dir_defaults_and_overrides(tmp_path: Path) -> None:
    """Resolve request assembly logs under storage by default and config-relative overrides when set."""
    runtime_paths = _runtime_paths(tmp_path)

    default_dir = resolve_llm_request_log_dir(runtime_paths=runtime_paths, configured_log_dir=None)
    custom_dir = resolve_llm_request_log_dir(
        runtime_paths=runtime_paths,
        configured_log_dir="./custom-logs/llm",
    )

    assert default_dir == runtime_paths.storage_root / "logs" / "llm_requests"
    assert custom_dir == runtime_paths.config_dir / "custom-logs" / "llm"


def test_json_safe_value_redacts_secret_keys_and_uses_repr_fallback() -> None:
    """Secret-looking keys should be redacted and non-JSON objects should fall back to `repr`."""
    payload = {
        "api_key": "secret",
        "nested": {"client_secret": "other-secret"},
        "custom": _NonJsonValue(),
    }

    sanitized = _json_safe_value(payload)

    assert sanitized["api_key"] == _REDACTED
    assert sanitized["nested"]["client_secret"] == _REDACTED
    assert sanitized["custom"] == "<non-json-value>"


@pytest.mark.asyncio
async def test_log_llm_request_writes_daily_jsonl(tmp_path: Path) -> None:
    """One request assembly record should append to the date-partitioned JSONL file."""
    log_dir = tmp_path / "logs"
    record = LLMRequestRecord(
        timestamp="2026-04-09T12:00:00+00:00",
        agent_name="general",
        session_id="session-1",
        room_id="!room:localhost",
        thread_id="$thread",
        run_id="run-1",
        provider="OpenAI",
        model_config_name="default",
        model_id="test-model",
        system_prompt="system",
        messages=[{"role": "user", "content": "hello"}],
        tools=None,
        model_parameters={"temperature": 0.1},
        cache_metadata=None,
    )

    await log_llm_request(record, log_dir=log_dir, now=datetime(2026, 4, 9, tzinfo=UTC))

    log_path = log_dir / "llm_requests_2026-04-09.jsonl"
    lines = log_path.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 1
    payload = json.loads(lines[0])
    assert payload["agent_name"] == "general"
    assert payload["model_id"] == "test-model"


@pytest.mark.asyncio
async def test_ai_response_logs_llm_request_when_enabled(tmp_path: Path) -> None:
    """Non-streaming agent execution should log one pre-provider request assembly record."""
    config = _config(tmp_path, log_llm_requests=True)
    runtime_paths = runtime_paths_for(config)
    model = _FakeRequestLoggingModel()

    async def fake_cached_attempt(*_args: object, **_kwargs: object) -> RunOutput:
        await model.ainvoke(
            _request_messages(),
            Message(role="assistant", content=""),
            tools=_tool_definition(),
        )
        return _build_completed_run_output(model)

    agent = MagicMock()
    agent.model = model

    with (
        patch("mindroom.ai.open_resolved_scope_session_context", side_effect=lambda **_kwargs: _open_scope()),
        patch("mindroom.ai._prepare_agent_and_prompt", new=AsyncMock(return_value=(agent, "prompt", [], MagicMock()))),
        patch("mindroom.ai._run_cached_agent_attempt", new=AsyncMock(side_effect=fake_cached_attempt)),
        patch("mindroom.ai.close_agent_runtime_sqlite_dbs"),
    ):
        response = await ai_response(
            agent_name="general",
            prompt="hello",
            session_id="session-1",
            runtime_paths=runtime_paths,
            config=config,
            room_id="!room:localhost",
            thread_id="$thread-root",
            run_id="run-1",
        )

    assert response == "final answer"
    log_dir = runtime_paths.storage_root / "logs" / "llm_requests"
    payloads = _read_log_payloads(log_dir)
    assert len(payloads) == 1
    payload = payloads[0]
    assert payload["agent_name"] == "general"
    assert payload["session_id"] == "session-1"
    assert payload["room_id"] == "!room:localhost"
    assert payload["thread_id"] == "$thread-root"
    assert payload["run_id"] == "run-1"
    assert payload["provider"] == "FakeProvider"
    assert payload["model_config_name"] == "default"
    assert payload["model_id"] == "test-model"
    assert payload["system_prompt"] == "system prompt"
    assert payload["model_parameters"]["api_key"] == "***redacted***"
    assert payload["cache_metadata"]["model_parameters"]["cache_control"]["type"] == "ephemeral"
    assert payload["tools"][0]["function"]["name"] == "lookup"


@pytest.mark.asyncio
async def test_stream_agent_response_logs_llm_request_when_enabled(tmp_path: Path) -> None:
    """Streaming agent execution should emit the same request assembly log shape."""
    config = _config(tmp_path, log_llm_requests=True)
    runtime_paths = runtime_paths_for(config)
    model = _FakeRequestLoggingModel()

    async def fake_stream(_prompt: str, **_kwargs: object) -> AsyncIterator[RunContentEvent]:
        async for _ in model.ainvoke_stream(
            _request_messages(),
            Message(role="assistant", content=""),
            tools=_tool_definition(),
        ):
            pass
        yield RunContentEvent(content="streamed answer")

    agent = MagicMock()
    agent.model = model
    agent.arun = MagicMock(return_value=fake_stream("prompt"))

    with (
        patch("mindroom.ai.open_resolved_scope_session_context", side_effect=lambda **_kwargs: _open_scope()),
        patch("mindroom.ai._prepare_agent_and_prompt", new=AsyncMock(return_value=(agent, "prompt", [], MagicMock()))),
        patch("mindroom.ai.close_agent_runtime_sqlite_dbs"),
    ):
        chunks = [
            chunk
            async for chunk in stream_agent_response(
                agent_name="general",
                prompt="hello",
                session_id="session-1",
                runtime_paths=runtime_paths,
                config=config,
                room_id="!room:localhost",
                thread_id="$thread-root",
                run_id="run-2",
            )
        ]

    assert len(chunks) == 1
    assert isinstance(chunks[0], RunContentEvent)
    log_dir = runtime_paths.storage_root / "logs" / "llm_requests"
    payloads = _read_log_payloads(log_dir)
    assert len(payloads) == 1
    payload = payloads[0]
    assert payload["agent_name"] == "general"
    assert payload["run_id"] == "run-2"
    assert payload["thread_id"] == "$thread-root"
    assert payload["system_prompt"] == "system prompt"


@pytest.mark.asyncio
async def test_ai_response_does_not_log_when_disabled(tmp_path: Path) -> None:
    """Disabling request logging should leave the log directory untouched."""
    config = _config(tmp_path, log_llm_requests=False)
    runtime_paths = runtime_paths_for(config)
    model = _FakeRequestLoggingModel()

    async def fake_cached_attempt(*_args: object, **_kwargs: object) -> RunOutput:
        await model.ainvoke(
            _request_messages(),
            Message(role="assistant", content=""),
            tools=_tool_definition(),
        )
        return _build_completed_run_output(model)

    agent = MagicMock()
    agent.model = model

    with (
        patch("mindroom.ai.open_resolved_scope_session_context", side_effect=lambda **_kwargs: _open_scope()),
        patch("mindroom.ai._prepare_agent_and_prompt", new=AsyncMock(return_value=(agent, "prompt", [], MagicMock()))),
        patch("mindroom.ai._run_cached_agent_attempt", new=AsyncMock(side_effect=fake_cached_attempt)),
        patch("mindroom.ai.close_agent_runtime_sqlite_dbs"),
    ):
        response = await ai_response(
            agent_name="general",
            prompt="hello",
            session_id="session-1",
            runtime_paths=runtime_paths,
            config=config,
        )

    assert response == "final answer"
    log_dir = runtime_paths.storage_root / "logs" / "llm_requests"
    assert not log_dir.exists()


@pytest.mark.asyncio
async def test_team_response_logs_llm_request_when_enabled(tmp_path: Path) -> None:
    """Team execution should log the coordinator model's request assembly data."""
    config = _config(tmp_path, log_llm_requests=True)
    runtime_paths = runtime_paths_for(config)
    orchestrator = MagicMock()
    orchestrator.config = config
    orchestrator.runtime_paths = runtime_paths
    orchestrator.knowledge_managers = {}
    orchestrator.agent_bots = {"general": MagicMock()}

    model = _FakeRequestLoggingModel()
    team = MagicMock()
    team.id = "team-logger"
    team.name = "Team Logger"
    team.model = model
    team.db = None

    async def fake_team_arun(*_args: object, **_kwargs: object) -> TeamRunOutput:
        await model.ainvoke(
            _request_messages(),
            Message(role="assistant", content=""),
            tools=_tool_definition(),
        )
        return _build_completed_team_run_output()

    team.arun = AsyncMock(side_effect=fake_team_arun)
    team_members = ResolvedExactTeamMembers(
        requested_agent_names=["general"],
        agents=[],
        display_names=["General"],
        materialized_agent_names={"general"},
        failed_agent_names=[],
    )
    prepared_execution = SimpleNamespace(prepared_prompt="team prompt", run_metadata=None, unseen_event_ids=[])

    with (
        patch("mindroom.teams._ensure_request_team_knowledge_managers", new=AsyncMock(return_value={})),
        patch("mindroom.teams._materialize_team_members", return_value=team_members),
        patch("mindroom.teams.open_bound_scope_session_context", side_effect=lambda **_kwargs: _open_scope()),
        patch("mindroom.teams.build_materialized_team_instance", return_value=team),
        patch("mindroom.teams.prepare_materialized_team_execution", new=AsyncMock(return_value=prepared_execution)),
        patch("mindroom.teams.close_team_runtime_sqlite_dbs"),
    ):
        response = await team_response(
            agent_names=["general"],
            mode=TeamMode.COORDINATE,
            message="hello",
            orchestrator=orchestrator,
            execution_identity=None,
            session_id="team-session-1",
            room_id="!room:localhost",
            thread_id="$team-thread",
            run_id="team-run-1",
        )

    assert "team answer" in response
    log_dir = runtime_paths.storage_root / "logs" / "llm_requests"
    payloads = _read_log_payloads(log_dir)
    assert len(payloads) == 1
    payload = payloads[0]
    assert payload["agent_name"] == "team-logger"
    assert payload["session_id"] == "team-session-1"
    assert payload["room_id"] == "!room:localhost"
    assert payload["thread_id"] == "$team-thread"
    assert payload["run_id"] == "team-run-1"
    assert payload["model_id"] == "test-model"
