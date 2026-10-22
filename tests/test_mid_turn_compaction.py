"""Compaction between two model requests of one turn, on the owned agent loop."""
# ruff: noqa: D103

from __future__ import annotations

import asyncio
import json
import re
import threading
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any
from uuid import uuid4

import pytest
from agno.tools import Toolkit
from agno.tools.function import Function

from mindroom import helper_usage
from mindroom.agent_loop.model_loop import LoopHooks, SendRequest
from mindroom.agent_loop.run import run_agent
from mindroom.agent_loop.spec import AgentSpec, RunContext
from mindroom.config.models import CompactionConfig, ModelConfig
from mindroom.constants import MATRIX_SOURCE_EVENT_PROMPTS_METADATA_KEY
from mindroom.helper_usage import HelperUsageOwner
from mindroom.history import compaction, mid_turn_compaction
from mindroom.history.mid_turn_compaction import mid_turn_compaction_hook
from mindroom.history.replay import compaction_summary_message, estimate_request_messages_tokens
from mindroom.history.request_messages import TurnMessages, build_request, scope_history_messages
from mindroom.history.run_visibility import QUEUED_MESSAGE_NOTICE_MARKER_KEY as _NOTICE_KEY
from mindroom.history.storage import new_scope_session, read_scope_state, set_force_compaction_state
from mindroom.history.summary_message import is_compaction_summary, is_scope_summary
from mindroom.history.summary_rows import folded_tool_call_ids
from mindroom.history.types import (
    CompactionLifecycleFailure,
    CompactionLifecycleStart,
    CompactionOutcome,
    HistoryScope,
    HistoryScopeState,
)
from mindroom.hooks import EVENT_COMPACTION_AFTER, EVENT_COMPACTION_BEFORE, CompactionHookContext, HookRegistry, hook
from mindroom.llm.events import RunFinished
from mindroom.llm.media import File, Image
from mindroom.llm.messages import Message
from mindroom.llm.provider import (
    Capabilities,
    ModelIdentity,
    ModelRequest,
    NativeCompactionReplay,
    TextDelta,
    ToolCallReady,
    ToolSpec,
    TurnCompleted,
    UsageReported,
)
from mindroom.llm.runs import RunRecord, RunStatus, TeamRunRecord, ToolExecution
from mindroom.llm.usage import MessageMetrics
from mindroom.model_usage import response_context_tokens
from mindroom.native_compaction import record_native_checkpoint
from mindroom.session_storage.agent_storage import create_session_storage, create_state_storage
from mindroom.session_storage.store import persist_to
from mindroom.tool_system.agno_toolkits import adapt_toolkit
from mindroom.tool_system.runtime_context import tool_runtime_context
from mindroom.usage.storage import project_usage, quote_identifier
from tests.conftest import seed_session
from tests.fakes.summary_model import summary_model
from tests.history_helpers import (
    RecordingCompactionLifecycle,
    _completed_run,
    _hook_runtime_context,
    _make_config,
    _plugin,
    _session,
    folded_run_ids,
    log_of,
    seed_summary,
    segments_of,
    summary_rows_of,
)
from tests.providers.openai_family_support import responses_adapter

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Callable, Sequence
    from pathlib import Path

    from mindroom.agent_loop.model_loop import BeforeRequestHook, LoopState, RequestDecision
    from mindroom.config.main import Config
    from mindroom.constants import RuntimePaths
    from mindroom.history.turn_recorder import TurnRecorder
    from mindroom.history.types import CompactionLifecycle
    from mindroom.llm.provider import StreamEvent
    from mindroom.session_storage.store import ConversationStore

_TOOL_RESULT = "r" * 4000
_SCOPE = HistoryScope(kind="agent", scope_id="test_agent")
_TEST_MODEL = ModelIdentity(id="test-model", provider="test", provider_name="test")


@dataclass
class _Request:
    roles: list[str]
    contents: list[str]
    messages: list[Message]
    request: ModelRequest


class _ToolLoopModel:
    """Call ``probe`` for ``tool_rounds`` requests, then answer; record every request."""

    def __init__(
        self,
        *,
        tool_rounds: int,
        calls_per_round: int = 1,
        usage: Callable[[int], MessageMetrics] | None = None,
        reasoning: str | None = None,
    ) -> None:
        self.identity = _TEST_MODEL
        self.capabilities = Capabilities()
        self.native_compaction = None
        self.replay_estimator = None
        self.model_params: dict[str, Any] = {}
        self.tool_rounds = tool_rounds
        self.calls_per_round = calls_per_round
        self.usage = usage
        self.reasoning = reasoning
        self.requests: list[_Request] = []

    def respond(self, number: int, request: ModelRequest) -> Message:  # noqa: ARG002
        metrics = self.usage(number) if self.usage is not None else MessageMetrics()
        if number <= self.tool_rounds:
            return Message(
                role="assistant",
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
                metrics=metrics,
            )
        return Message(role="assistant", content="done", metrics=metrics)

    async def stream(self, request: ModelRequest) -> AsyncIterator[StreamEvent]:
        self.requests.append(
            _Request(
                roles=[message.role for message in request.messages],
                contents=[str(message.content) for message in request.messages],
                messages=list(request.messages),
                request=request,
            ),
        )
        message = self.respond(len(self.requests), request)
        if message.content:
            yield TextDelta(str(message.content))
        for call in message.tool_calls or []:
            yield ToolCallReady(call)
        yield UsageReported(message.metrics)
        yield TurnCompleted(message=message, stop_reason="tool_use" if message.tool_calls else "end")


