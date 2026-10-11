"""Compaction between two model requests of one turn."""
# ruff: noqa: D103

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

import pytest
from agno.agent import Agent
from agno.db.in_memory import InMemoryDb
from agno.db.sqlite import SqliteDb
from agno.media import Image
from agno.metrics import MessageMetrics
from agno.models.message import Message
from agno.models.response import ModelResponse
from agno.run.agent import RunOutput
from agno.run.base import RunStatus
from agno.session.summary import SessionSummary
from agno.team import Team
from agno.tools.function import Function

from mindroom.agents import create_agent
from mindroom.agno_compat_model_hooks import install_request_preparation
from mindroom.config.models import CompactionConfig, ModelConfig
from mindroom.constants import QUEUED_MESSAGE_NOTICE_MARKER_KEY as _NOTICE_KEY
from mindroom.history import agno_compat_message_builder
from mindroom.history.mid_turn_compaction import install_mid_turn_compaction
from mindroom.history.replay import is_compaction_summary
from mindroom.synthetic_model import SyntheticModel
from mindroom.usage_storage import project_usage
from tests.conftest import FakeModel
from tests.history_helpers import _make_config

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Callable
    from pathlib import Path

    from mindroom.config.main import Config
    from mindroom.constants import RuntimePaths

_TOOL_RESULT = "r" * 4000


@pytest.fixture(autouse=True)
def _install_message_builder_patch() -> None:
    agno_compat_message_builder.apply_patch()


@dataclass
class _Request:
    roles: list[str]
    contents: list[str]
    messages: list[Message]


class _ToolLoopModel(SyntheticModel):
    """Call ``probe`` for ``tool_rounds`` requests, then answer; record every request."""

    def __init__(
        self,
        *,
        tool_rounds: int,
        calls_per_round: int = 1,
        usage: Callable[[int], MessageMetrics] | None = None,
    ) -> None:
        super().__init__(id="test-model", name="test", provider="test")
        self.tool_rounds = tool_rounds
        self.calls_per_round = calls_per_round
        self.usage = usage
        self.requests: list[_Request] = []

    async def ainvoke(self, messages: list[Message], **_kwargs: object) -> ModelResponse:
        self.requests.append(
            _Request(
                roles=[message.role for message in messages],
                contents=[str(message.content) for message in messages],
                messages=list(messages),
            ),
        )
        number = len(self.requests)
        metrics = self.usage(number) if self.usage is not None else MessageMetrics()
        if number <= self.tool_rounds:
            return ModelResponse(
                content=f"step {number}",
                tool_calls=[
                    {
                        "id": f"call_{number}_{index}",
                        "type": "function",
                        "function": {"name": "probe", "arguments": "{}"},
                    }
                    for index in range(self.calls_per_round)
                ],
                response_usage=metrics,
            )
        return ModelResponse(content="done", response_usage=metrics)

    async def ainvoke_stream(self, messages: list[Message], **kwargs: object) -> AsyncIterator[ModelResponse]:
        yield await self.ainvoke(messages, **kwargs)


def probe() -> str:
    """Return one large tool result."""
    return _TOOL_RESULT


@dataclass
class _SummaryCalls:
    inputs: list[str] = field(default_factory=list)
    fail: bool = False

    async def __call__(
        self,
        *,
        model: object,  # noqa: ARG002
        summary_input: str,
        summary_prompt: str,  # noqa: ARG002
        timeout_seconds: float,  # noqa: ARG002
        on_response: Callable[[ModelResponse], Any] | None = None,
    ) -> SessionSummary:
        self.inputs.append(summary_input)
        if on_response is not None:
            await on_response(ModelResponse(response_usage=MessageMetrics(input_tokens=7, output_tokens=3)))
        if self.fail:
            msg = "summary model unavailable"
            raise RuntimeError(msg)
        return SessionSummary(summary=f"SUMMARY-{len(self.inputs)}", updated_at=datetime.now(UTC))


@pytest.fixture
def summary_calls(monkeypatch: pytest.MonkeyPatch) -> _SummaryCalls:
    calls = _SummaryCalls()
    monkeypatch.setattr("mindroom.history.compaction.generate_compaction_summary", calls)
    monkeypatch.setattr(
        "mindroom.model_loading.get_model_instance",
        lambda *_args, **_kwargs: FakeModel(id="summary-model", provider="fake"),
    )
    return calls


