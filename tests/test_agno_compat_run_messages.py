"""Failed and cancelled runs retain current request details alongside their totals."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING

import pytest
from agno.agent import Agent
from agno.agent import _run as agent_run
from agno.db.base import SessionType
from agno.exceptions import ModelProviderError, RunCancelledException
from agno.metrics import MessageMetrics, RunMetrics
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
def test_terminal_snapshot_keeps_requests_after_checkpoint(tmp_path: Path, *, team: bool, cancelled: bool) -> None:
    """A stale checkpoint must not hide later requests or persist transient messages."""
    storage = create_state_storage("status", tmp_path, subdir="sessions", session_table="status_sessions")
    first = Message(role="assistant", content="Checking", metrics=MessageMetrics(input_tokens=10))
    second = Message(role="assistant", content="Checked", metrics=MessageMetrics(input_tokens=20))
    transient = Message(role="user", content="Transient", add_to_agent_memory=False)
    messages = RunMessages(messages=[first, transient, second])
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
        assert run.messages == [first, second]
        usage = project_usage(run.to_dict())
        assert [request["metrics"]["input_tokens"] for request in usage["requests"]] == [10, 20]
    finally:
        storage.close()


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