def probe() -> str:
    """Return one large tool result."""
    return _TOOL_RESULT


class _CancelOnSuccess(RecordingCompactionLifecycle):
    """Cancel the turn when its compaction reports success, right after the commit."""

    async def complete_success(self, outcome: CompactionOutcome) -> None:  # noqa: ARG002
        raise asyncio.CancelledError


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
        on_usage: Callable[[MessageMetrics], Any] | None = None,
    ) -> str:
        self.inputs.append(summary_input)
        if on_usage is not None:
            await on_usage(MessageMetrics(input_tokens=7, output_tokens=3))
        if self.cancel:
            raise asyncio.CancelledError
        if self.fail:
            msg = "summary model unavailable"
            raise RuntimeError(msg)
        return f"SUMMARY-{len(self.inputs)}"


@pytest.fixture
def summary_calls(monkeypatch: pytest.MonkeyPatch) -> _SummaryCalls:
    calls = _SummaryCalls()
    monkeypatch.setattr("mindroom.history.compaction.generate_compaction_summary", calls)
    monkeypatch.setattr("mindroom.history.compaction.build_model_provider", lambda *_args: summary_model())
    return calls


def _config(
    tmp_path: Path,
    *,
    enabled: bool = True,
    context_window: int | None = 4000,
    replay_window_tokens: int | None = None,
    summary_context_window: int = 100_000,
) -> tuple[Config, RuntimePaths]:
    return _make_config(
        tmp_path,
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


@dataclass
class _Agent:
    """The test agent as one reply runs it: its tools, where its runs are stored, and how its turns compact.

    ``replays_history`` puts the stored runs and the scope summary in each request; ``folds_into_scope`` binds the
    mid-turn hook to the scope, as a reply that replays its history does, so a fold appends a summary row.
    ``before_request`` hooks run ahead of the compaction hook; ``records`` keeps every run record a run stored.
    """

    model: _ToolLoopModel
    config: Config
    runtime_paths: RuntimePaths
    tools: list[Callable[..., object] | Function] = field(default_factory=lambda: [probe])
    store: ConversationStore | None = None
    replays_history: bool = False
    folds_into_scope: bool = False
    lifecycle: CompactionLifecycle | None = None
    turn_recorder: TurnRecorder | None = None
    native_compaction: NativeCompactionReplay | None = None
    tool_call_limit: int | None = None
    before_request: tuple[BeforeRequestHook, ...] = ()
    records: list[RunRecord] = field(default_factory=list)

    def spec(self) -> AgentSpec:
        return AgentSpec(
            id="test_agent",
            name="test_agent",
            model=self.model,
            system_message=None,
            toolkits=(adapt_toolkit(Toolkit(name="probes", tools=list(self.tools))),),
            tool_call_limit=self.tool_call_limit,
        )


def _agent(model: _ToolLoopModel, config: Config, runtime_paths: RuntimePaths, **kwargs: Any) -> _Agent:  # noqa: ANN401
    return _Agent(model, config, runtime_paths, **kwargs)


def _scoped_agent(
    model: _ToolLoopModel,
    config: Config,
    runtime_paths: RuntimePaths,
    storage: ConversationStore,
    **kwargs: Any,  # noqa: ANN401
) -> _Agent:
    kwargs.setdefault("folds_into_scope", True)
    return _Agent(model, config, runtime_paths, store=storage, replays_history=True, **kwargs)


def _turn(prompt: Sequence[Message] | str) -> TurnMessages:
    """Split a run's input as a reply does: transient context, the prompt (its last stored user message), context."""
    messages = [Message(role="user", content=prompt)] if isinstance(prompt, str) else list(prompt)
    transient = tuple(message for message in messages if not message.add_to_agent_memory)
    stored = [message for message in messages if message.add_to_agent_memory]
    return TurnMessages(prompt=stored[-1], context=tuple(stored[:-1]), transient=transient)


async def _run(
    agent: _Agent,
    prompt: Sequence[Message] | str,
    *,
    stream: bool = False,
    session_id: str = "session-1",
    metadata: dict[str, Any] | None = None,
) -> RunRecord:
    turn = _turn(prompt)
    store = agent.store
    session = (store.load_session(session_id, "agent") if store is not None else None) or new_scope_session(
        session_id=session_id,
        scope_id="test_agent",
        is_team=False,
    )
    settings = agent.config.resolve_entity("test_agent").history_settings
    history = (
        scope_history_messages(
            session.runs,
            scope=_SCOPE,
            num_history_runs=settings.policy.limit if settings.policy.mode == "runs" else None,
            num_history_messages=settings.policy.limit if settings.policy.mode == "messages" else None,
            max_tool_calls_from_history=settings.max_tool_calls_from_history,
        )
        if agent.replays_history
        else []
    )
    compaction_hook = mid_turn_compaction_hook(
        config=agent.config,
        runtime_paths=agent.runtime_paths,
        entity_name="test_agent",
        scope_id="test_agent",
        is_team=False,
        model_name="default",
        model=agent.model,
        history_settings=settings,
        store=store,
        session=session if agent.folds_into_scope else None,
        scope=_SCOPE if agent.folds_into_scope else None,
        native_compaction=agent.native_compaction,
        deferred_tool_names=frozenset(),
        prompt_id=turn.prompt.id,
        lifecycle=agent.lifecycle,
        turn_recorder=agent.turn_recorder,
    )
    store_persist = persist_to(store, session) if store is not None else None

    async def persist(record: RunRecord) -> None:
        agent.records.append(record)
        if store_persist is not None:
            await store_persist(record)

    events = run_agent(
        agent.spec(),
        build_request(
            system=(),
            summary=session.summary if agent.replays_history else None,
            history=history,
            turn=turn,
        ),
        ctx=RunContext(run_id=str(uuid4()), session_id=session_id, metadata=metadata),
        hooks=LoopHooks(before_request=(*agent.before_request, compaction_hook)),
        persist=persist,
        request_template=ModelRequest(messages=(), stream=stream, native_compaction=agent.native_compaction),
    )
    finished = [event async for event in events][-1]
    assert isinstance(finished, RunFinished)
    assert isinstance(finished.record, RunRecord)
    return finished.record


def _before_each_request(prepare: Callable[[LoopState], None]) -> BeforeRequestHook:
    """Return a hook that changes or watches a request just before compaction sizes it."""

    async def before_request(state: LoopState) -> RequestDecision:
        prepare(state)
        return SendRequest()

    return before_request


def _first_compacted(model: _ToolLoopModel) -> _Request:
    return next(request for request in model.requests if any(is_compaction_summary(m) for m in request.messages))


def _ends_with_prompt_then_summary(request: _Request, prompt: str = "Do the task") -> bool:
    """Whether a compacted request ends with its prompt and then the summary of the work done on it."""
    return request.contents[-2] == prompt and is_compaction_summary(request.messages[-1])


def _seeded_storage(config: Config, runtime_paths: RuntimePaths) -> ConversationStore:
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


def _usage_rows(storage: ConversationStore) -> list[tuple[str, dict[str, Any]]]:
    """Return every stored usage row as ``(session_id, usage)``."""
    with storage.transaction(write=False) as connection:
        rows = connection.execute(
            f"SELECT session_id, usage_data FROM {quote_identifier(storage.names.usage)}",  # noqa: S608
        ).fetchall()
    return [(session_id, json.loads(data)) for session_id, data in rows]


def _summary_usage(storage: ConversationStore) -> list[tuple[str, dict[str, Any]]]:
    return [
        (session_id, usage) for session_id, usage in _usage_rows(storage) if usage.get("kind") == "compaction_summary"
    ]


def _binding(tmp_path: Path, deferred_tool_names: frozenset[str]) -> mid_turn_compaction._MidTurnCompaction:
    """Return the mid-turn binding of a run-local test agent whose tool search defers ``deferred_tool_names``."""
    config, paths = _config(tmp_path)
    return mid_turn_compaction._MidTurnCompaction(
        config=config,
        runtime_paths=paths,
        entity_name="test_agent",
        scope_id="test_agent",
        is_team=False,
        model_name="default",
        model=_ToolLoopModel(tool_rounds=0),
        history_settings=config.resolve_entity("test_agent").history_settings,
        store=None,
        session=None,
        scope=None,
        native_compaction=None,
        deferred_tool_names=deferred_tool_names,
        prompt_id=None,
        lifecycle=None,
        turn_recorder=None,
    )


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
    assert _ends_with_prompt_then_summary(compacted)
    assert "tool" not in compacted.roles
    assert all(message.role != "assistant" for message in compacted.messages)
    stored_roles = [message.role for message in output.messages]
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


def test_request_sizing_never_counts_attached_file_bytes(tmp_path: Path) -> None:
    """A provider estimator must not count an attachment's encoded bytes as prompt text."""
    model = responses_adapter(tmp_path, "gpt-6-luna", store=False)
    pdf = File(content=b"%PDF-1.7\n" + b"x" * 300_000, mime_type="application/pdf", filename="report.pdf")
    message = Message(role="user", content="Summarize the attached report.", files=[pdf])

    tokens = estimate_request_messages_tokens([message], replay_model=model)

    assert tokens < 10_000


_BIG_TOOL = ToolSpec(name="big_tool", description="d" * 80_000, parameters={})


def test_request_sizing_leaves_out_deferred_tool_definitions(tmp_path: Path) -> None:
    """Tool search keeps deferred definitions out of the context until the model loads them."""
    tools = [_BIG_TOOL, ToolSpec(name="lookup", description="Look up a value.", parameters={})]
    binding = _binding(tmp_path, frozenset({"big_tool"}))

    assert mid_turn_compaction._tool_tokens(binding, tools, []) < 1_000
    assert mid_turn_compaction._tool_tokens(binding, tools, []) > 0


@pytest.mark.parametrize("provider", ["openai", "claude"])
def test_request_sizing_counts_deferred_tool_definitions_once_tool_search_loads_them(
    tmp_path: Path,
    provider: str,
) -> None:
    """A deferred definition enters the context once when a tool search in the request returns it.

    OpenAI replays the loaded definitions inside the search output, which the message estimate counts; Claude
    replays only references that the provider expands.
    """
    if provider == "openai":
        replay_model = responses_adapter(tmp_path, "gpt-6-luna", store=False)
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
                    "tools": [
                        {"type": "function", "name": "big_tool", "description": "d" * 80_000, "parameters": {}},
                    ],
                },
            ],
        }
    else:
        replay_model = None
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

    total = estimate_request_messages_tokens(searched, replay_model=replay_model) + mid_turn_compaction._tool_tokens(
        _binding(tmp_path, frozenset({"big_tool"})),
        [_BIG_TOOL],
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
    assert compacted.contents[-3] == "Do the task"
    assert is_compaction_summary(compacted.messages[-2])
    assert compacted.contents[-1] == "transient turn context"
    assert "u" * 100 in summary_calls.inputs[0]
    assert "transient turn context" not in summary_calls.inputs[0]


@pytest.mark.asyncio
async def test_rewritten_request_carries_no_response_chain(tmp_path: Path, summary_calls: _SummaryCalls) -> None:
    config, paths = _config(tmp_path)
    model = _ToolLoopModel(tool_rounds=5)

    def chain(state: LoopState) -> None:
        for message in state.messages:
            if message.role == "assistant":
                message.provider_data = {**(message.provider_data or {}), "response_id": "resp"}

    await _run(_agent(model, config, paths, before_request=(_before_each_request(chain),)), "Do the task")

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
    config.agents["test_agent"].max_tool_calls_from_history = limit
    model = _ToolLoopModel(tool_rounds=5)

    await _run(_agent(model, config, paths), "Do the task")

    assert summary_calls.inputs
    assert summary_calls.inputs[0].count(_TOOL_RESULT[:200]) >= 2


@pytest.mark.asyncio
async def test_rewritten_request_over_the_limit_raises_the_summary_budget_error(
    tmp_path: Path,
    summary_calls: _SummaryCalls,
) -> None:
    config, paths = _config(tmp_path)
    model = _ToolLoopModel(tool_rounds=5)
    agent = _agent(model, config, paths)

    with pytest.raises(
        mid_turn_compaction.HistorySummaryBudgetError,
        match="Saved conversation summary exceeds the available history budget",
    ):
        await _run(agent, [Message(role="user", content="p" * 16_000)])

    assert summary_calls.inputs
    assert [record.status for record in agent.records] == [RunStatus.error]


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
        "mindroom.history.compaction.build_model_provider",
        lambda *_args: summary_model(model_id="claude-haiku-5-5", claude=True),
    )
    config, paths = _config(tmp_path, context_window=20_000, summary_context_window=8_000)
    model = _ToolLoopModel(tool_rounds=25)
    facts = iter(range(100))

    def probe() -> str:
        """Return one tool result carrying a distinct fact."""
        return f"FACT-{next(facts):02d} " + "r" * 4_000

    output = await _run(_agent(model, config, paths, tools=[probe]), "Do the task", stream=stream)

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