def _config(
    tmp_path: Path,
    *,
    enabled: bool = True,
    context_window: int | None = 4000,
) -> tuple[Config, RuntimePaths]:
    return _make_config(
        tmp_path,
        defaults_compaction=CompactionConfig(enabled=enabled, model="summary", reserve_tokens=500),
        models={
            "default": ModelConfig(provider="openai", id="test-model", context_window=context_window),
            "summary": ModelConfig(provider="openai", id="summary-model", context_window=100_000),
        },
    )


def _agent(
    model: _ToolLoopModel,
    config: Config,
    runtime_paths: RuntimePaths,
    **kwargs: Any,  # noqa: ANN401
) -> Agent:
    agent = Agent(
        id="test_agent",
        model=model,
        tools=[probe],
        add_history_to_context=False,
        telemetry=False,
        **kwargs,
    )
    install_mid_turn_compaction(
        agent,
        config=config,
        runtime_paths=runtime_paths,
        entity_name="test_agent",
        model_name="default",
    )
    return agent


async def _run(agent: Agent, prompt: list[Message] | str, *, stream: bool = False) -> RunOutput:
    if not stream:
        output = await agent.arun(prompt)
        assert isinstance(output, RunOutput)
        return output
    final: RunOutput | None = None
    async for event in agent.arun(prompt, stream=True, yield_run_output=True):
        if isinstance(event, RunOutput):
            final = event
    assert final is not None
    return final


def _first_compacted(model: _ToolLoopModel) -> _Request:
    return next(request for request in model.requests if any(is_compaction_summary(m) for m in request.messages))


@pytest.mark.asyncio
@pytest.mark.parametrize("stream", [False, True])
async def test_run_local_tool_loop_compacts_and_finishes(
    tmp_path: Path,
    summary_calls: _SummaryCalls,
    *,
    stream: bool,
) -> None:
    config, paths = _config(tmp_path)
    model = _ToolLoopModel(tool_rounds=5)

    output = await _run(_agent(model, config, paths), [Message(role="user", content="Do the task")], stream=stream)

    assert str(output.content).endswith("done")
    assert summary_calls.inputs
    compacted = _first_compacted(model)
    summaries = [message for message in compacted.messages if is_compaction_summary(message)]
    assert len(summaries) == 1
    assert summaries[0].from_history is False
    assert compacted.contents[-1] == "Do the task"
    assert "tool" not in compacted.roles
    assert all(message.role != "assistant" for message in compacted.messages)
    stored_roles = [message.role for message in output.messages or []]
    assert stored_roles.count("tool") < 5


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("enabled", "context_window"),
    [(False, 4000), (True, None)],
    ids=["disabled", "no_context_window"],
)
async def test_mid_turn_compaction_skips_when_disabled_or_unavailable(
    tmp_path: Path,
    summary_calls: _SummaryCalls,
    *,
    enabled: bool,
    context_window: int | None,
) -> None:
    config, paths = _config(tmp_path, enabled=enabled, context_window=context_window)
    model = _ToolLoopModel(tool_rounds=5)

    output = await _run(_agent(model, config, paths), "Do the task")

    assert str(output.content).endswith("done")
    assert summary_calls.inputs == []
    assert [request.roles.count("tool") for request in model.requests] == [0, 1, 2, 3, 4, 5]


@pytest.mark.asyncio
async def test_failed_mid_turn_compaction_does_not_retry_in_the_same_run(
    tmp_path: Path,
    summary_calls: _SummaryCalls,
) -> None:
    config, paths = _config(tmp_path)
    summary_calls.fail = True
    model = _ToolLoopModel(tool_rounds=6)

    output = await _run(_agent(model, config, paths), "Do the task")

    assert str(output.content).endswith("done")
    assert len(summary_calls.inputs) == 1
    assert not any(is_compaction_summary(message) for request in model.requests for message in request.messages)


@pytest.mark.asyncio
async def test_failed_mid_turn_compaction_does_not_carry_into_the_next_run(
    tmp_path: Path,
    summary_calls: _SummaryCalls,
) -> None:
    config, paths = _config(tmp_path)
    summary_calls.fail = True
    model = _ToolLoopModel(tool_rounds=6)
    agent = _agent(model, config, paths)
    await _run(agent, "Do the task")
    summary_calls.fail = False
    model.requests.clear()
    model.tool_rounds = 6

    output = await _run(agent, "Do it again")

    assert str(output.content).endswith("done")
    assert len(summary_calls.inputs) >= 2
    assert any(is_compaction_summary(message) for request in model.requests for message in request.messages)


