"""Compaction between two model requests of one turn."""
# ruff: noqa: D103

from __future__ import annotations

import asyncio
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
from agno.run.agent import RunInput, RunOutput
from agno.run.base import RunStatus
from agno.session.summary import SessionSummary
from agno.team import Team
from agno.tools.function import Function

from mindroom.agent_storage import create_session_storage, get_agent_session
from mindroom.agents import create_agent
from mindroom.agno_compat_model_hooks import install_request_preparation
from mindroom.config.models import CompactionConfig, ModelConfig
from mindroom.constants import QUEUED_MESSAGE_NOTICE_MARKER_KEY as _NOTICE_KEY
from mindroom.history import agno_compat_message_builder, mid_turn_compaction
from mindroom.history.agno_compat_message_builder import built_request_session
from mindroom.history.mid_turn_compaction import bind_compaction_lifecycle, install_mid_turn_compaction
from mindroom.history.replay import compaction_summary_message, is_compaction_summary
from mindroom.history.storage import read_scope_state, set_force_compaction_state
from mindroom.history.types import CompactionOutcome, HistoryScope, HistoryScopeState
from mindroom.native_compaction import NativeCompactionModel
from mindroom.synthetic_model import SyntheticModel
from mindroom.usage_storage import project_usage
from tests.conftest import FakeModel, seed_session
from tests.history_helpers import (
    RecordingCompactionLifecycle,
    _completed_run,
    _make_config,
    _session,
    archived_run_ids,
    compaction_generations,
)

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


_SCOPE = HistoryScope(kind="agent", scope_id="test_agent")


def _scoped_agent(
    model: _ToolLoopModel,
    config: Config,
    runtime_paths: RuntimePaths,
    storage: object,
    **kwargs: Any,  # noqa: ANN401
) -> Agent:
    agent = _agent(model, config, runtime_paths, db=storage, **kwargs)
    agent.add_history_to_context = True
    agent.num_history_runs = None
    agent.store_history_messages = False
    return agent


def _seeded_storage(config: Config, runtime_paths: RuntimePaths) -> object:
    storage = create_session_storage("test_agent", config, runtime_paths, execution_identity=None)
    seed_session(
        storage,
        _session(
            "session-1",
            runs=[
                _completed_run(
                    "run-1",
                    messages=[Message(role="user", content="q1"), Message(role="assistant", content="a1")],
                ),
                _completed_run(
                    "run-2",
                    messages=[Message(role="user", content="q2"), Message(role="assistant", content="a2")],
                ),
            ],
        ),
    )
    return storage


@pytest.mark.asyncio
@pytest.mark.usefixtures("summary_calls")
@pytest.mark.parametrize("stream", [False, True])
async def test_long_single_turn_compacts_mid_turn_and_continues(
    tmp_path: Path,
    *,
    stream: bool,
) -> None:
    config, paths = _config(tmp_path)
    storage = _seeded_storage(config, paths)
    model = _ToolLoopModel(tool_rounds=5)
    agent = _scoped_agent(model, config, paths, storage)

    output = await _run_in_session(agent, "Do the task", stream=stream)

    assert str(output.content).endswith("done")
    compacted = _first_compacted(model)
    summaries = [message for message in compacted.messages if is_compaction_summary(message)]
    assert len(summaries) == 1
    assert summaries[0].from_history is True
    assert compacted.contents[-1] == "Do the task"
    assert not {"q1", "a1", "q2", "a2"} & set(compacted.contents)
    archived = archived_run_ids(storage)
    assert archived[:2] == ["run-1", "run-2"]
    assert archived[2].startswith(f"{output.run_id}:compaction-snapshot:")
    assert output.run_id not in archived
    generations = compaction_generations(storage, _SCOPE.key)
    stored = get_agent_session(storage, "session-1")
    assert stored is not None
    assert stored.summary is not None
    assert stored.summary.summary == generations[-1].summary
    assert any(run.run_id == output.run_id for run in stored.runs or [])


@pytest.mark.asyncio
async def test_next_turn_reuses_the_post_compaction_prefix(tmp_path: Path, summary_calls: _SummaryCalls) -> None:
    config, paths = _config(tmp_path)
    storage = _seeded_storage(config, paths)
    model = _ToolLoopModel(tool_rounds=5)
    agent = _scoped_agent(model, config, paths, storage)
    await _run_in_session(agent, "Do the task")
    last_request = model.requests[-1]
    model.requests.clear()
    model.tool_rounds = 0

    await _run_in_session(agent, "Thanks")

    next_request = model.requests[0]
    assert summary_calls.inputs
    assert next_request.roles[: len(last_request.roles)] == last_request.roles
    assert next_request.contents[: len(last_request.contents)] == last_request.contents