class _PathLoop(_ToolLoopModel):
    """A tool loop whose calls each name their own file."""

    def respond(self, number: int, request: ModelRequest) -> Message:
        message = super().respond(number, request)
        for call in message.tool_calls or []:
            call["function"]["arguments"] = json.dumps({"path": f"{call['id']}.txt"})
        return message


@pytest.mark.asyncio
async def test_a_parallel_tool_batch_larger_than_one_summary_input_folds_without_truncation(
    tmp_path: Path,
    summary_calls: _SummaryCalls,
) -> None:
    """A batch is kept whole in the request, but its results reach the summarizer across chunks, none cut short."""
    config, paths = _config(tmp_path, context_window=8_000, summary_context_window=6_000)
    model = _PathLoop(tool_rounds=2, calls_per_round=4)
    facts = iter(range(100))
    fact_paths: dict[str, str] = {}

    def probe(path: str) -> str:
        """Return one tool result carrying a distinct fact at its end.

        Args:
            path: The file to read.

        """
        fact = f"FACT-{next(facts):02d}"
        fact_paths[fact] = path
        return "r" * 4_000 + f" {fact}"

    output = await _run(_agent(model, config, paths, tools=[probe]), "Do the task")

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
        agent = _scoped_agent(model, config, paths, storage, tools=[probe])
    else:
        agent = _agent(model, config, paths, tools=[probe])
    # The prompt fits the model's window but not one summary input.
    prompt = "Do the task. " + "p" * 40_000
    # A Matrix turn also records its source prompts in the run's metadata.
    metadata = {MATRIX_SOURCE_EVENT_PROMPTS_METADATA_KEY: {"$prompt": prompt}}

    output = await _run(agent, prompt, metadata=metadata)

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

    output = await _run(_agent(model, config, paths, tools=[probe]), "Do the task")

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
@pytest.mark.usefixtures("summary_calls")
async def test_folded_request_usage_stays_on_a_run_cancelled_after_compaction(tmp_path: Path) -> None:
    config, paths = _config(tmp_path)
    storage = _seeded_storage(config, paths)
    model = _ToolLoopModel(
        tool_rounds=5,
        usage=lambda number: MessageMetrics(input_tokens=1001 * number, output_tokens=1),
    )
    agent = _scoped_agent(model, config, paths, storage, lifecycle=_CancelOnSuccess())

    with pytest.raises(asyncio.CancelledError):
        await _run(agent, "Do the task")

    # The cancelled run's record keeps the usage of the requests the fold took out of it.
    requests = project_usage(agent.records[-1].to_dict())["requests"]
    assert [request["metrics"]["input_tokens"] for request in requests] == [
        1001 * number for number in range(1, len(model.requests) + 1)
    ]