@pytest.mark.asyncio
async def test_request_sizing_uses_the_latest_response_usage(tmp_path: Path, summary_calls: _SummaryCalls) -> None:
    config, paths = _config(tmp_path, context_window=1_000_000)
    model = _ToolLoopModel(tool_rounds=2, usage=lambda number: MessageMetrics(input_tokens=999_000 * number))

    await _run(_agent(model, config, paths), "Do the task")

    # The estimate alone stays far below the window; only the reported usage triggers compaction.
    assert summary_calls.inputs
    assert not any(is_compaction_summary(message) for message in model.requests[0].messages)
    assert any(is_compaction_summary(message) for message in model.requests[1].messages)


@pytest.mark.asyncio
async def test_usage_less_server_never_anchors_sizing(tmp_path: Path, summary_calls: _SummaryCalls) -> None:
    config, paths = _config(tmp_path)
    model = _ToolLoopModel(tool_rounds=5, usage=lambda _number: MessageMetrics())

    await _run(_agent(model, config, paths), "Do the task")

    assert summary_calls.inputs
    assert max(request.roles.count("tool") for request in model.requests) < 5


@pytest.mark.asyncio
async def test_unseen_thread_context_is_folded_and_transient_context_kept(
    tmp_path: Path,
    summary_calls: _SummaryCalls,
) -> None:
    config, paths = _config(tmp_path)
    model = _ToolLoopModel(tool_rounds=4)
    unseen = Message(role="user", content="u" * 6000)
    transient = Message(role="user", content="transient turn context", add_to_agent_memory=False)
    prompt = Message(role="user", content="Do the task")

    await _run(_agent(model, config, paths), [unseen, transient, prompt])

    compacted = _first_compacted(model)
    assert "u" * 6000 not in compacted.contents
    assert compacted.contents[-2:] == ["transient turn context", "Do the task"]
    assert "u" * 100 in summary_calls.inputs[0]
    assert "transient turn context" not in summary_calls.inputs[0]


@pytest.mark.asyncio
async def test_rewritten_request_carries_no_response_chain(tmp_path: Path, summary_calls: _SummaryCalls) -> None:
    config, paths = _config(tmp_path)
    model = _ToolLoopModel(tool_rounds=5)

    async def chain(messages: list[Message], *_args: object) -> None:
        for message in messages:
            if message.role == "assistant":
                message.provider_data = {**(message.provider_data or {}), "response_id": "resp"}

    agent = _agent(model, config, paths)
    install_request_preparation(model, marker="_test_chain", prepare=chain)

    await _run(agent, "Do the task")

    assert summary_calls.inputs
    compacted = _first_compacted(model)
    assert not any((message.provider_data or {}).get("response_id") for message in compacted.messages)


@pytest.mark.asyncio
@pytest.mark.parametrize("limit", [0, 1])
async def test_snapshot_summary_keeps_current_turn_tool_results_despite_history_limits(
    tmp_path: Path,
    summary_calls: _SummaryCalls,
    limit: int,
) -> None:
    config, paths = _config(tmp_path)
    model = _ToolLoopModel(tool_rounds=5)

    await _run(_agent(model, config, paths, max_tool_calls_from_history=limit), "Do the task")

    assert summary_calls.inputs
    assert summary_calls.inputs[0].count(_TOOL_RESULT[:200]) >= 2


@pytest.mark.asyncio
async def test_rewritten_request_over_the_limit_raises_the_summary_budget_error(
    tmp_path: Path,
    summary_calls: _SummaryCalls,
) -> None:
    config, paths = _config(tmp_path)
    model = _ToolLoopModel(tool_rounds=5)

    output = await _run(_agent(model, config, paths), [Message(role="user", content="p" * 16_000)])

    assert summary_calls.inputs
    assert "Saved conversation summary exceeds the available history budget" in str(output.content)


