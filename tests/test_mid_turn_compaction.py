"""Compaction between two model requests of one turn."""
# ruff: noqa: D103

from __future__ import annotations

import asyncio
import json
import re
import threading
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any
from unittest.mock import AsyncMock

import pytest
from agno.agent import Agent
from agno.db.in_memory import InMemoryDb
from agno.db.sqlite import SqliteDb
from agno.media import File, Image
from agno.metrics import MessageMetrics
from agno.models.anthropic import Claude
from agno.models.message import Message
from agno.models.response import ModelResponse, ToolExecution
from agno.run.agent import RunInput, RunOutput
from agno.run.base import RunStatus
from agno.run.team import TeamRunOutput
from agno.session.summary import SessionSummary
from agno.team import Team
from agno.tools.function import Function

from mindroom import ai, helper_usage
from mindroom.agents import create_agent
from mindroom.agno_compat_model_hooks import install_request_preparation
from mindroom.config.agent import TeamConfig
from mindroom.config.models import CompactionConfig, ModelConfig
from mindroom.constants import MATRIX_SOURCE_EVENT_PROMPTS_METADATA_KEY
from mindroom.constants import QUEUED_MESSAGE_NOTICE_MARKER_KEY as _NOTICE_KEY
from mindroom.execution_preparation import _PreparedExecutionContext
from mindroom.helper_usage import HelperUsageOwner
from mindroom.history import agno_compat_message_builder, mid_turn_compaction
from mindroom.history.mid_turn_compaction import bind_compaction_lifecycle, install_mid_turn_compaction
from mindroom.history.replay import compaction_summary_message, estimate_request_messages_tokens, is_compaction_summary
from mindroom.history.storage import (
    new_scope_session,
    read_scope_state,
    set_force_compaction_state,
)
from mindroom.history.summary_rows import folded_tool_call_ids
from mindroom.history.turn_recorder import TurnRecorder
from mindroom.history.types import CompactionOutcome, HistoryScope, HistoryScopeState, PreparedHistoryState
from mindroom.hooks import EVENT_COMPACTION_AFTER, EVENT_COMPACTION_BEFORE, CompactionHookContext, HookRegistry, hook
from mindroom.model_usage import response_context_tokens
from mindroom.native_compaction import NativeCompactionModel, record_native_checkpoint
from mindroom.providers.anthropic_claude import MindRoomAnthropicClaude
from mindroom.providers.claude_prompt_cache import install_claude_deferred_tool_search
from mindroom.providers.openai_models import MindRoomOpenAIResponses
from mindroom.providers.openai_tool_search import install_openai_deferred_tool_search
from mindroom.providers.synthetic_model import SyntheticModel
from mindroom.session_storage.agent_storage import create_session_storage, get_agent_session, get_team_session
from mindroom.teams import TeamMode, _create_team_instance, prepare_materialized_team_execution
from mindroom.tool_system.runtime_context import tool_runtime_context
from mindroom.usage.storage import project_usage
from tests.conftest import FakeModel, make_turn_context, seed_session
from tests.history_helpers import (
    RecordingCompactionLifecycle,
    _completed_run,
    _completed_team_run,
    _hook_runtime_context,
    _make_config,
    _plugin,
    _session,
    _team_session,
    folded_run_ids,
    log_of,
    seed_summary,
    segments_of,
    summary_rows_of,
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
        reasoning: str | None = None,
    ) -> None:
        super().__init__(id="test-model", name="test", provider="test")
        self.tool_rounds = tool_rounds
        self.calls_per_round = calls_per_round
        self.usage = usage
        self.reasoning = reasoning
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
                reasoning_content=self.reasoning,
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
    cancel: bool = False

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
        if self.cancel:
            raise asyncio.CancelledError
        if self.fail:
            msg = "summary model unavailable"
            raise RuntimeError(msg)
        return SessionSummary(summary=f"SUMMARY-{len(self.inputs)}", updated_at=datetime.now(UTC))


@pytest.fixture
def summary_calls(monkeypatch: pytest.MonkeyPatch) -> _SummaryCalls:
    calls = _SummaryCalls()
    monkeypatch.setattr("mindroom.history.compaction.generate_compaction_summary", calls)
    monkeypatch.setattr(
        "mindroom.providers.model_loading.get_model_instance",
        lambda *_args, **_kwargs: FakeModel(id="summary-model", provider="fake"),
    )
    return calls