@pytest.mark.asyncio
async def test_queued_notice_survives_mid_turn_compaction(tmp_path: Path, summary_calls: _SummaryCalls) -> None:
    config, paths = _config(tmp_path)
    model = _ToolLoopModel(tool_rounds=5)
    notice = Message(role="user", content="A newer message is waiting.", provider_data={_NOTICE_KEY: True})

    def add_notice(state: LoopState) -> None:
        if len(model.requests) == 2 and notice not in state.messages:
            state.messages.append(notice)

    await _run(_agent(model, config, paths, before_request=(_before_each_request(add_notice),)), "Do the task")

    assert summary_calls.inputs
    compacted = _first_compacted(model)
    # The notice arrived after the folded work, so it stays the last word after the summary.
    assert compacted.contents[-3] == "Do the task"
    assert is_compaction_summary(compacted.messages[-2])
    assert compacted.contents[-1] == "A newer message is waiting."


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

    layout = mid_turn_compaction._layout(messages, prompt.id, scoped=True)

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

    layout = mid_turn_compaction._layout(messages, prompt.id, scoped=False)

    assert layout.folded == (call, result)
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
    media = Message(
        role="user",
        content="The tool call above generated the attached media.",
        images=[Image(id="tool_image", content=b"\x89PNG" + b"0" * 400, mime_type="image/png")],
    )

    def add_media(state: LoopState) -> None:
        if len(model.requests) == 1 and media not in state.messages:
            state.messages.append(media)

    await _run(_agent(model, config, paths, before_request=(_before_each_request(add_media),)), "Do the task")

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
    storage = create_session_storage("test_agent", config, paths, execution_identity=None)

    await _run(_agent(_ToolLoopModel(tool_rounds=5), config, paths, store=storage), "Do the task")

    summaries = _summary_usage(storage)
    assert len(summaries) == len(summary_calls.inputs) >= 1
    assert all(usage["metrics"]["input_tokens"] == 7 for _session_id, usage in summaries)


