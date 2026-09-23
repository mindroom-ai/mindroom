"""Failed and cancelled runs retain current request details alongside their totals."""

from __future__ import annotations

import subprocess
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

import pytest
from agno.agent import Agent
from agno.agent import _run as agent_run
from agno.db.base import SessionType
from agno.exceptions import ModelProviderError, RunCancelledException
from agno.metrics import MessageMetrics, RunMetrics
from agno.models.base import MessageData
from agno.models.message import Message
from agno.models.response import ModelResponse
from agno.run.agent import RunOutput
from agno.run.base import RunStatus
from agno.run.messages import RunMessages
from agno.run.team import TeamRunOutput
from agno.session.agent import AgentSession
from agno.team import _run as team_run
from agno.tools.function import Function

from mindroom.agent_storage import create_state_storage
from mindroom.config.agent import AgentConfig
from mindroom.config.main import Config
from mindroom.constants import resolve_runtime_paths
from mindroom.usage_stats import collect_admin_usage
from mindroom.usage_storage import project_usage
from tests.history_helpers import RecordingModel

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Iterator
    from pathlib import Path


@pytest.mark.parametrize("team", [False, True])
@pytest.mark.parametrize("cancelled", [False, True])
@pytest.mark.parametrize("current", ["requests", "transient", "empty"])
def test_terminal_snapshot_keeps_requests_after_checkpoint(
    tmp_path: Path,
    *,
    team: bool,
    cancelled: bool,
    current: str,
) -> None:
    """A stale checkpoint must not hide later requests or persist transient messages."""
    # Creating owned storage installs the repair, as it does in production.
    storage = create_state_storage("status", tmp_path, subdir="sessions", session_table="status_sessions")
    first = Message(role="assistant", content="Checking", metrics=MessageMetrics(input_tokens=10))
    second = Message(role="assistant", content="Checked", metrics=MessageMetrics(input_tokens=20))
    transient = Message(role="user", content="Transient", add_to_agent_memory=False)
    messages = RunMessages(
        messages={"requests": [first, transient, second], "transient": [transient], "empty": []}[current],
    )
    run_type = TeamRunOutput if team else RunOutput
    run = run_type(messages=[first], metrics=RunMetrics(input_tokens=30))
    try:
        if cancelled:
            error = RunCancelledException("Cancelled")
            if team:
                team_run._handle_team_run_cancellation(run, error, messages)
            else:
                agent_run._handle_run_cancellation(run, error, messages)
            assert run.status == RunStatus.cancelled
        elif team:
            team_run.flush_in_flight_messages_on_error_team(run, messages)
        else:
            agent_run.flush_in_flight_messages_on_error(run, messages)
        assert (run.messages or []) == ([first, second] if current == "requests" else [])
        usage = project_usage(run.to_dict())
        assert [request["metrics"]["input_tokens"] for request in usage.get("requests", [])] == (
            [10, 20] if current == "requests" else []
        )
    finally:
        storage.close()