def _config(
    tmp_path: Path,
    *,
    enabled: bool = True,
    context_window: int | None = 4000,
    replay_window_tokens: int | None = None,
    summary_context_window: int = 100_000,
    teams: dict[str, TeamConfig] | None = None,
) -> tuple[Config, RuntimePaths]:
    return _make_config(
        tmp_path,
        teams=teams,
        defaults_compaction=CompactionConfig(
            enabled=enabled,
            model="summary",
            reserve_tokens=500,
            replay_window_tokens=replay_window_tokens,
        ),
        models={
            "default": ModelConfig(provider="openai", id="test-model", context_window=context_window),
            "summary": ModelConfig(provider="openai", id="summary-model", context_window=summary_context_window),
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
@pytest.mark.parametrize("stream", [False, True])
async def test_repeated_run_local_compaction_keeps_a_string_prompt(
    tmp_path: Path,
    summary_calls: _SummaryCalls,
    *,
    stream: bool,
) -> None:
    config, paths = _config(tmp_path)
    model = _ToolLoopModel(tool_rounds=12)

    output = await _run(_agent(model, config, paths), "Do the task", stream=stream)

    assert str(output.content).endswith("done")
    assert len(summary_calls.inputs) >= 2
    compacted = [request for request in model.requests if any(is_compaction_summary(m) for m in request.messages)]
    for request in compacted:
        assert request.contents.count("Do the task") == 1
        assert sum(is_compaction_summary(message) for message in request.messages) == 1


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
@pytest.mark.parametrize("input_tokens", [0, 10])
async def test_request_sizing_counts_the_turns_thinking(
    tmp_path: Path,
    summary_calls: _SummaryCalls,
    input_tokens: int,
) -> None:
    config, paths = _config(tmp_path, context_window=20_000)
    model = _ToolLoopModel(
        tool_rounds=2,
        usage=lambda _number: MessageMetrics(input_tokens=input_tokens),
        reasoning="t" * 100_000,
    )

    await _run(_agent(model, config, paths), "Do the task")

    assert summary_calls.inputs
    assert any(is_compaction_summary(message) for message in model.requests[1].messages)


@pytest.mark.asyncio
async def test_request_sizing_runs_off_the_event_loop(
    tmp_path: Path,
    summary_calls: _SummaryCalls,  # noqa: ARG001
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config, paths = _config(tmp_path)
    loop_thread = threading.get_ident()
    sizing_threads: list[int] = []
    original = mid_turn_compaction.estimate_request_messages_tokens

    def recording(*args: Any, **kwargs: Any) -> int:  # noqa: ANN401
        sizing_threads.append(threading.get_ident())
        return original(*args, **kwargs)

    monkeypatch.setattr(mid_turn_compaction, "estimate_request_messages_tokens", recording)

    await _run(_agent(_ToolLoopModel(tool_rounds=5), config, paths), "Do the task")

    assert sizing_threads
    assert loop_thread not in sizing_threads


def test_request_sizing_never_counts_attached_file_bytes() -> None:
    """A provider estimator must not count an attachment's encoded bytes as prompt text."""
    model = MindRoomOpenAIResponses(id="gpt-6-luna", store=False)
    pdf = File(content=b"%PDF-1.7\n" + b"x" * 300_000, mime_type="application/pdf", filename="report.pdf")
    message = Message(role="user", content="Summarize the attached report.", files=[pdf])

    tokens = estimate_request_messages_tokens([message], replay_model=model)

    assert tokens < 10_000


@pytest.mark.parametrize("provider", ["openai", "claude"])
def test_request_sizing_leaves_out_deferred_tool_definitions(provider: str) -> None:
    """Tool search keeps deferred definitions out of the context until the model loads them."""
    if provider == "openai":
        model: Any = MindRoomOpenAIResponses(id="gpt-6-luna", store=False)
        install_openai_deferred_tool_search(model, deferred_tool_names=frozenset({"big_tool"}))
    else:
        model = MindRoomAnthropicClaude(id="claude-sonnet-5-5", api_key="test")
        install_claude_deferred_tool_search(model, deferred_tool_names=frozenset({"big_tool"}))
    tools = [
        {"type": "function", "function": {"name": "big_tool", "description": "d" * 80_000, "parameters": {}}},
        {"type": "function", "function": {"name": "lookup", "description": "Look up a value.", "parameters": {}}},
    ]

    assert mid_turn_compaction._tool_tokens(model, tools, []) < 1_000
    assert mid_turn_compaction._tool_tokens(model, tools, []) > 0


@pytest.mark.parametrize("provider", ["openai", "claude"])
def test_request_sizing_counts_deferred_tool_definitions_once_tool_search_loads_them(provider: str) -> None:
    """A deferred definition enters the context once when a tool search in the request returns it.

    OpenAI replays the loaded definitions inside the search output, which the message estimate counts; Claude
    replays only references that the provider expands.
    """
    big_tool = {"type": "function", "function": {"name": "big_tool", "description": "d" * 80_000, "parameters": {}}}
    if provider == "openai":
        model: Any = MindRoomOpenAIResponses(id="gpt-6-luna", store=False)
        install_openai_deferred_tool_search(model, deferred_tool_names=frozenset({"big_tool"}))
        provider_data: dict[str, Any] = {
            "tool_search_items": [
                {
                    "type": "tool_search_call",
                    "id": "ts_1",
                    "call_id": "search_1",
                    "execution": "server",
                    "arguments": {"query": "big"},
                    "status": "completed",
                },
                {
                    "type": "tool_search_output",
                    "id": "tso_1",
                    "call_id": "search_1",
                    "execution": "server",
                    "status": "completed",
                    "tools": [{"type": "function", **big_tool["function"]}],
                },
            ],
        }
    else:
        model = MindRoomAnthropicClaude(id="claude-sonnet-5-5", api_key="test")
        install_claude_deferred_tool_search(model, deferred_tool_names=frozenset({"big_tool"}))
        provider_data = {
            "server_tool_blocks": [
                {"type": "server_tool_use", "id": "srv_1", "name": "tool_search_tool_regex", "input": {"query": "big"}},
                {
                    "type": "tool_search_tool_result",
                    "tool_use_id": "srv_1",
                    "content": {
                        "type": "tool_search_tool_search_result",
                        "tool_references": [{"type": "tool_reference", "tool_name": "big_tool"}],
                    },
                },
            ],
        }
    searched = [
        Message(role="user", content="Use the big tool"),
        Message(role="assistant", provider_data=provider_data),
    ]

    total = estimate_request_messages_tokens(searched, replay_model=model) + mid_turn_compaction._tool_tokens(
        model,
        [big_tool],
        searched,
    )

    assert 15_000 < total < 30_000


def test_aggregate_usage_of_retried_requests_never_anchors_sizing() -> None:
    """A response whose counters sum several retried attempts describes no single request's context."""
    message = Message(
        role="assistant",
        content="done",
        metrics=MessageMetrics(input_tokens=90_000),
        provider_data={"mindroom_aggregate_usage": True},
    )

    assert (
        response_context_tokens(message, provider="openai", configured_provider="openai", model_id="gpt-6-luna") is None
    )


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
async def test_rewritten_request_within_the_model_window_is_sent(
    tmp_path: Path,
    summary_calls: _SummaryCalls,
) -> None:
    config, paths = _config(tmp_path, context_window=100_000, replay_window_tokens=4000)
    model = _ToolLoopModel(tool_rounds=5)

    output = await _run(_agent(model, config, paths), [Message(role="user", content="p" * 16_000)])

    assert str(output.content).endswith("done")
    # The prompt alone exceeds the replay window, so compacting again would only summarize every new tool result.
    assert len(summary_calls.inputs) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("stream", [False, True])
async def test_turn_larger_than_one_summary_input_folds_whole_in_chunks(
    tmp_path: Path,
    summary_calls: _SummaryCalls,
    monkeypatch: pytest.MonkeyPatch,
    *,
    stream: bool,
) -> None:
    # A Claude summarizer sizes its input by bytes, more conservatively than request estimates.
    monkeypatch.setattr(
        "mindroom.providers.model_loading.get_model_instance",
        lambda *_args, **_kwargs: Claude(id="claude-haiku-5-5", api_key="test"),
    )
    config, paths = _config(tmp_path, context_window=20_000, summary_context_window=8_000)
    model = _ToolLoopModel(tool_rounds=25)
    facts = iter(range(100))

    def probe() -> str:
        """Return one tool result carrying a distinct fact."""
        return f"FACT-{next(facts):02d} " + "r" * 4_000

    agent = _agent(model, config, paths)
    agent.tools = [probe]

    output = await _run(agent, "Do the task", stream=stream)

    assert str(output.content).endswith("done")
    compacted = _first_compacted(model)
    assert "tool" not in compacted.roles
    assert "assistant" not in compacted.roles
    summary_inputs = "\n".join(summary_calls.inputs)
    assert "Run truncated to fit compaction budget." not in summary_inputs
    seen = set(re.findall(r"FACT-\d\d", " ".join(model.requests[model.requests.index(compacted) - 1].contents)))
    assert len(summary_calls.inputs) >= 2
    assert seen
    assert all(fact in summary_inputs for fact in seen)


@pytest.mark.asyncio
async def test_a_parallel_tool_batch_larger_than_one_summary_input_folds_without_truncation(
    tmp_path: Path,
    summary_calls: _SummaryCalls,
) -> None:
    """A batch is kept whole in the request, but its results reach the summarizer across chunks, none cut short."""
    config, paths = _config(tmp_path, context_window=8_000, summary_context_window=6_000)

    class _PathLoop(_ToolLoopModel):
        async def ainvoke(self, messages: list[Message], **kwargs: object) -> ModelResponse:
            response = await super().ainvoke(messages, **kwargs)
            for call in response.tool_calls or []:
                call["function"]["arguments"] = json.dumps({"path": f"{call['id']}.txt"})
            return response

    model = _PathLoop(tool_rounds=2, calls_per_round=4)
    facts = iter(range(100))
    fact_paths: dict[str, str] = {}

    def probe(path: str) -> str:
        """Return one tool result carrying a distinct fact at its end."""
        fact = f"FACT-{next(facts):02d}"
        fact_paths[fact] = path
        return "r" * 4_000 + f" {fact}"

    agent = _agent(model, config, paths)
    agent.tools = [probe]

    output = await _run(agent, "Do the task")

    assert str(output.content).endswith("done")
    summary_inputs = "\n".join(summary_calls.inputs)
    assert "Run truncated to fit compaction budget." not in summary_inputs
    assert len(fact_paths) == 8
    # Each result names its own call's arguments, so the summary can tell which file it came from.
    for fact, path in fact_paths.items():
        assert any(fact in summary_input and path in summary_input for summary_input in summary_calls.inputs), fact


@pytest.mark.asyncio
@pytest.mark.parametrize("scoped", [False, True])
async def test_a_prompt_larger_than_the_summary_input_keeps_the_first_exchange_in_the_summary(
    tmp_path: Path,
    summary_calls: _SummaryCalls,
    *,
    scoped: bool,
) -> None:
    config, paths = _config(tmp_path, context_window=60_000, summary_context_window=8_000)
    model = _ToolLoopModel(tool_rounds=60)
    facts = iter(range(100))

    def probe() -> str:
        """Return one tool result carrying a distinct fact."""
        return f"FACT-{next(facts):02d} " + "r" * 4_000

    if scoped:
        storage = create_session_storage("test_agent", config, paths, execution_identity=None)
        seed_session(storage, _session("session-1"))
        agent = _scoped_agent(model, config, paths, storage)
    else:
        agent = _agent(model, config, paths)
    agent.tools = [probe]
    # The prompt fits the model's window but not one summary input.
    prompt = "Do the task. " + "p" * 40_000
    # A Matrix turn also records its source prompts in the run's metadata.
    metadata = {MATRIX_SOURCE_EVENT_PROMPTS_METADATA_KEY: {"$prompt": prompt}}

    output = await agent.arun(prompt, session_id="session-1" if scoped else None, metadata=metadata)

    assert str(output.content).endswith("done")
    assert summary_calls.inputs
    assert "FACT-00" in "\n".join(summary_calls.inputs)


@pytest.mark.asyncio
async def test_an_exchange_larger_than_the_summary_input_still_folds(
    tmp_path: Path,
    summary_calls: _SummaryCalls,
) -> None:
    config, paths = _config(tmp_path, context_window=20_000, summary_context_window=6_000)
    model = _ToolLoopModel(tool_rounds=8)
    sizes = iter([4_000] * 6 + [72_000] + [4_000] * 10)

    def probe() -> str:
        """Return one tool result whose size varies by call."""
        return "r" * next(sizes)

    agent = _agent(model, config, paths)
    agent.tools = [probe]

    output = await _run(agent, "Do the task")

    assert str(output.content).endswith("done")
    assert summary_calls.inputs
    assert "tool" not in _first_compacted(model).roles


@pytest.mark.asyncio
async def test_request_sizing_counts_thinking_the_response_did_not_show(
    tmp_path: Path,
    summary_calls: _SummaryCalls,
) -> None:
    config, paths = _config(tmp_path, context_window=20_000)
    model = _ToolLoopModel(tool_rounds=2, usage=lambda _number: MessageMetrics(input_tokens=10, output_tokens=30_000))

    await _run(_agent(model, config, paths), "Do the task")

    assert summary_calls.inputs
    assert any(is_compaction_summary(message) for message in model.requests[1].messages)


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
async def test_folded_request_usage_survives_approval_resume(tmp_path: Path, summary_calls: _SummaryCalls) -> None:
    config, paths = _config(tmp_path)

    class _PausingLoop(_ToolLoopModel):
        async def ainvoke(self, messages: list[Message], **kwargs: object) -> ModelResponse:
            response = await super().ainvoke(messages, **kwargs)
            if len(self.requests) == 5 and response.tool_calls:
                response.tool_calls[0]["function"]["name"] = "approve_me"
            return response

    model = _PausingLoop(
        tool_rounds=9,
        usage=lambda number: MessageMetrics(input_tokens=1001 * number, output_tokens=1),
    )
    agent = _agent(model, config, paths, db=SqliteDb(db_file=str(tmp_path / "runs.db")))
    agent.tools = [probe, Function(name="approve_me", entrypoint=lambda: "approved", requires_confirmation=True)]
    paused = await agent.arun("Do the task", session_id="session")
    for requirement in paused.requirements or []:
        requirement.confirm()

    output = await agent.acontinue_run(run_id=paused.run_id, requirements=paused.requirements, session_id="session")

    assert len(summary_calls.inputs) >= 2
    requests = project_usage(output.to_dict())["requests"]
    assert [request["metrics"]["input_tokens"] for request in requests] == [
        1001 * number for number in range(1, len(model.requests) + 1)
    ]


@pytest.mark.asyncio
@pytest.mark.usefixtures("summary_calls")
async def test_folded_request_usage_stays_on_a_run_cancelled_after_compaction(tmp_path: Path) -> None:
    config, paths = _config(tmp_path)
    storage = _seeded_storage(config, paths)
    model = _ToolLoopModel(
        tool_rounds=5,
        usage=lambda number: MessageMetrics(input_tokens=1001 * number, output_tokens=1),
    )
    agent = _scoped_agent(model, config, paths, storage)

    class _CancelOnSuccess(RecordingCompactionLifecycle):
        async def complete_success(self, outcome: CompactionOutcome) -> None:  # noqa: ARG002
            raise asyncio.CancelledError

    bind_compaction_lifecycle(agent, _CancelOnSuccess())
    runs: list[RunOutput] = []

    async def capture(_messages: list[Message], _tools: object, run_response: RunOutput | None) -> None:
        if run_response is not None:
            runs.append(run_response)

    install_request_preparation(model, marker="_test_capture", prepare=capture)

    with pytest.raises(asyncio.CancelledError):
        await _run_in_session(agent, "Do the task")

    requests = project_usage(runs[-1].to_dict())["requests"]
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


def test_replayed_queued_notice_is_folded() -> None:
    old_notice = Message(
        role="user",
        content="A newer message is waiting.",
        provider_data={_NOTICE_KEY: "persisted"},
        from_history=True,
    )
    prompt = Message(role="user", content="Do the task")
    messages = [
        Message(role="system", content="Be precise."),
        old_notice,
        prompt,
        Message(role="assistant", content="step"),
        Message(role="tool", content="r"),
    ]
    run_response = RunOutput(run_id="run", input=RunInput(input_content=[prompt]))

    layout = mid_turn_compaction._layout(messages, run_response, scoped=True)

    assert layout.kept == (prompt,)
    assert old_notice in layout.history


def test_tool_results_after_an_interleaved_receipt_fold_with_their_call() -> None:
    prompt = Message(role="user", content="Do the task")
    call = Message(
        role="assistant",
        tool_calls=[{"id": "c1", "type": "function", "function": {"name": "probe", "arguments": "{}"}}],
    )
    # An approval receipt sits between a paused call and its approved result.
    receipt = Message(role="system", content="Approved.", add_to_agent_memory=False)
    result = Message(role="tool", tool_call_id="c1", content="r")
    messages = [Message(role="system", content="Be precise."), prompt, call, receipt, result]
    run_response = RunOutput(run_id="run", input=RunInput(input_content=[prompt]))

    layout = mid_turn_compaction._layout(messages, run_response, scoped=False)

    assert layout.exchanges == ((call, result),)
    assert layout.kept == (prompt, receipt)


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
async def test_run_local_summary_usage_goes_to_the_replys_usage_owner(
    tmp_path: Path,
    summary_calls: _SummaryCalls,
) -> None:
    config, paths = _config(tmp_path)
    db_file = str(tmp_path / "owner.db")

    def owner_storage() -> SqliteDb:
        return SqliteDb(db_file=db_file, session_table="owner_sessions")

    owner = HelperUsageOwner(
        storage_factory=owner_storage,
        session_id="owner-session",
        initial_session=new_scope_session(session_id="owner-session", scope_id="crew", is_team=True),
    )
    token = helper_usage._HELPER_USAGE_OWNER.set(owner)
    try:
        # A team member keeps no storage of its own; the reply's owner records its summary usage.
        await _run(_agent(_ToolLoopModel(tool_rounds=5), config, paths), "Do the task")
    finally:
        helper_usage._HELPER_USAGE_OWNER.reset(token)

    storage = owner_storage()
    try:
        with storage.db_engine.begin() as connection:
            rows = connection.exec_driver_sql("SELECT session_id, usage_data FROM owner_sessions_usage").all()
    finally:
        storage.close()
    summaries = [
        (session_id, json.loads(data))
        for session_id, data in rows
        if json.loads(data).get("kind") == "compaction_summary"
    ]
    assert len(summaries) == len(summary_calls.inputs) >= 1
    assert {session_id for session_id, _ in summaries} == {"owner-session"}


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


@pytest.mark.asyncio
async def test_scoped_compaction_after_resume_keeps_the_run_local_summary(
    tmp_path: Path,
    summary_calls: _SummaryCalls,
) -> None:
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
    local_summaries = len(summary_calls.inputs)
    assert local_summaries
    for requirement in paused.requirements or []:
        requirement.confirm()

    # The resumed request replays stored history, so its next compaction is scoped.
    agent.add_history_to_context = True
    model.requests.clear()
    await agent.acontinue_run(run_id=paused.run_id, requirements=paused.requirements, session_id="session")

    assert len(summary_calls.inputs) > local_summaries
    assert f"SUMMARY-{local_summaries}" in summary_calls.inputs[local_summaries]


def _factory_target(kind: str, config: Config, paths: RuntimePaths) -> Agent | Team:
    if kind == "agent":
        target: Agent | Team = create_agent("test_agent", config, paths, execution_identity=None)
    else:
        target = _create_team_instance(
            agents=[],
            mode=TeamMode.COORDINATE,
            config=config,
            runtime_paths=paths,
            team_display_name="Test Team",
            scope_context=None,
            execution_identity=None,
            model_name="default",
            configured_team_name="test_team",
        )
    target.tools = [probe]
    return target


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["agent", "team"])
async def test_factories_cap_a_request_before_compacting_it(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    summary_calls: _SummaryCalls,
    kind: str,
) -> None:
    config, paths = _config(
        tmp_path,
        context_window=12_000,
        teams={"test_team": TeamConfig(display_name="Test Team", role="Test team", agents=["test_agent"])},
    )
    # The fourth response reports a request far over the window, so only the fifth request would compact.
    model = _ToolLoopModel(
        tool_rounds=8,
        usage=lambda number: MessageMetrics(input_tokens=1_000_000 if number == 4 else 10, output_tokens=1),
    )

    def load_model(_config: Config, _paths: RuntimePaths, model_name: str = "default", **_kwargs: object) -> object:
        return model if model_name == "default" else FakeModel(id="summary-model", provider="fake")

    monkeypatch.setattr("mindroom.providers.model_loading.get_model_instance", load_model)
    target = _factory_target(kind, config, paths)
    # The cap allows the limit plus two requests, so it refuses the fifth.
    target.tool_call_limit = 2

    await target.arun("Do the task")

    assert len(model.requests) == 4
    assert summary_calls.inputs == []


@pytest.mark.asyncio
async def test_prepared_agent_reply_posts_mid_turn_compaction_notices(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    summary_calls: _SummaryCalls,
) -> None:
    config, paths = _config(tmp_path)
    storage = _seeded_storage(config, paths)
    agent = _scoped_agent(_ToolLoopModel(tool_rounds=5), config, paths, storage)
    prepared = ai._PreparedAgentRun(
        agent=agent,
        messages=(Message(role="user", content="Do the task"),),
        unseen_event_ids=[],
        prepared_history=PreparedHistoryState(),
        runtime_model_name="default",
    )
    monkeypatch.setattr(ai, "_prepare_agent_and_prompt", AsyncMock(return_value=prepared))
    lifecycle = RecordingCompactionLifecycle()
    recorder = TurnRecorder(user_message="Do the task")

    await ai._prepare_agent_run_context(
        make_turn_context("test_agent", session_id="session-1"),
        prompt="Do the task",
        session_id="session-1",
        runtime_paths=paths,
        config=config,
        scope_context=None,
        thread_history=None,
        knowledge=None,
        include_interactive_questions=False,
        tool_function_filter=None,
        include_openai_compat_guidance=False,
        execution_identity=None,
        compaction_lifecycle=lifecycle,
        delegation_depth=0,
        refresh_scheduler=None,
        model_prompt=None,
        current_timestamp_ms=None,
        current_event_id=None,
        current_prompt_is_structured=False,
        turn_recorder=recorder,
        pipeline_timing=None,
    )
    await _run_in_session(agent, "Do the task")

    assert summary_calls.inputs
    assert any(isinstance(event, CompactionOutcome) for event in lifecycle.events)
    # An interrupted record of this run would leave out the calls the fold summarized.
    assert "call_1_0" in recorder.folded_tool_call_ids


@pytest.mark.asyncio
async def test_prepared_team_reply_posts_mid_turn_compaction_notices(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    summary_calls: _SummaryCalls,
) -> None:
    config, paths = _config(
        tmp_path,
        teams={"test_team": TeamConfig(display_name="Test Team", role="Test team", agents=["test_agent"])},
    )
    storage = create_session_storage("test_agent", config, paths, execution_identity=None)
    seed_session(
        storage,
        _team_session(
            "session-1",
            team_id="test_team",
            runs=[_completed_team_run("run-1", team_id="test_team"), _completed_team_run("run-2", team_id="test_team")],
        ),
    )
    team = Team(
        id="test_team",
        name="test_team",
        model=_ToolLoopModel(tool_rounds=5),
        members=[],
        tools=[probe],
        db=storage,
        add_history_to_context=True,
        store_history_messages=False,
        add_session_summary_to_context=False,
        telemetry=False,
    )
    install_mid_turn_compaction(team, config=config, runtime_paths=paths, entity_name="test_team", model_name="default")
    prepared = _PreparedExecutionContext(
        messages=(Message(role="user", content="Do the task"),),
        unseen_event_ids=[],
        prepared_history=PreparedHistoryState(),
    )
    monkeypatch.setattr("mindroom.teams.prepare_bound_team_run_context", AsyncMock(return_value=prepared))
    lifecycle = RecordingCompactionLifecycle()
    recorder = TurnRecorder(user_message="Do the task")

    await prepare_materialized_team_execution(
        make_turn_context("test_team", session_id="session-1"),
        scope_context=None,
        agents=[],
        team=team,
        message="Do the task",
        thread_history=[],
        config=config,
        runtime_paths=paths,
        runtime_model=config.resolve_runtime_model(entity_name="test_team"),
        response_sender_id=None,
        current_sender_id=None,
        configured_team_name="test_team",
        compaction_lifecycle=lifecycle,
        turn_recorder=recorder,
    )
    await team.arun("Do the task", session_id="session-1")

    assert summary_calls.inputs
    assert any(isinstance(event, CompactionOutcome) for event in lifecycle.events)
    assert "call_1_0" in recorder.folded_tool_call_ids


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
    # Nothing moved: the fold's rows sort after the stored runs they cover, and the run after them.
    rows = summary_rows_of(storage, _SCOPE.key)
    assert log_of(storage, _SCOPE.key) == [
        "run:run-1",
        "run:run-2",
        *(f"row:{row.summary}" for row in rows),
        f"run:{output.run_id}",
    ]
    assert [run_id for run_id, _segment in segments_of(storage, _SCOPE.key)] == [output.run_id] * len(rows)
    stored = get_agent_session(storage, "session-1")
    assert stored is not None
    assert stored.summary is not None
    assert stored.summary.summary == rows[-1].summary
    assert [run.run_id for run in stored.runs or []] == [output.run_id]


@pytest.mark.asyncio
@pytest.mark.usefixtures("summary_calls")
@pytest.mark.parametrize("stream", [False, True])
async def test_long_single_team_turn_compacts_mid_turn_and_continues(
    tmp_path: Path,
    *,
    stream: bool,
) -> None:
    config, paths = _config(
        tmp_path,
        teams={"test_team": TeamConfig(display_name="Test Team", role="Test team", agents=["test_agent"])},
    )
    storage = create_session_storage("test_agent", config, paths, execution_identity=None)
    seed_session(
        storage,
        _team_session(
            "session-1",
            team_id="test_team",
            runs=[_completed_team_run("run-1", team_id="test_team"), _completed_team_run("run-2", team_id="test_team")],
        ),
    )
    model = _ToolLoopModel(tool_rounds=5)
    team = Team(
        id="test_team",
        name="test_team",
        model=model,
        members=[],
        tools=[probe],
        db=storage,
        add_history_to_context=True,
        store_history_messages=False,
        add_session_summary_to_context=False,
        telemetry=False,
    )
    install_mid_turn_compaction(team, config=config, runtime_paths=paths, entity_name="test_team", model_name="default")

    if stream:
        outputs = [
            event
            async for event in team.arun("Do the task", session_id="session-1", stream=True, yield_run_output=True)
            if isinstance(event, TeamRunOutput)
        ]
        output = outputs[-1]
    else:
        output = await team.arun("Do the task", session_id="session-1")
    assert isinstance(output, TeamRunOutput)

    assert str(output.content).endswith("done")
    compacted = _first_compacted(model)
    summaries = [message for message in compacted.messages if is_compaction_summary(message)]
    assert len(summaries) == 1
    assert summaries[0].from_history is True
    assert compacted.contents[-1] == "Do the task"
    assert not any("team question" in content or "team answer" in content for content in compacted.contents)
    team_scope = HistoryScope(kind="team", scope_id="test_team")
    rows = summary_rows_of(storage, team_scope.key)
    assert log_of(storage, team_scope.key) == [
        "run:run-1",
        "run:run-2",
        *(f"row:{row.summary}" for row in rows),
        f"run:{output.run_id}",
    ]
    assert {run_id for run_id, _segment in segments_of(storage, team_scope.key)} == {output.run_id}
    stored = get_team_session(storage, "session-1")
    assert stored is not None
    assert stored.summary is not None
    assert stored.summary.summary == rows[-1].summary


class _DelegateThenLoop(_ToolLoopModel):
    """Delegate to the member first, then run the tool loop."""

    async def ainvoke(self, messages: list[Message], **kwargs: object) -> ModelResponse:
        response = await super().ainvoke(messages, **kwargs)
        if len(self.requests) == 1:
            response.tool_calls = [
                {
                    "id": "delegate_1",
                    "type": "function",
                    "function": {
                        "name": "delegate_task_to_member",
                        "arguments": '{"member_id": "helper", "task": "Do the subtask"}',
                    },
                },
            ]
        return response


@pytest.mark.asyncio
async def test_scoped_team_compaction_keeps_the_turns_member_runs(
    tmp_path: Path,
    summary_calls: _SummaryCalls,
) -> None:
    config, paths = _config(
        tmp_path,
        teams={"test_team": TeamConfig(display_name="Test Team", role="Test team", agents=["test_agent"])},
    )
    storage = create_session_storage("test_agent", config, paths, execution_identity=None)
    member = Agent(id="helper", name="helper", model=_ToolLoopModel(tool_rounds=0), telemetry=False)
    team = Team(
        id="test_team",
        name="test_team",
        model=_DelegateThenLoop(tool_rounds=6),
        members=[member],
        tools=[probe],
        db=storage,
        add_history_to_context=True,
        store_history_messages=False,
        add_session_summary_to_context=False,
        telemetry=False,
    )
    install_mid_turn_compaction(team, config=config, runtime_paths=paths, entity_name="test_team", model_name="default")

    output = await team.arun("Do the task", session_id="session-1")

    assert isinstance(output, TeamRunOutput)
    assert str(output.content).endswith("done")
    assert summary_calls.inputs
    stored = get_team_session(storage, "session-1")
    assert stored is not None
    assert any(run.parent_run_id == output.run_id for run in stored.runs or [])


@pytest.mark.asyncio
async def test_request_with_only_the_summary_to_fold_is_sent_as_is(
    tmp_path: Path,
    summary_calls: _SummaryCalls,
) -> None:
    config, paths = _config(tmp_path)
    storage = create_session_storage("test_agent", config, paths, execution_identity=None)
    session = _session("session-1")
    seed_session(storage, session)
    seed_summary(storage, session, _SCOPE, "EARLIER-WORK")
    model = _ToolLoopModel(tool_rounds=0)

    output = await _run_in_session(_scoped_agent(model, config, paths, storage), "p" * 16_000)

    assert str(output.content) == "done"
    assert summary_calls.inputs == []
    assert any("EARLIER-WORK" in content for content in model.requests[0].contents)


@pytest.mark.asyncio
async def test_a_first_turn_compacts_mid_turn_before_its_session_row_exists(
    tmp_path: Path,
    summary_calls: _SummaryCalls,
) -> None:
    config, paths = _config(tmp_path)
    storage = create_session_storage("test_agent", config, paths, execution_identity=None)

    output = await _run_in_session(_scoped_agent(_ToolLoopModel(tool_rounds=5), config, paths, storage), "Do the task")

    assert summary_calls.inputs
    rows = summary_rows_of(storage, _SCOPE.key)
    assert rows
    assert log_of(storage, _SCOPE.key) == [*(f"row:{row.summary}" for row in rows), f"run:{output.run_id}"]
    assert {run_id for run_id, _segment in segments_of(storage, _SCOPE.key)} == {output.run_id}


@pytest.mark.asyncio
async def test_a_second_fold_of_the_turn_covers_no_run_twice(tmp_path: Path, summary_calls: _SummaryCalls) -> None:
    config, paths = _config(tmp_path)
    storage = _seeded_storage(config, paths)

    await _run_in_session(_scoped_agent(_ToolLoopModel(tool_rounds=12), config, paths, storage), "Do the task")

    rows = summary_rows_of(storage, _SCOPE.key)
    assert len(rows) >= 2
    # Every later fold sorts with the first, after the stored runs, and covers none of them again.
    assert log_of(storage, _SCOPE.key)[: 2 + len(rows)] == [
        "run:run-1",
        "run:run-2",
        *(f"row:{row.summary}" for row in rows),
    ]
    assert len(summary_calls.inputs) == len(rows)
    assert "q1" not in summary_calls.inputs[-1]


@pytest.mark.asyncio
async def test_the_turns_metadata_reaches_the_summary_once(
    tmp_path: Path,
    summary_calls: _SummaryCalls,
) -> None:
    config, paths = _config(tmp_path)
    storage = _seeded_storage(config, paths)
    agent = _scoped_agent(_ToolLoopModel(tool_rounds=5), config, paths, storage)

    await agent.arun("Do the task", session_id="session-1", metadata={"matrix_seen_event_ids": ["$x"]})

    snapshots = [run for run in summary_calls.inputs[0].split("<run ") if ":fold:" in run]
    assert len(snapshots) >= 2
    assert sum("<run_metadata>" in run for run in snapshots) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("earlier_summary", [None, "OLD-SUMMARY"])
async def test_first_request_over_the_limit_from_tools_compacts_history_before_dropping_it(
    tmp_path: Path,
    summary_calls: _SummaryCalls,
    earlier_summary: str | None,
) -> None:
    config, paths = _config(tmp_path)
    storage = create_session_storage("test_agent", config, paths, execution_identity=None)
    history = [
        _completed_run(
            f"run-{index}",
            messages=[
                Message(role="user", content=f"q{index} " + "h" * 1200),
                Message(role="assistant", content=f"a{index}"),
            ],
        )
        for index in range(4)
    ]
    session = _session("session-1", runs=history)
    seed_session(storage, session)
    if earlier_summary is not None:
        seed_summary(storage, session, _SCOPE, earlier_summary)
    model = _ToolLoopModel(tool_rounds=0)
    agent = _scoped_agent(model, config, paths, storage)
    # Tool definitions alone push the first request, which has no exchanges of its own yet, over the limit.
    agent.tools = [Function(name="lookup", description="d" * 10_000, entrypoint=lambda: "ok")]

    output = await _run_in_session(agent, "Do the task")

    assert str(output.content) == "done"
    first = model.requests[0]
    assert not any(content.startswith("q0 ") for content in first.contents)
    # The history left the request only because a summary now covers it.
    assert summary_calls.inputs
    assert any(is_compaction_summary(message) for message in first.messages)
    assert folded_run_ids(storage) == [f"run-{index}" for index in range(4)]
    if earlier_summary is not None:
        assert earlier_summary in summary_calls.inputs[0]


@pytest.mark.asyncio
async def test_run_local_compaction_summarizes_replayed_history_it_folds(
    tmp_path: Path,
    summary_calls: _SummaryCalls,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config, paths = _config(tmp_path)
    storage = _seeded_storage(config, paths)
    model = _ToolLoopModel(tool_rounds=5)
    agent = _scoped_agent(model, config, paths, storage)
    # A request that replays history but cannot update its stored session compacts run-locally.
    monkeypatch.setattr(mid_turn_compaction, "_scoped_session", lambda *_args: None)

    output = await _run_in_session(agent, "Do the task")

    assert str(output.content).endswith("done")
    assert summary_calls.inputs
    assert "q1" in summary_calls.inputs[0]
    assert "q2" in summary_calls.inputs[0]


@pytest.mark.asyncio
async def test_next_turn_reuses_the_post_compaction_prefix(tmp_path: Path, summary_calls: _SummaryCalls) -> None:
    config, paths = _config(tmp_path)
    storage = _seeded_storage(config, paths)
    model = _ToolLoopModel(tool_rounds=5)
    agent = _scoped_agent(model, config, paths, storage)
    await _run_in_session(agent, "Do the task")
    last_request = model.requests[-1]
    calls_after_first_turn = len(summary_calls.inputs)
    model.requests.clear()
    model.tool_rounds = 0

    await _run_in_session(agent, "Thanks")

    next_request = model.requests[0]
    assert calls_after_first_turn
    assert len(summary_calls.inputs) == calls_after_first_turn
    assert next_request.roles[: len(last_request.roles)] == last_request.roles
    assert next_request.contents[: len(last_request.contents)] == last_request.contents
    assert not {"q1", "a1", "q2", "a2"} & set(next_request.contents)


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
async def test_compaction_after_an_approval_resume_reaches_the_stored_session(
    tmp_path: Path,
    summary_calls: _SummaryCalls,
) -> None:
    config, paths = _config(tmp_path)
    storage = _seeded_storage(config, paths)

    class _PausingLoop(_ToolLoopModel):
        async def ainvoke(self, messages: list[Message], **kwargs: object) -> ModelResponse:
            response = await super().ainvoke(messages, **kwargs)
            if len(self.requests) == 5 and response.tool_calls:
                response.tool_calls[0]["function"]["name"] = "approve_me"
            return response

    model = _PausingLoop(tool_rounds=12)
    agent = _scoped_agent(model, config, paths, storage)
    agent.tools = [probe, Function(name="approve_me", entrypoint=lambda: "approved", requires_confirmation=True)]
    paused = await agent.arun("Do the task", session_id="session-1")
    assert paused.status == RunStatus.paused
    compactions_before_resume = len(summary_calls.inputs)
    for requirement in paused.requirements or []:
        requirement.confirm()

    await agent.acontinue_run(run_id=paused.run_id, requirements=paused.requirements, session_id="session-1")

    assert len(summary_calls.inputs) > compactions_before_resume
    stored = get_agent_session(storage, "session-1")
    rows = summary_rows_of(storage, _SCOPE.key)
    assert stored is not None
    assert stored.summary is not None
    assert stored.summary.summary == rows[-1].summary
    # The completed run sorts after every fold of both legs, and the next reply replays only it after the summary.
    assert log_of(storage, _SCOPE.key) == [
        "run:run-1",
        "run:run-2",
        *(f"row:{row.summary}" for row in rows),
        f"run:{paused.run_id}",
    ]
    assert [run.run_id for run in stored.runs or []] == [paused.run_id]


@pytest.mark.asyncio
async def test_team_approval_resume_after_mid_turn_compaction(tmp_path: Path, summary_calls: _SummaryCalls) -> None:
    config, paths = _config(
        tmp_path,
        teams={"test_team": TeamConfig(display_name="Test Team", role="Test team", agents=["test_agent"])},
    )
    storage = create_session_storage("test_agent", config, paths, execution_identity=None)
    seed_session(
        storage,
        _team_session(
            "session-1",
            team_id="test_team",
            runs=[_completed_team_run("run-1", team_id="test_team"), _completed_team_run("run-2", team_id="test_team")],
        ),
    )

    class _PausingLoop(_ToolLoopModel):
        async def ainvoke(self, messages: list[Message], **kwargs: object) -> ModelResponse:
            response = await super().ainvoke(messages, **kwargs)
            if len(self.requests) == 5 and response.tool_calls:
                response.tool_calls[0]["function"]["name"] = "approve_me"
            return response

    model = _PausingLoop(tool_rounds=6)
    team = Team(
        id="test_team",
        name="test_team",
        model=model,
        members=[],
        tools=[probe, Function(name="approve_me", entrypoint=lambda: "approved", requires_confirmation=True)],
        db=storage,
        add_history_to_context=True,
        store_history_messages=False,
        add_session_summary_to_context=False,
        telemetry=False,
    )
    install_mid_turn_compaction(team, config=config, runtime_paths=paths, entity_name="test_team", model_name="default")

    paused = await team.arun("Do the task", session_id="session-1")
    assert paused.status == RunStatus.paused
    assert summary_calls.inputs
    paused_request = model.requests[-1]
    for requirement in paused.requirements or []:
        requirement.confirm()
    model.requests.clear()
    await team.acontinue_run(run_id=paused.run_id, requirements=paused.requirements, session_id="session-1")

    resumed = model.requests[0]
    summaries = [message for message in resumed.messages if is_compaction_summary(message)]
    assert len(summaries) == 1
    assert summaries[0].content == next(m.content for m in paused_request.messages if is_compaction_summary(m))
    stored = get_team_session(storage, "session-1")
    rows = summary_rows_of(storage, HistoryScope(kind="team", scope_id="test_team").key)
    assert stored is not None
    assert stored.summary is not None
    assert stored.summary.summary == rows[-1].summary


@pytest.mark.asyncio
async def test_failure_in_a_later_chunk_leaves_request_and_storage_unchanged(
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
    # A mid-turn compaction writes nothing until its last chunk succeeds.
    stored = get_agent_session(storage, "session-1")
    assert stored is not None
    assert summary_rows_of(storage, _SCOPE.key) == []
    assert stored.summary is None


@pytest.mark.asyncio
async def test_failure_in_a_later_chunk_of_the_turn_commits_nothing(
    tmp_path: Path,
    summary_calls: _SummaryCalls,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config, paths = _config(tmp_path, summary_context_window=6_000)
    storage = create_session_storage("test_agent", config, paths, execution_identity=None)
    seed_session(storage, _session("session-1"))

    async def fail_second(**kwargs: Any) -> SessionSummary:  # noqa: ANN401
        if summary_calls.inputs:
            summary_calls.inputs.append(kwargs["summary_input"])
            msg = "summary model unavailable"
            raise RuntimeError(msg)
        return await summary_calls(**kwargs)

    monkeypatch.setattr("mindroom.history.compaction.generate_compaction_summary", fail_second)
    model = _ToolLoopModel(tool_rounds=5)

    output = await _run_in_session(_scoped_agent(model, config, paths, storage), "Do the task")

    assert str(output.content).endswith("done")
    assert len(summary_calls.inputs) == 2
    # The second chunk failed, so nothing was written and the request kept every exchange unchanged.
    assert not any(is_compaction_summary(message) for request in model.requests for message in request.messages)
    assert summary_rows_of(storage, _SCOPE.key) == []
    stored = get_agent_session(storage, "session-1")
    assert stored is not None
    assert stored.summary is None


@pytest.mark.asyncio
@pytest.mark.parametrize("stop", [RuntimeError, asyncio.CancelledError])
async def test_rolling_back_a_partial_compaction_keeps_an_earlier_one(
    tmp_path: Path,
    summary_calls: _SummaryCalls,
    monkeypatch: pytest.MonkeyPatch,
    stop: type[BaseException],
) -> None:
    config, paths = _config(tmp_path, summary_context_window=6_000)
    storage = create_session_storage("test_agent", config, paths, execution_identity=None)
    seed_session(storage, _session("session-1"))
    attempts: list[int] = []
    first_summary: list[str | None] = []
    original_compact = mid_turn_compaction.compact_scope_mid_turn

    async def counted_compaction(**kwargs: Any) -> object:  # noqa: ANN401
        attempts.append(0)
        if len(attempts) == 2:
            rows = summary_rows_of(storage, _SCOPE.key)
            first_summary.append(rows[-1].summary if rows else None)
        return await original_compact(**kwargs)

    async def fail_second_chunk_of_second_attempt(**kwargs: Any) -> SessionSummary:  # noqa: ANN401
        attempts[-1] += 1
        if len(attempts) == 2 and attempts[-1] == 2:
            summary_calls.inputs.append(kwargs["summary_input"])
            raise stop
        return await summary_calls(**kwargs)

    monkeypatch.setattr(mid_turn_compaction, "compact_scope_mid_turn", counted_compaction)
    monkeypatch.setattr("mindroom.history.compaction.generate_compaction_summary", fail_second_chunk_of_second_attempt)

    agent = _scoped_agent(_ToolLoopModel(tool_rounds=14), config, paths, storage)
    if stop is asyncio.CancelledError:
        with pytest.raises(asyncio.CancelledError):
            await _run_in_session(agent, "Do the task")
    else:
        await _run_in_session(agent, "Do the task")

    assert len(attempts) >= 2
    assert attempts[1] == 2
    assert first_summary[0] is not None
    assert [row.summary for row in summary_rows_of(storage, _SCOPE.key)] == [first_summary[0]]


@pytest.mark.asyncio
async def test_cancellation_in_a_later_chunk_commits_nothing(
    tmp_path: Path,
    summary_calls: _SummaryCalls,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config, paths = _config(tmp_path, summary_context_window=6_000)
    storage = create_session_storage("test_agent", config, paths, execution_identity=None)
    seed_session(storage, _session("session-1"))

    async def cancel_second(**kwargs: Any) -> SessionSummary:  # noqa: ANN401
        if summary_calls.inputs:
            raise asyncio.CancelledError
        return await summary_calls(**kwargs)

    monkeypatch.setattr("mindroom.history.compaction.generate_compaction_summary", cancel_second)

    with pytest.raises(asyncio.CancelledError):
        await _run_in_session(_scoped_agent(_ToolLoopModel(tool_rounds=5), config, paths, storage), "Do the task")

    assert len(summary_calls.inputs) == 1
    assert summary_rows_of(storage, _SCOPE.key) == []


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
    # Two earlier runs and the running turn, however many exchanges the turn folded.
    outcome = next(event for event in lifecycle.events if isinstance(event, CompactionOutcome))
    assert outcome.compacted_run_count == 3


@pytest.mark.asyncio
@pytest.mark.usefixtures("summary_calls")
async def test_mid_turn_compaction_emits_compaction_hooks(tmp_path: Path) -> None:
    config, paths = _config(tmp_path)
    storage = _seeded_storage(config, paths)
    observed: list[CompactionHookContext] = []

    @hook(EVENT_COMPACTION_BEFORE)
    async def before(ctx: CompactionHookContext) -> None:
        observed.append(ctx)

    @hook(EVENT_COMPACTION_AFTER)
    async def after(ctx: CompactionHookContext) -> None:
        observed.append(ctx)

    registry = HookRegistry.from_plugins([_plugin("compaction-hooks", [before, after])])
    hook_context = _hook_runtime_context(config=config, runtime_paths=paths, registry=registry, session_id="session-1")

    with tool_runtime_context(hook_context):
        await _run_in_session(
            _scoped_agent(_ToolLoopModel(tool_rounds=5, calls_per_round=2), config, paths, storage),
            "Do the task",
        )

    assert [ctx.event_name for ctx in observed[:2]] == [EVENT_COMPACTION_BEFORE, EVENT_COMPACTION_AFTER]
    # The hooks see the running turn's folded tool work, not only the earlier runs.
    assert any(message.role == "tool" for message in observed[0].messages)
    # They see each folded message once, not the per-call copies the summarizer reads.
    assert len({message.id for message in observed[0].messages}) == len(observed[0].messages)
    assert observed[1].token_count_after is not None
    assert observed[1].token_count_after < observed[1].token_count_before


@pytest.mark.asyncio
async def test_cancellation_during_the_summary_call_writes_nothing_and_propagates(
    tmp_path: Path,
    summary_calls: _SummaryCalls,
) -> None:
    config, paths = _config(tmp_path)
    storage = _seeded_storage(config, paths)
    agent = _scoped_agent(_ToolLoopModel(tool_rounds=5), config, paths, storage)
    lifecycle = RecordingCompactionLifecycle()
    bind_compaction_lifecycle(agent, lifecycle)
    summary_calls.cancel = True

    with pytest.raises(asyncio.CancelledError):
        await _run_in_session(agent, "Do the task")

    assert summary_calls.inputs
    assert summary_rows_of(storage, _SCOPE.key) == []
    assert [type(event).__name__ for event in lifecycle.events] == [
        "CompactionLifecycleStart",
        "CompactionLifecycleFailure",
    ]


@pytest.mark.asyncio
@pytest.mark.usefixtures("summary_calls")
async def test_cancellation_after_the_commit_keeps_the_fold_and_its_effects(tmp_path: Path) -> None:
    config, paths = _config(tmp_path)
    storage = _seeded_storage(config, paths)
    model = _ToolLoopModel(tool_rounds=5)
    agent = _scoped_agent(model, config, paths, storage)

    class _CancelOnSuccess(RecordingCompactionLifecycle):
        async def complete_success(self, outcome: CompactionOutcome) -> None:  # noqa: ARG002
            raise asyncio.CancelledError

    bind_compaction_lifecycle(agent, _CancelOnSuccess())
    live_lists: list[list[Message]] = []
    run_ids: list[str] = []

    async def capture(messages: list[Message], _tools: object, run_response: RunOutput | None) -> None:
        live_lists.append(messages)
        if run_response is not None and run_response.run_id:
            run_ids.append(run_response.run_id)

    install_request_preparation(model, marker="_test_capture", prepare=capture)

    with pytest.raises(asyncio.CancelledError):
        await _run_in_session(agent, "Do the task")

    # The request was rewritten with the commit, before the lifecycle notice was cancelled.
    request = live_lists[-1]
    assert any(is_compaction_summary(message) for message in request)
    assert not any(message.role == "tool" for message in request)
    # The fold stays: a restarted attempt replays its summary instead of the runs it covered.
    assert [run_id for run_id, _segment in segments_of(storage, _SCOPE.key)] == [run_ids[-1]]
    assert folded_run_ids(storage) == ["run-1", "run-2"]


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

    layout = mid_turn_compaction._layout(messages, run_response, scoped=True)
    mid_turn_compaction._rewrite(messages, layout, compaction_summary_message("NEW", run_local=False))

    # The leading message is edited in place, so projections that track it by identity (approval receipts) still do.
    assert messages[0] is legacy_system
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


@pytest.mark.asyncio
async def test_oversized_native_request_in_a_stored_conversation_covers_the_turn(
    tmp_path: Path,
    summary_calls: _SummaryCalls,
) -> None:
    config, paths = _config(tmp_path)
    storage = _seeded_storage(config, paths)
    model = _NativeToolLoopModel(tool_rounds=5)
    model.configure_native_compaction(threshold=2000)

    output = await _run_in_session(_scoped_agent(model, config, paths, storage), "Do the task")

    assert summary_calls.inputs
    assert model.native_compaction is None
    rows = summary_rows_of(storage, _SCOPE.key)
    assert log_of(storage, _SCOPE.key) == [
        "run:run-1",
        "run:run-2",
        *(f"row:{row.summary}" for row in rows),
        f"run:{output.run_id}",
    ]
    stored = get_agent_session(storage, "session-1")
    assert stored is not None
    assert stored.summary is not None
    assert stored.summary.summary == rows[-1].summary


@pytest.mark.asyncio
async def test_failed_text_fallback_keeps_native_compaction(tmp_path: Path, summary_calls: _SummaryCalls) -> None:
    config, paths = _config(tmp_path)
    summary_calls.fail = True
    model = _NativeToolLoopModel(tool_rounds=5)
    model.configure_native_compaction(threshold=2000)

    await _run(_agent(model, config, paths), "Do the task")

    assert summary_calls.inputs
    assert model.native_compaction is not None


@pytest.mark.asyncio
async def test_a_request_the_target_never_built_is_not_compacted(
    tmp_path: Path,
    summary_calls: _SummaryCalls,
) -> None:
    config, paths = _config(tmp_path)
    model = _ToolLoopModel(tool_rounds=0)
    agent = _agent(model, config, paths)
    await _run(agent, "Say hi")
    # A skill review reuses the agent's model for its own fork of the conversation.
    fork = [Message(role="user", content="Review this"), Message(role="assistant", content="p" * 16_000)]

    await model.aresponse(
        messages=[*fork, Message(role="user", content="Improve a skill")],
        run_response=RunOutput(run_id="review"),
    )

    assert summary_calls.inputs == []


class _CheckpointingLoop(_NativeToolLoopModel):
    """A native tool loop whose provider compacts on its third request."""

    async def ainvoke(self, messages: list[Message], **kwargs: object) -> ModelResponse:
        response = await super().ainvoke(messages, **kwargs)
        if len(self.requests) == 3:
            items = [{"type": "compaction", "id": "cmp", "encrypted_content": "c" * 400}]
            record_native_checkpoint(response, items, self.native_compaction)
        return response


@pytest.mark.asyncio
async def test_thinking_a_native_checkpoint_replaced_never_triggers_text_compaction(
    tmp_path: Path,
    summary_calls: _SummaryCalls,
) -> None:
    config, paths = _config(tmp_path, context_window=40_000)
    model = _CheckpointingLoop(
        tool_rounds=6,
        usage=lambda number: MessageMetrics(input_tokens=0, output_tokens=15_000 if number < 3 else 5_000),
    )
    model.configure_native_compaction(threshold=20_000)

    await _run(_agent(model, config, paths), "Do the task")

    assert summary_calls.inputs == []
    assert model.native_compaction is not None


class _TwoTaskLeader(SyntheticModel):
    """Delegate two tasks to the same member in one response, then answer."""

    def __init__(self) -> None:
        super().__init__(id="leader-model", name="leader", provider="test")
        self.requests = 0

    async def ainvoke(self, messages: list[Message], **_kwargs: object) -> ModelResponse:  # noqa: ARG002
        self.requests += 1
        if self.requests > 1:
            return ModelResponse(content="team done")
        return ModelResponse(
            tool_calls=[
                {
                    "id": f"delegate_{task}",
                    "type": "function",
                    "function": {
                        "name": "delegate_task_to_member",
                        "arguments": json.dumps({"member_id": "test_agent", "task": f"Task {task}"}),
                    },
                }
                for task in ("A", "B")
            ],
        )

    async def ainvoke_stream(self, messages: list[Message], **kwargs: object) -> AsyncIterator[ModelResponse]:
        yield await self.ainvoke(messages, **kwargs)


class _PerTaskLoop(SyntheticModel):
    """Call ``probe`` a fixed number of times per delegated task, interleaving tasks, then answer."""

    def __init__(self, *, tool_rounds: int) -> None:
        super().__init__(id="test-model", name="test", provider="test")
        self.tool_rounds = tool_rounds
        self.calls: dict[str, int] = {}
        self.requests: list[tuple[str, list[Message]]] = []

    async def ainvoke(self, messages: list[Message], **_kwargs: object) -> ModelResponse:
        task = next(name for name in ("Task A", "Task B") if any(name in str(message.content) for message in messages))
        self.requests.append((task, list(messages)))
        await asyncio.sleep(0)
        self.calls[task] = self.calls.get(task, 0) + 1
        if self.calls[task] <= self.tool_rounds:
            call_id = f"call_{task[-1]}_{self.calls[task]}"
            return ModelResponse(
                tool_calls=[{"id": call_id, "type": "function", "function": {"name": "probe", "arguments": "{}"}}],
            )
        return ModelResponse(content=f"{task} done")

    async def ainvoke_stream(self, messages: list[Message], **kwargs: object) -> AsyncIterator[ModelResponse]:
        yield await self.ainvoke(messages, **kwargs)


@pytest.mark.asyncio
async def test_parallel_delegations_to_one_member_both_compact(tmp_path: Path, summary_calls: _SummaryCalls) -> None:
    config, paths = _config(tmp_path)
    member_model = _PerTaskLoop(tool_rounds=6)
    member = Agent(id="test_agent", name="Test Agent", model=member_model, tools=[probe], telemetry=False)
    install_mid_turn_compaction(
        member,
        config=config,
        runtime_paths=paths,
        entity_name="test_agent",
        model_name="default",
    )
    team = Team(id="crew", name="Crew", model=_TwoTaskLeader(), members=[member], telemetry=False)

    output = await team.arun("Coordinate the tasks")

    assert str(output.content).endswith("team done")
    assert summary_calls.inputs
    for task in ("Task A", "Task B"):
        compacted = [
            messages
            for name, messages in member_model.requests
            if name == task and any(is_compaction_summary(m) for m in messages)
        ]
        assert compacted, task


def test_a_team_fold_summarizes_the_tool_calls_of_the_members_it_delegated_to() -> None:
    """A stopped team reply drops the member tools a fold already covered, including a nested team's."""
    delegation = Message(
        role="assistant",
        tool_calls=[{"id": "delegate", "type": "function", "function": {"name": "delegate_task_to_member"}}],
    )
    layout = mid_turn_compaction._Layout(
        leading=0,
        kept=(),
        history=(),
        exchanges=((delegation, Message(role="tool", tool_call_id="delegate", content="done")),),
        prompt=None,
        previous_run_local_summary=None,
    )
    nested = TeamRunOutput(
        run_id="nested",
        member_responses=[RunOutput(run_id="deep", tools=[ToolExecution(tool_call_id="deep-call")])],
    )
    run_response = TeamRunOutput(
        run_id="team",
        member_responses=[RunOutput(run_id="member", tools=[ToolExecution(tool_call_id="member-call")]), nested],
    )

    segment = mid_turn_compaction._segment(run_response, layout, set())

    assert folded_tool_call_ids(segment.payload()) == {"delegate", "member-call", "deep-call"}