@pytest.mark.asyncio
async def test_approval_resume_after_mid_turn_compaction(tmp_path: Path, summary_calls: _SummaryCalls) -> None:
    config, paths = _config(tmp_path)
    storage = _seeded_storage(config, paths)

    class _PausingLoop(_ToolLoopModel):
        async def ainvoke(self, messages: list[Message], **kwargs: object) -> ModelResponse:
            response = await super().ainvoke(messages, **kwargs)
            if len(self.requests) == 5 and response.tool_calls:
                response.tool_calls[0]["function"]["name"] = "approve_me"
            return response

    model = _PausingLoop(tool_rounds=6)
    agent = _scoped_agent(model, config, paths, storage)
    agent.tools = [probe, Function(name="approve_me", entrypoint=lambda: "approved", requires_confirmation=True)]

    paused = await agent.arun("Do the task", session_id="session-1")
    assert paused.status == RunStatus.paused
    assert summary_calls.inputs
    paused_request = model.requests[-1]
    for requirement in paused.requirements or []:
        requirement.confirm()
    model.requests.clear()
    await agent.acontinue_run(run_id=paused.run_id, requirements=paused.requirements, session_id="session-1")

    resumed = model.requests[0]
    summaries = [message for message in resumed.messages if is_compaction_summary(message)]
    assert len(summaries) == 1
    assert summaries[0].content == next(m.content for m in paused_request.messages if is_compaction_summary(m))
    assert resumed.contents[: len(paused_request.contents)] == paused_request.contents