@pytest.mark.asyncio
async def test_folded_request_usage_stays_in_the_usage_projection(
    tmp_path: Path,
    summary_calls: _SummaryCalls,
) -> None:
    config, paths = _config(tmp_path)
    model = _ToolLoopModel(
        tool_rounds=8,
        usage=lambda number: MessageMetrics(input_tokens=1001 * number, output_tokens=1),
    )

    output = await _run(_agent(model, config, paths), "Do the task")

    assert len(summary_calls.inputs) >= 2
    projected = project_usage(output.to_dict())
    requests = projected["requests"]
    assert [request["metrics"]["input_tokens"] for request in requests] == [
        1001 * number for number in range(1, len(model.requests) + 1)
    ]


@pytest.mark.asyncio
async def test_queued_notice_survives_mid_turn_compaction(tmp_path: Path, summary_calls: _SummaryCalls) -> None:
    config, paths = _config(tmp_path)
    model = _ToolLoopModel(tool_rounds=5)
    agent = _agent(model, config, paths)
    notice = Message(role="user", content="A newer message is waiting.", provider_data={_NOTICE_KEY: True})

    async def add_notice(messages: list[Message], *_args: object) -> None:
        if len(model.requests) == 2 and notice not in messages:
            messages.append(notice)

    install_request_preparation(model, marker="_test_notice", prepare=add_notice)

    await _run(agent, "Do the task")

    assert summary_calls.inputs
    compacted = _first_compacted(model)
    assert compacted.contents[-2:] == ["Do the task", "A newer message is waiting."]


@pytest.mark.asyncio
async def test_parallel_tool_batch_is_folded_whole(tmp_path: Path, summary_calls: _SummaryCalls) -> None:
    config, paths = _config(tmp_path)
    model = _ToolLoopModel(tool_rounds=3, calls_per_round=2)

    await _run(_agent(model, config, paths), "Do the task")

    compacted = _first_compacted(model)
    assert "tool" not in compacted.roles
    assert "assistant" not in compacted.roles
    assert summary_calls.inputs[0].count('tool_call_id="call_1_') == 2


@pytest.mark.asyncio
async def test_folded_tool_media_is_summarized_not_kept(tmp_path: Path, summary_calls: _SummaryCalls) -> None:
    config, paths = _config(tmp_path)
    model = _ToolLoopModel(tool_rounds=5)
    agent = _agent(model, config, paths)
    media = Message(
        role="user",
        content="The tool call above generated the attached media.",
        images=[Image(id="tool_image", content=b"\x89PNG" + b"0" * 400, mime_type="image/png")],
    )

    async def add_media(messages: list[Message], *_args: object) -> None:
        if len(model.requests) == 1 and media not in messages:
            messages.append(media)

    install_request_preparation(model, marker="_test_media", prepare=add_media)

    await _run(agent, "Do the task")

    compacted = _first_compacted(model)
    assert not any(message.images for message in compacted.messages)
    assert "generated the attached media" in summary_calls.inputs[0]
    assert "MDAw" not in summary_calls.inputs[0]


@pytest.mark.asyncio
async def test_unscoped_first_request_without_foldable_messages_is_sent_as_is(
    tmp_path: Path,
    summary_calls: _SummaryCalls,
) -> None:
    config, paths = _config(tmp_path)
    model = _ToolLoopModel(tool_rounds=0)

    output = await _run(_agent(model, config, paths), [Message(role="user", content="p" * 16_000)])

    assert str(output.content) == "done"
    assert summary_calls.inputs == []
    assert model.requests[0].contents[-1] == "p" * 16_000


@pytest.mark.asyncio
async def test_run_local_summary_usage_is_recorded(tmp_path: Path, summary_calls: _SummaryCalls) -> None:
    config, paths = _config(tmp_path)
    model = _ToolLoopModel(tool_rounds=5)
    storage = SqliteDb(db_file=str(tmp_path / "usage.db"), session_table="test_agent_sessions")
    try:
        await _run(_agent(model, config, paths, db=storage), "Do the task")

        with storage.db_engine.begin() as connection:
            rows = connection.exec_driver_sql("SELECT usage_data FROM test_agent_sessions_usage").scalars().all()
    finally:
        storage.close()

    summaries = [json.loads(row) for row in rows if json.loads(row).get("kind") == "compaction_summary"]
    assert len(summaries) == len(summary_calls.inputs) >= 1
    assert all(summary["metrics"]["input_tokens"] == 7 for summary in summaries)