def test_installation_preserves_existing_stream_wrappers() -> None:
    """Installation keeps later wrappers in the chain and repeated calls wrap nothing twice."""
    code = """
import asyncio

from agno.agent import Agent
from agno.agent import _run as agent_run
from agno.models.base import Model
from agno.team import _run as team_run
from mindroom import agno_compat_run_messages as patch
from tests.history_helpers import RecordingModel

events = []
process = Model.process_response_stream
aprocess = Model.aprocess_response_stream

def existing_stream(*args, **kwargs):
    events.append("sync-before")
    yield from process(*args, **kwargs)
    events.append("sync-after")

async def existing_astream(*args, **kwargs):
    events.append("async-before")
    async for event in aprocess(*args, **kwargs):
        yield event
    events.append("async-after")

def installed():
    return (
        Model.process_response_stream,
        Model.aprocess_response_stream,
        agent_run.flush_in_flight_messages_on_error,
        team_run.flush_in_flight_messages_on_error_team,
        agent_run._handle_run_cancellation,
        team_run._handle_team_run_cancellation,
    )

Model.process_response_stream = existing_stream
Model.aprocess_response_stream = existing_astream
patch.install_patch()
first = installed()
patch.install_patch()
assert all(current is original for current, original in zip(installed(), first, strict=True))

agent = Agent(model=RecordingModel(id="test-model", provider="test-provider"), telemetry=False)
assert "".join(event.content or "" for event in agent.run("Hello", stream=True)) == "ok"

async def consume():
    return "".join([event.content or "" async for event in agent.arun("Hello", stream=True)])

assert asyncio.run(consume()) == "ok"
assert events == ["sync-before", "sync-after", "async-before", "async-after"], events
"""
    result = subprocess.run(
        ["uv", "run", "python", "-c", code],
        check=False,
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == 0, result.stderr


@dataclass
class _InterruptedModel(RecordingModel):
    responses: list[ModelResponse] = field(default_factory=list)
    failure: Exception = field(default_factory=lambda: ModelProviderError("Stream failed"))

    def invoke(self, *_args: object, **_kwargs: object) -> ModelResponse:
        if not self.responses:
            raise self.failure
        return self.responses.pop(0)

    async def ainvoke(self, *_args: object, **_kwargs: object) -> ModelResponse:
        return self.invoke()

    def invoke_stream(self, *_args: object, **_kwargs: object) -> Iterator[ModelResponse]:
        yield self.invoke()

    async def ainvoke_stream(self, *_args: object, **_kwargs: object) -> AsyncIterator[ModelResponse]:
        yield self.invoke()


@pytest.mark.parametrize("asynchronous", [False, True])
@pytest.mark.asyncio
async def test_positional_stream_options_keep_cancellation_usage(tmp_path: Path, *, asynchronous: bool) -> None:
    """Agno's positional options must preserve the run owning the interrupted request."""
    storage = create_state_storage("status", tmp_path, subdir="sessions", session_table="status_sessions")
    model = _InterruptedModel(
        id="test-model",
        provider="test-provider",
        responses=[
            ModelResponse(content="Ready", response_usage=MessageMetrics(input_tokens=7, total_tokens=7)),
        ],
    )
    messages = [Message(role="user", content="Check status")]
    assistant = Message(role="assistant")
    run = RunOutput(metrics=RunMetrics())
    options = (messages, assistant, MessageData(), None, None, "auto", run, True)
    stream = model.aprocess_response_stream(*options) if asynchronous else model.process_response_stream(*options)
    try:
        event = await anext(stream) if asynchronous else next(stream)
        assert event.content == "Ready"
        agent_run._handle_run_cancellation(run, RunCancelledException("Cancelled"), RunMessages(messages=messages))
        assert run.metrics.total_tokens == 7
        assert [request["metrics"]["total_tokens"] for request in project_usage(run.to_dict())["requests"]] == [7]
    finally:
        if asynchronous:
            await stream.aclose()
        else:
            stream.close()
        storage.close()


@pytest.mark.parametrize("asynchronous", [False, True])
@pytest.mark.parametrize("cancelled", [False, True])
@pytest.mark.asyncio
async def test_interrupted_continuation_exports_every_completed_request(
    tmp_path: Path,
    *,
    asynchronous: bool,
    cancelled: bool,
) -> None:
    """A resumed run must save post-approval requests even when its last call fails."""
    paths = resolve_runtime_paths(config_path=tmp_path / "config.yaml", storage_path=tmp_path, process_env={})
    config = Config(agents={"status": AgentConfig(display_name="Status")})
    storage = create_state_storage(
        "status",
        tmp_path / "agents/status",
        subdir="sessions",
        session_table="status_sessions",
    )

    def check_status() -> str:
        return "Ready"

    model = _InterruptedModel(
        id="test-model",
        provider="test-provider",
        failure=RunCancelledException("Cancelled") if cancelled else ModelProviderError("Stream failed"),
        responses=[
            ModelResponse(
                role="assistant",
                tool_calls=[
                    {
                        "id": f"call-{index}",
                        "type": "function",
                        "function": {"name": name, "arguments": "{}"},
                    },
                ],
                response_usage=MessageMetrics(input_tokens=inputs, output_tokens=outputs, total_tokens=total),
            )
            for index, (name, inputs, outputs, total) in enumerate(
                [
                    ("approve_status", 7, 3, 10),
                    ("check_status", 14, 6, 20),
                ],
            )
        ],
    )
    agent = Agent(
        id="status",
        model=model,
        db=storage,
        telemetry=False,
        tools=[Function(name="approve_status", entrypoint=check_status, requires_confirmation=True), check_status],
    )
    try:
        paused = agent.run("Check status twice", session_id="session")
        assert paused.status == RunStatus.paused
        for requirement in paused.requirements:
            requirement.confirm()
        if asynchronous:
            async for _ in agent.acontinue_run(
                run_id=paused.run_id,
                session_id="session",
                requirements=paused.requirements,
                stream=True,
            ):
                pass
        else:
            list(
                agent.continue_run(
                    run_id=paused.run_id,
                    session_id="session",
                    requirements=paused.requirements,
                    stream=True,
                ),
            )
        session = storage.get_session("session", session_type=SessionType.AGENT)
        assert isinstance(session, AgentSession)
        assert session.runs[-1].status == (RunStatus.cancelled if cancelled else RunStatus.error)
        assert session.session_data["session_metrics"]["total_tokens"] == 30
        report = collect_admin_usage(config=config, runtime_paths=paths, include_requests=True)
        assert report.totals.total_tokens == 30
        assert sorted(row.totals.total_tokens for row in report.request_breakdown) == [10, 20]
        assert report.request_coverage.unavailable_sources == 0
    finally:
        storage.close()