@pytest.mark.asyncio
async def test_scoped_summary_usage_is_recorded_in_the_scope_storage(
    tmp_path: Path,
    summary_calls: _SummaryCalls,
) -> None:
    config, paths = _config(tmp_path)
    storage = _seeded_storage(config, paths)

    await _run(_scoped_agent(_ToolLoopModel(tool_rounds=5), config, paths, storage), "Do the task")

    assert len(_summary_usage(storage)) == len(summary_calls.inputs) >= 1


@pytest.mark.asyncio
async def test_run_local_summary_usage_goes_to_the_replys_usage_owner(
    tmp_path: Path,
    summary_calls: _SummaryCalls,
) -> None:
    config, paths = _config(tmp_path)
    owner_storage = create_state_storage("owner", tmp_path, session_table="owner_sessions")
    owner = HelperUsageOwner(
        storage_factory=lambda: owner_storage,
        session_id="owner-session",
        initial_session=new_scope_session(session_id="owner-session", scope_id="crew", is_team=True),
    )
    token = helper_usage._HELPER_USAGE_OWNER.set(owner)
    try:
        # A team member keeps no storage of its own; the reply's owner records its summary usage.
        await _run(_agent(_ToolLoopModel(tool_rounds=5), config, paths), "Do the task")
    finally:
        helper_usage._HELPER_USAGE_OWNER.reset(token)

    summaries = _summary_usage(owner_storage)
    assert len(summaries) == len(summary_calls.inputs) >= 1
    assert {session_id for session_id, _ in summaries} == {"owner-session"}


@pytest.mark.asyncio
async def test_the_loop_caps_a_request_before_compacting_it(tmp_path: Path, summary_calls: _SummaryCalls) -> None:
    config, paths = _config(tmp_path, context_window=12_000)
    # The fourth response reports a request far over the window, so only the fifth request would compact.
    model = _ToolLoopModel(
        tool_rounds=8,
        usage=lambda number: MessageMetrics(input_tokens=1_000_000 if number == 4 else 10, output_tokens=1),
    )

    # The cap allows the limit plus two requests, so it refuses the fifth.
    await _run(_agent(model, config, paths, tool_call_limit=2), "Do the task")

    assert len(model.requests) == 4
    assert summary_calls.inputs == []


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

    output = await _run(agent, "Do the task", stream=stream)

    assert str(output.content).endswith("done")
    compacted = _first_compacted(model)
    summaries = [message for message in compacted.messages if is_compaction_summary(message)]
    assert len(summaries) == 1
    assert summaries[0].from_history is True
    assert _ends_with_prompt_then_summary(compacted)
    assert not {"q1", "a1", "q2", "a2"} & set(compacted.contents)
    # The fold's rows sort after the stored runs they cover, and the run after them.
    rows = summary_rows_of(storage, _SCOPE.key)
    assert log_of(storage, _SCOPE.key) == [
        "run:run-1",
        "run:run-2",
        *(f"row:{row.summary}" for row in rows),
        f"run:{output.run_id}",
    ]
    assert [run_id for run_id, _segment in segments_of(storage, _SCOPE.key)] == [output.run_id] * len(rows)
    stored = storage.load_session("session-1", "agent")
    assert stored is not None
    assert stored.summary is not None
    assert stored.summary.text == rows[-1].summary
    assert [run.run_id for run in stored.runs] == [output.run_id]


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

    output = await _run(_scoped_agent(model, config, paths, storage), "p" * 16_000)

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

    output = await _run(_scoped_agent(_ToolLoopModel(tool_rounds=5), config, paths, storage), "Do the task")

    assert summary_calls.inputs
    rows = summary_rows_of(storage, _SCOPE.key)
    assert rows
    assert log_of(storage, _SCOPE.key) == [*(f"row:{row.summary}" for row in rows), f"run:{output.run_id}"]
    assert {run_id for run_id, _segment in segments_of(storage, _SCOPE.key)} == {output.run_id}