@pytest.mark.asyncio
async def test_resumed_run_does_not_anchor_on_its_paused_response(
    tmp_path: Path,
    summary_calls: _SummaryCalls,
) -> None:
    config, paths = _config(tmp_path, context_window=1_000_000)
    model = _ToolLoopModel(
        tool_rounds=1,
        usage=lambda number: MessageMetrics(input_tokens=999_900 if number == 1 else 10),
    )
    agent = _agent(model, config, paths, db=InMemoryDb())
    agent.tools = [Function(name="probe", entrypoint=probe, requires_confirmation=True)]

    paused = await agent.arun("Do the task", session_id="session")
    assert paused.status == RunStatus.paused
    for requirement in paused.requirements or []:
        requirement.confirm()
    await agent.acontinue_run(run_id=paused.run_id, requirements=paused.requirements, session_id="session")

    assert summary_calls.inputs == []
    assert len(model.requests) == 2


@pytest.mark.asyncio
async def test_run_local_summary_survives_approval_resume(tmp_path: Path, summary_calls: _SummaryCalls) -> None:
    config, paths = _config(tmp_path)

    class _PausingLoop(_ToolLoopModel):
        async def ainvoke(self, messages: list[Message], **kwargs: object) -> ModelResponse:
            response = await super().ainvoke(messages, **kwargs)
            if len(self.requests) == 5 and response.tool_calls:
                response.tool_calls[0]["function"]["name"] = "approve_me"
            return response

    model = _PausingLoop(tool_rounds=6)
    agent = _agent(model, config, paths, db=SqliteDb(db_file=str(tmp_path / "runs.db")))
    agent.tools = [probe, Function(name="approve_me", entrypoint=lambda: "approved", requires_confirmation=True)]

    paused = await agent.arun("Do the task", session_id="session")
    assert paused.status == RunStatus.paused
    assert summary_calls.inputs
    for requirement in paused.requirements or []:
        requirement.confirm()
    model.requests.clear()
    await agent.acontinue_run(run_id=paused.run_id, requirements=paused.requirements, session_id="session")

    resumed = model.requests[0]
    summaries = [message for message in resumed.messages if is_compaction_summary(message)]
    assert len(summaries) >= 1
    assert all(summary.from_history is False for summary in summaries)


def test_factories_install_mid_turn_compaction() -> None:
    from tests.conftest import runtime_paths_for  # noqa: PLC0415
    from tests.test_agents import _test_config  # noqa: PLC0415

    config = _test_config()
    agent = create_agent("calculator", config, runtime_paths_for(config), execution_identity=None)

    assert vars(agent.model).get("_mindroom_mid_turn_compaction_installed") is True
    assert vars(agent.model).get("_mindroom_model_call_cap_installed") is True


class _DelegatingLeader(SyntheticModel):
    """Delegate once to the member, then answer."""

    def __init__(self) -> None:
        super().__init__(id="leader-model", name="leader", provider="test")
        self.requests = 0

    async def ainvoke(self, messages: list[Message], **_kwargs: object) -> ModelResponse:  # noqa: ARG002
        self.requests += 1
        if self.requests == 1:
            return ModelResponse(
                tool_calls=[
                    {
                        "id": "delegate_1",
                        "type": "function",
                        "function": {
                            "name": "delegate_task_to_member",
                            "arguments": '{"member_id": "test_agent", "task": "Do the task"}',
                        },
                    },
                ],
            )
        return ModelResponse(content="team done")

    async def ainvoke_stream(self, messages: list[Message], **kwargs: object) -> AsyncIterator[ModelResponse]:
        yield await self.ainvoke(messages, **kwargs)


@pytest.mark.asyncio
async def test_team_member_loop_compacts_run_locally(tmp_path: Path, summary_calls: _SummaryCalls) -> None:
    config, paths = _config(tmp_path)
    member_model = _ToolLoopModel(tool_rounds=5)
    member = _agent(member_model, config, paths)
    member.name = "Test Agent"
    team = Team(id="crew", name="Crew", model=_DelegatingLeader(), members=[member], telemetry=False)

    output = await team.arun("Coordinate the task")

    assert str(output.content).endswith("team done")
    assert summary_calls.inputs
    compacted = _first_compacted(member_model)
    assert [message.from_history for message in compacted.messages if is_compaction_summary(message)] == [False]