@pytest.mark.asyncio
async def test_partial_commit_then_failure_leaves_the_request_unchanged(
    tmp_path: Path,
    summary_calls: _SummaryCalls,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config, paths = _make_config(
        tmp_path,
        defaults_compaction=CompactionConfig(model="summary", reserve_tokens=500),
        models={
            "default": ModelConfig(provider="openai", id="test-model", context_window=4000),
            "summary": ModelConfig(provider="openai", id="summary-model", context_window=6000),
        },
    )
    storage = create_session_storage("test_agent", config, paths, execution_identity=None)
    history = [
        _completed_run(f"run-{index}", messages=[Message(role="user", content="h" * 6000)]) for index in range(2)
    ]
    seed_session(storage, _session("session-1", runs=history))

    async def fail_second(**kwargs: Any) -> SessionSummary:  # noqa: ANN401
        if summary_calls.inputs:
            summary_calls.inputs.append(kwargs["summary_input"])
            msg = "summary model unavailable"
            raise RuntimeError(msg)
        return await summary_calls(**kwargs)

    monkeypatch.setattr("mindroom.history.compaction.generate_compaction_summary", fail_second)
    model = _ToolLoopModel(tool_rounds=4)

    output = await _run_in_session(_scoped_agent(model, config, paths, storage), "Do the task")

    assert str(output.content).endswith("done")
    assert len(summary_calls.inputs) == 2
    assert not any(is_compaction_summary(message) for request in model.requests for message in request.messages)
    stored = get_agent_session(storage, "session-1")
    assert stored is not None
    generations = compaction_generations(storage, _SCOPE.key)
    assert generations
    assert stored.summary is not None
    assert stored.summary.summary == generations[-1].summary


@pytest.mark.asyncio
async def test_force_flag_survives_mid_turn_compaction(tmp_path: Path, summary_calls: _SummaryCalls) -> None:
    config, paths = _config(tmp_path)
    storage = _seeded_storage(config, paths)
    stored = get_agent_session(storage, "session-1")
    assert stored is not None
    set_force_compaction_state(stored, _SCOPE, HistoryScopeState(), force=True)
    storage.upsert_session(stored)
    model = _ToolLoopModel(tool_rounds=5)

    await _run_in_session(_scoped_agent(model, config, paths, storage), "Do the task")

    assert summary_calls.inputs
    after = get_agent_session(storage, "session-1")
    assert after is not None
    assert read_scope_state(after, _SCOPE).force_compact_before_next_run is True


@pytest.mark.asyncio
async def test_mid_turn_compaction_posts_notices(tmp_path: Path, summary_calls: _SummaryCalls) -> None:
    config, paths = _config(tmp_path)
    storage = _seeded_storage(config, paths)
    model = _ToolLoopModel(tool_rounds=5)
    agent = _scoped_agent(model, config, paths, storage)
    lifecycle = RecordingCompactionLifecycle()
    bind_compaction_lifecycle(agent, lifecycle)

    await _run_in_session(agent, "Do the task")

    assert summary_calls.inputs
    kinds = [type(event).__name__ for event in lifecycle.events]
    assert kinds[0] == "CompactionLifecycleStart"
    assert "CompactionOutcome" in kinds


@pytest.mark.asyncio
@pytest.mark.usefixtures("summary_calls")
async def test_cancellation_after_the_snapshot_commit_still_rewrites_the_request(tmp_path: Path) -> None:
    config, paths = _config(tmp_path)
    storage = _seeded_storage(config, paths)
    model = _ToolLoopModel(tool_rounds=5)
    agent = _scoped_agent(model, config, paths, storage)

    class _CancelOnSuccess(RecordingCompactionLifecycle):
        async def complete_success(self, outcome: CompactionOutcome) -> None:  # noqa: ARG002
            raise asyncio.CancelledError

    bind_compaction_lifecycle(agent, _CancelOnSuccess())
    live_lists: list[list[Message]] = []

    async def capture(messages: list[Message], *_args: object) -> None:
        live_lists.append(messages)

    install_request_preparation(model, marker="_test_capture", prepare=capture)

    with pytest.raises(asyncio.CancelledError):
        await _run_in_session(agent, "Do the task")

    request = live_lists[-1]
    assert any(is_compaction_summary(message) for message in request)
    assert all(message.role != "tool" for message in request)
    live_session = built_request_session(agent)
    assert live_session is not None
    generations = compaction_generations(storage, _SCOPE.key)
    assert live_session.summary is not None
    assert live_session.summary.summary == generations[-1].summary


async def _run_in_session(agent: Agent, prompt: str, *, stream: bool = False) -> RunOutput:
    if not stream:
        output = await agent.arun(prompt, session_id="session-1")
        assert isinstance(output, RunOutput)
        return output
    final: RunOutput | None = None
    async for event in agent.arun(prompt, session_id="session-1", stream=True, yield_run_output=True):
        if isinstance(event, RunOutput):
            final = event
    assert final is not None
    return final


def test_legacy_resume_then_mid_turn_compaction_leaves_one_summary() -> None:
    legacy_system = Message(
        role="system",
        content=(
            "Be precise.\n\nHere is a brief summary of your previous interactions:\n\n"
            "<summary_of_previous_interactions>\nOLD\n</summary_of_previous_interactions>\n\n"
            "Note: this information is from previous interactions and may be outdated. "
            "You should ALWAYS prefer information from this conversation over the past summary.\n\n"
            "Current date: Monday"
        ),
    )
    prompt = Message(role="user", content="Do the task")
    messages = [legacy_system, prompt, Message(role="assistant", content="step"), Message(role="tool", content="r")]
    run_response = RunOutput(run_id="run", input=RunInput(input_content=[prompt]))

    layout = mid_turn_compaction._layout(messages, run_response)
    mid_turn_compaction._rewrite(messages, layout, compaction_summary_message("NEW", from_history=True))

    assert messages[0].content == "Be precise.\n\nCurrent date: Monday"
    assert [is_compaction_summary(message) for message in messages] == [False, True, False]
    assert messages[2] is prompt


class _NativeToolLoopModel(NativeCompactionModel, _ToolLoopModel):
    """A tool-loop model on a native compaction route whose provider never compacts."""

    def native_compaction_supported(self) -> bool:
        return True

    def native_compaction_endpoint(self) -> str:
        return "test-endpoint"


@pytest.mark.asyncio
async def test_native_request_under_the_limit_never_text_compacts(
    tmp_path: Path,
    summary_calls: _SummaryCalls,
) -> None:
    config, paths = _config(tmp_path, context_window=1_000_000)
    model = _NativeToolLoopModel(tool_rounds=4, usage=lambda number: MessageMetrics(input_tokens=999_900 * number))
    model.configure_native_compaction(threshold=500_000)

    await _run(_agent(model, config, paths), "Do the task")

    assert summary_calls.inputs == []
    assert model.native_compaction is not None


@pytest.mark.asyncio
async def test_oversized_native_request_turns_native_off_and_compacts_as_text(
    tmp_path: Path,
    summary_calls: _SummaryCalls,
) -> None:
    config, paths = _config(tmp_path)
    model = _NativeToolLoopModel(tool_rounds=5)
    model.configure_native_compaction(threshold=2000)

    await _run(_agent(model, config, paths), "Do the task")

    assert summary_calls.inputs
    assert model.native_compaction is None
    compacted = _first_compacted(model)
    assert compacted.contents[-1] == "Do the task"
    assert "tool" not in compacted.roles