@pytest.mark.asyncio
async def test_a_second_fold_of_the_turn_covers_no_run_twice(tmp_path: Path, summary_calls: _SummaryCalls) -> None:
    config, paths = _config(tmp_path)
    storage = _seeded_storage(config, paths)

    await _run(_scoped_agent(_ToolLoopModel(tool_rounds=12), config, paths, storage), "Do the task")

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

    await _run(agent, "Do the task", metadata={"matrix_seen_event_ids": ["$x"]})

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
    # Tool definitions alone push the first request, which has no exchanges of its own yet, over the limit.
    lookup = Function(name="lookup", description="d" * 10_000, entrypoint=lambda: "ok")
    agent = _scoped_agent(model, config, paths, storage, tools=[lookup])

    output = await _run(agent, "Do the task")

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
) -> None:
    config, paths = _config(tmp_path)
    storage = _seeded_storage(config, paths)
    model = _ToolLoopModel(tool_rounds=5)
    # A request that replays history but cannot update its stored session compacts run-locally.
    agent = _scoped_agent(model, config, paths, storage, folds_into_scope=False)

    output = await _run(agent, "Do the task")

    assert str(output.content).endswith("done")
    assert summary_calls.inputs
    assert "q1" in summary_calls.inputs[0]
    assert "q2" in summary_calls.inputs[0]


@pytest.mark.asyncio
async def test_a_replayed_run_local_fold_leaves_the_scope_summary_first(
    tmp_path: Path,
    summary_calls: _SummaryCalls,
) -> None:
    """Only a scoped fold moves the scope's summary after its prompt; a stored run-local fold keeps its own in place."""
    config, paths = _config(tmp_path)
    storage = create_session_storage("test_agent", config, paths, execution_identity=None)
    session = _session("session-1")
    seed_session(storage, session)
    seed_summary(storage, session, _SCOPE, "EARLIER-WORK")
    model = _ToolLoopModel(tool_rounds=5)
    # A request that replays history but cannot update its stored session compacts run-locally.
    await _run(_scoped_agent(model, config, paths, storage, folds_into_scope=False), "Do the task")
    assert summary_calls.inputs
    model.requests.clear()
    model.tool_rounds = 0

    await _run(_scoped_agent(model, config, paths, storage), "Thanks")

    request = model.requests[0]
    first_history = next(index for index, message in enumerate(request.messages) if message.role != "system")
    assert is_scope_summary(request.messages[first_history])
    assert "Do the task" in request.contents


@pytest.mark.asyncio
async def test_next_turn_reuses_the_post_compaction_prefix(tmp_path: Path, summary_calls: _SummaryCalls) -> None:
    config, paths = _config(tmp_path)
    storage = _seeded_storage(config, paths)
    model = _ToolLoopModel(tool_rounds=5)
    agent = _scoped_agent(model, config, paths, storage)
    await _run(agent, "Do the task")
    last_request = model.requests[-1]
    calls_after_first_turn = len(summary_calls.inputs)
    model.requests.clear()
    model.tool_rounds = 0

    await _run(agent, "Thanks")

    next_request = model.requests[0]
    assert calls_after_first_turn
    assert len(summary_calls.inputs) == calls_after_first_turn
    assert next_request.roles[: len(last_request.roles)] == last_request.roles
    assert next_request.contents[: len(last_request.contents)] == last_request.contents
    assert not {"q1", "a1", "q2", "a2"} & set(next_request.contents)


@pytest.mark.asyncio
async def test_failure_in_a_later_chunk_of_the_turn_commits_nothing(
    tmp_path: Path,
    summary_calls: _SummaryCalls,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config, paths = _config(tmp_path, summary_context_window=6_000)
    storage = create_session_storage("test_agent", config, paths, execution_identity=None)
    seed_session(storage, _session("session-1"))

    async def fail_second(**kwargs: Any) -> str:  # noqa: ANN401
        if summary_calls.inputs:
            summary_calls.inputs.append(kwargs["summary_input"])
            msg = "summary model unavailable"
            raise RuntimeError(msg)
        return await summary_calls(**kwargs)

    monkeypatch.setattr("mindroom.history.compaction.generate_compaction_summary", fail_second)
    model = _ToolLoopModel(tool_rounds=5)

    output = await _run(_scoped_agent(model, config, paths, storage), "Do the task")

    assert str(output.content).endswith("done")
    assert len(summary_calls.inputs) == 2
    # The second chunk failed, so nothing was written and the request kept every exchange unchanged.
    assert not any(is_compaction_summary(message) for request in model.requests for message in request.messages)
    assert summary_rows_of(storage, _SCOPE.key) == []
    stored = storage.load_session("session-1", "agent")
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

    async def fail_second_chunk_of_second_attempt(**kwargs: Any) -> str:  # noqa: ANN401
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
            await _run(agent, "Do the task")
    else:
        await _run(agent, "Do the task")

    assert len(attempts) >= 2
    assert attempts[1] == 2
    assert first_summary[0] is not None
    assert [row.summary for row in summary_rows_of(storage, _SCOPE.key)] == [first_summary[0]]


@pytest.mark.asyncio
async def test_force_flag_survives_mid_turn_compaction(tmp_path: Path, summary_calls: _SummaryCalls) -> None:
    config, paths = _config(tmp_path)
    storage = _seeded_storage(config, paths)
    stored = storage.load_session("session-1", "agent")
    assert stored is not None
    set_force_compaction_state(stored, _SCOPE, HistoryScopeState(), force=True)
    storage.save_session_fields(stored, "metadata")
    model = _ToolLoopModel(tool_rounds=5)

    await _run(_scoped_agent(model, config, paths, storage), "Do the task")

    assert summary_calls.inputs
    after = storage.load_session("session-1", "agent")
    assert after is not None
    assert read_scope_state(after, _SCOPE).force_compact_before_next_run is True


@pytest.mark.asyncio
async def test_mid_turn_compaction_posts_notices(tmp_path: Path, summary_calls: _SummaryCalls) -> None:
    config, paths = _config(tmp_path)
    storage = _seeded_storage(config, paths)
    lifecycle = RecordingCompactionLifecycle()

    await _run(_scoped_agent(_ToolLoopModel(tool_rounds=5), config, paths, storage, lifecycle=lifecycle), "Do the task")

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
        await _run(
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
    lifecycle = RecordingCompactionLifecycle()
    agent = _scoped_agent(_ToolLoopModel(tool_rounds=5), config, paths, storage, lifecycle=lifecycle)
    summary_calls.cancel = True

    with pytest.raises(asyncio.CancelledError):
        await _run(agent, "Do the task")

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
    live_lists: list[list[Message]] = []
    run_ids: list[str] = []

    def capture(state: LoopState) -> None:
        live_lists.append(state.messages)
        run_ids.append(state.record.run_id)

    agent = _scoped_agent(
        _ToolLoopModel(tool_rounds=5),
        config,
        paths,
        storage,
        lifecycle=_CancelOnSuccess(),
        before_request=(_before_each_request(capture),),
    )

    with pytest.raises(asyncio.CancelledError):
        await _run(agent, "Do the task")

    # The request was rewritten with the commit, before the lifecycle notice was cancelled.
    request = live_lists[-1]
    assert any(is_compaction_summary(message) for message in request)
    assert not any(message.role == "tool" for message in request)
    # The fold stays: a restarted attempt replays its summary instead of the runs it covered.
    assert [run_id for run_id, _segment in segments_of(storage, _SCOPE.key)] == [run_ids[-1]]
    assert folded_run_ids(storage) == ["run-1", "run-2"]


@dataclass
class _HeldSuccess(RecordingCompactionLifecycle):
    """Hold the success notice until released, so the turn can be stopped while it is being delivered."""

    entered: asyncio.Event = field(default_factory=asyncio.Event)
    release: asyncio.Event = field(default_factory=asyncio.Event)

    async def complete_success(self, outcome: CompactionOutcome) -> None:
        self.entered.set()
        await self.release.wait()
        await super().complete_success(outcome)


@pytest.mark.asyncio
@pytest.mark.usefixtures("summary_calls")
async def test_stopping_the_turn_after_the_commit_still_reports_success(tmp_path: Path) -> None:
    """A stopped turn finishes reporting a stored fold, so its notice never claims the compaction failed."""
    config, paths = _config(tmp_path)
    storage = _seeded_storage(config, paths)
    lifecycle = _HeldSuccess()
    agent = _scoped_agent(_ToolLoopModel(tool_rounds=5), config, paths, storage, lifecycle=lifecycle)

    task = asyncio.create_task(_run(agent, "Do the task"))
    await asyncio.wait_for(lifecycle.entered.wait(), timeout=5)
    task.cancel()
    await asyncio.sleep(0)
    lifecycle.release.set()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert [type(event) for event in lifecycle.events] == [CompactionLifecycleStart, CompactionOutcome]


@pytest.mark.asyncio
@pytest.mark.usefixtures("summary_calls")
async def test_stopping_the_turn_while_the_fold_is_stored_still_reports_success(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A stop that lands while the row is being written still lets the stored fold apply and report success."""
    config, paths = _config(tmp_path)
    storage = _seeded_storage(config, paths)
    lifecycle = RecordingCompactionLifecycle()
    agent = _scoped_agent(_ToolLoopModel(tool_rounds=5), config, paths, storage, lifecycle=lifecycle)
    loop = asyncio.get_running_loop()
    appending = asyncio.Event()
    release = threading.Event()
    append_fold = compaction.append_fold

    def held_append(*args: Any, **kwargs: Any) -> bool:  # noqa: ANN401
        loop.call_soon_threadsafe(appending.set)
        assert release.wait(timeout=5)
        return append_fold(*args, **kwargs)

    monkeypatch.setattr(compaction, "append_fold", held_append)
    task = asyncio.create_task(_run(agent, "Do the task"))
    await asyncio.wait_for(appending.wait(), timeout=5)
    task.cancel()
    await asyncio.sleep(0)
    release.set()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert [type(event) for event in lifecycle.events] == [CompactionLifecycleStart, CompactionOutcome]
    assert len(segments_of(storage, _SCOPE.key)) == 1


@pytest.mark.asyncio
@pytest.mark.usefixtures("summary_calls")
async def test_a_fold_that_loses_its_append_reports_the_change_and_keeps_the_request(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A fold another writer outran stores nothing, leaves the request whole, and says the conversation changed."""
    config, paths = _config(tmp_path)
    storage = _seeded_storage(config, paths)
    model = _ToolLoopModel(tool_rounds=5)
    lifecycle = RecordingCompactionLifecycle()
    monkeypatch.setattr(compaction, "append_fold", lambda *_args, **_kwargs: False)

    await _run(_scoped_agent(model, config, paths, storage, lifecycle=lifecycle), "Do the task")

    assert type(lifecycle.events[0]) is CompactionLifecycleStart
    assert isinstance(lifecycle.events[1], CompactionLifecycleFailure)
    assert lifecycle.events[1].failure_reason == "The conversation changed during compaction."
    assert not any(is_compaction_summary(message) for request in model.requests for message in request.messages)
    assert segments_of(storage, _SCOPE.key) == []


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

    layout = mid_turn_compaction._layout(messages, prompt.id, scoped=True)
    mid_turn_compaction._rewrite(messages, layout, compaction_summary_message("NEW", run_local=False))

    # The leading message is edited in place, so projections that track it by identity (approval receipts) still do.
    assert messages[0] is legacy_system
    assert messages[0].content == "Be precise.\n\nCurrent date: Monday"
    assert [is_compaction_summary(message) for message in messages] == [False, False, True]
    assert messages[1] is prompt


_NATIVE_ROUTE = "test-route"


def _native(threshold: int) -> NativeCompactionReplay:
    return NativeCompactionReplay(route=_NATIVE_ROUTE, threshold=threshold)


@pytest.mark.asyncio
async def test_native_request_under_the_limit_never_text_compacts(
    tmp_path: Path,
    summary_calls: _SummaryCalls,
) -> None:
    config, paths = _config(tmp_path, context_window=1_000_000)
    model = _ToolLoopModel(tool_rounds=4, usage=lambda number: MessageMetrics(input_tokens=999_900 * number))

    await _run(_agent(model, config, paths, native_compaction=_native(500_000)), "Do the task")

    assert summary_calls.inputs == []
    assert model.requests[-1].request.native_compaction == _native(500_000)


@pytest.mark.asyncio
async def test_oversized_native_request_turns_native_off_and_compacts_as_text(
    tmp_path: Path,
    summary_calls: _SummaryCalls,
) -> None:
    config, paths = _config(tmp_path)
    model = _ToolLoopModel(tool_rounds=5)

    await _run(_agent(model, config, paths, native_compaction=_native(2000)), "Do the task")

    assert summary_calls.inputs
    assert model.requests[-1].request.native_compaction is None
    compacted = _first_compacted(model)
    assert _ends_with_prompt_then_summary(compacted)
    assert "tool" not in compacted.roles


@pytest.mark.asyncio
async def test_oversized_native_request_in_a_stored_conversation_covers_the_turn(
    tmp_path: Path,
    summary_calls: _SummaryCalls,
) -> None:
    config, paths = _config(tmp_path)
    storage = _seeded_storage(config, paths)
    model = _ToolLoopModel(tool_rounds=5)

    output = await _run(_scoped_agent(model, config, paths, storage, native_compaction=_native(2000)), "Do the task")

    assert summary_calls.inputs
    assert model.requests[-1].request.native_compaction is None
    rows = summary_rows_of(storage, _SCOPE.key)
    assert log_of(storage, _SCOPE.key) == [
        "run:run-1",
        "run:run-2",
        *(f"row:{row.summary}" for row in rows),
        f"run:{output.run_id}",
    ]
    stored = storage.load_session("session-1", "agent")
    assert stored is not None
    assert stored.summary is not None
    assert stored.summary.text == rows[-1].summary


@pytest.mark.asyncio
async def test_failed_text_fallback_keeps_native_compaction(tmp_path: Path, summary_calls: _SummaryCalls) -> None:
    config, paths = _config(tmp_path)
    summary_calls.fail = True
    model = _ToolLoopModel(tool_rounds=5)

    await _run(_agent(model, config, paths, native_compaction=_native(2000)), "Do the task")

    assert summary_calls.inputs
    assert model.requests[-1].request.native_compaction == _native(2000)


class _CheckpointingLoop(_ToolLoopModel):
    """A tool loop on a native route whose provider compacts on its third request."""

    def respond(self, number: int, request: ModelRequest) -> Message:
        message = super().respond(number, request)
        if number == 3:
            items = [{"type": "compaction", "id": "cmp", "encrypted_content": "c" * 400}]
            record_native_checkpoint(message, items, request.native_compaction)
        return message


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

    await _run(_agent(model, config, paths, native_compaction=_native(20_000)), "Do the task")

    assert summary_calls.inputs == []
    assert model.requests[-1].request.native_compaction == _native(20_000)


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
        folded=(delegation, Message(role="tool", tool_call_id="delegate", content="done")),
        prompt=None,
        previous_run_local_summary=None,
    )
    nested = TeamRunRecord(
        run_id="nested",
        member_responses=[RunRecord(run_id="deep", tools=[ToolExecution(tool_call_id="deep-call")])],
    )
    team_run = TeamRunRecord(
        run_id="team",
        member_responses=[RunRecord(run_id="member", tools=[ToolExecution(tool_call_id="member-call")]), nested],
    )

    segment = mid_turn_compaction._segment(team_run, layout, set())

    assert folded_tool_call_ids(segment.payload()) == {"delegate", "member-call", "deep-call"}
