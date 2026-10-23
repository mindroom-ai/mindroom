"""Shared helpers for the history test modules."""
# ruff: noqa: D102, ANN201, TC003

from __future__ import annotations

import asyncio
import json
import sqlite3
from collections.abc import AsyncIterator, Iterator
from contextlib import closing
from dataclasses import dataclass, field
from pathlib import Path
from types import SimpleNamespace
from typing import TYPE_CHECKING, Any
from unittest.mock import AsyncMock

from agno.agent import Agent
from agno.models.base import Model
from agno.models.response import ModelResponse

from mindroom.config.agent import AgentConfig, TeamConfig
from mindroom.config.main import Config
from mindroom.config.models import CompactionConfig, CompactionOverrideConfig, DefaultsConfig, ModelConfig
from mindroom.config.plugin import PluginEntryConfig
from mindroom.constants import (
    DEFAULT_COMPACTION_TIMEOUT_SECONDS,
    RuntimePaths,
    resolve_runtime_paths,
)
from mindroom.history import summary_rows
from mindroom.history.compaction import Summarizer, SummaryModel
from mindroom.history.storage import (
    _with_scope_state,
    append_compaction_chunk,
    new_scope_session,
    read_chunk_basis,
)
from mindroom.history.types import (
    CompactionLifecycleFailure,
    CompactionLifecycleProgress,
    CompactionLifecycleStart,
    CompactionOutcome,
    HistoryPolicy,
    HistoryScope,
    HistoryScopeState,
    ResolvedHistorySettings,
)
from mindroom.hooks import (
    HookRegistry,
)
from mindroom.llm.messages import Message
from mindroom.llm.provider import (
    Capabilities,
    ModelIdentity,
    ModelRequest,
    StreamEvent,
    TextDelta,
    ToolCallReady,
    TurnCompleted,
    UsageReported,
)
from mindroom.llm.runs import RunRecord, RunStatus, TeamRunRecord
from mindroom.llm.usage import MessageMetrics
from mindroom.message_target import MessageTarget
from mindroom.prompts import COMPACTION_SUMMARY_PROMPT
from mindroom.session_storage.agent_storage import create_session_storage
from mindroom.session_storage.types import ConversationSession
from mindroom.usage.storage import quote_identifier
from tests.authorization_helpers import (
    make_test_tool_runtime_context,
)
from tests.conftest import (
    bind_runtime_paths,
    make_conversation_reader_mock,
    make_relation_lookup,
    seed_session,
)
from tests.fakes.summary_model import summary_model

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

    from agno.models.message import Message as AgnoMessage

    from mindroom.history.types import ScopeSummary
    from mindroom.llm.provider import ModelProvider
    from mindroom.llm.runs import AnyRunRecord
    from mindroom.session_storage.store import ConversationStore
    from mindroom.tool_system.runtime_context import ToolRuntimeContext


class FakeModel(Model):
    """Minimal model returning one canned response, for deterministic agent tests."""

    def invoke(self, *_args: object, **_kwargs: object) -> ModelResponse:
        """Return one successful fake response."""
        return ModelResponse(content="ok")

    async def ainvoke(self, *_args: object, **_kwargs: object) -> ModelResponse:
        """Return one successful fake async response."""
        return ModelResponse(content="ok")

    def invoke_stream(self, *_args: object, **_kwargs: object) -> Iterator[ModelResponse]:
        """Yield one successful fake streaming response."""
        yield ModelResponse(content="ok")

    async def ainvoke_stream(self, *_args: object, **_kwargs: object) -> AsyncIterator[ModelResponse]:
        """Yield one successful fake async streaming response."""
        yield ModelResponse(content="ok")

    def _parse_provider_response(self, response: ModelResponse, *_args: object, **_kwargs: object) -> ModelResponse:
        return response

    def _parse_provider_response_delta(
        self,
        response: ModelResponse,
        *_args: object,
        **_kwargs: object,
    ) -> ModelResponse:
        return response


_DEFAULT_TEST_COMPACTION = CompactionConfig()

_ALL_HISTORY_SETTINGS = ResolvedHistorySettings(
    policy=HistoryPolicy(mode="all"),
    max_tool_calls_from_history=None,
)


def _summarizer(
    model: ModelProvider | None = None,
    input_budget_tokens: int = 16_000,
    *,
    name: str = "summary-model",
    fallback: SummaryModel | None = None,
    history_settings: ResolvedHistorySettings = _ALL_HISTORY_SETTINGS,
    prompt: str = COMPACTION_SUMMARY_PROMPT,
    timeout_seconds: float = DEFAULT_COMPACTION_TIMEOUT_SECONDS,
    session_id: str = "session-1",
    scope: HistoryScope | None = None,
    on_usage: Callable[[ModelIdentity, MessageMetrics], Awaitable[None]] | None = None,
) -> Summarizer:
    """Return a summarizer for the test agent's scope, by default with the production summary prompt."""
    return Summarizer(
        model=SummaryModel(model or summary_model(), name, input_budget_tokens),
        fallback=fallback,
        prompt=prompt,
        timeout_seconds=timeout_seconds,
        history_settings=history_settings,
        session_id=session_id,
        scope=scope or HistoryScope(kind="agent", scope_id="test_agent"),
        on_usage=on_usage,
    )


async def _reported(outcome: CompactionOutcome) -> CompactionOutcome:
    """Stand in for the reply lifecycle's success report."""
    return outcome


@dataclass
class RecordingModel(Model):
    """Model that records the final prompt message list."""

    seen_messages: list[AgnoMessage] = field(default_factory=list)

    def invoke(self, *_args: object, **kwargs: object) -> ModelResponse:
        messages = kwargs.get("messages")
        if isinstance(messages, list):
            self.seen_messages = list(messages)
        return ModelResponse(content="ok")

    async def ainvoke(self, *_args: object, **kwargs: object) -> ModelResponse:
        messages = kwargs.get("messages")
        if isinstance(messages, list):
            self.seen_messages = list(messages)
        return ModelResponse(content="ok")

    def invoke_stream(self, *_args: object, **_kwargs: object):
        yield ModelResponse(content="ok")

    async def ainvoke_stream(self, *_args: object, **_kwargs: object):
        yield ModelResponse(content="ok")

    def _parse_provider_response(self, response: ModelResponse, *_args: object, **_kwargs: object) -> ModelResponse:
        return response

    def _parse_provider_response_delta(
        self,
        response: ModelResponse,
        *_args: object,
        **_kwargs: object,
    ) -> ModelResponse:
        return response


@dataclass
class RecordingCompactionLifecycle:
    """Lifecycle test double that records foreground compaction notice ordering."""

    events: list[object] = field(default_factory=list)
    start_event_id: str | None = "$compaction"

    async def start(self, event: CompactionLifecycleStart) -> str | None:
        self.events.append(event)
        return self.start_event_id

    async def complete_success(self, outcome: CompactionOutcome) -> None:
        self.events.append(outcome)

    async def progress(self, event: CompactionLifecycleProgress) -> None:
        self.events.append(event)

    async def complete_failure(self, event: CompactionLifecycleFailure) -> None:
        self.events.append(event)


def completed_outcome(lifecycle: RecordingCompactionLifecycle) -> CompactionOutcome:
    """Return the one compaction outcome the lifecycle reported as completed."""
    [outcome] = [event for event in lifecycle.events if isinstance(event, CompactionOutcome)]
    return outcome


@dataclass
class FailingStartCompactionLifecycle(RecordingCompactionLifecycle):
    """Lifecycle test double whose initial notice delivery fails."""

    async def start(self, event: CompactionLifecycleStart) -> str | None:
        self.events.append(event)
        message = "matrix unavailable"
        raise RuntimeError(message)


def _runtime_paths(tmp_path: Path) -> RuntimePaths:
    return resolve_runtime_paths(
        config_path=tmp_path / "config.yaml",
        storage_path=tmp_path / "mindroom_data",
        process_env={
            "MATRIX_HOMESERVER": "http://localhost:8008",
            "MINDROOM_NAMESPACE": "",
        },
    )


def _make_config(
    tmp_path: Path,
    *,
    num_history_runs: int | None = None,
    num_history_messages: int | None = None,
    compaction: CompactionOverrideConfig | None = None,
    defaults_compaction: CompactionConfig | None = _DEFAULT_TEST_COMPACTION,
    context_window: int | None = 48_000,
    models: dict[str, ModelConfig] | None = None,
    teams: dict[str, TeamConfig] | None = None,
) -> tuple[Config, RuntimePaths]:
    runtime_paths = _runtime_paths(tmp_path)
    config = bind_runtime_paths(
        Config(
            agents={
                "test_agent": AgentConfig(
                    display_name="Test Agent",
                    num_history_runs=num_history_runs,
                    num_history_messages=num_history_messages,
                    compaction=compaction,
                ),
            },
            teams=teams or {},
            defaults=DefaultsConfig(tools=[], compaction=defaults_compaction),
            models=(
                models
                if models is not None
                else {
                    "default": ModelConfig(
                        provider="openai",
                        id="test-model",
                        context_window=context_window,
                    ),
                }
            ),
        ),
        runtime_paths,
    )
    return config, runtime_paths


def _completed_run(
    run_id: str,
    *,
    agent_id: str = "test_agent",
    messages: list[Message] | None = None,
) -> RunRecord:
    return RunRecord(
        run_id=run_id,
        agent_id=agent_id,
        status=RunStatus.completed,
        messages=messages
        or [
            Message(role="user", content=f"{run_id} question"),
            Message(role="assistant", content=f"{run_id} answer"),
        ],
    )


def _completed_team_run(
    run_id: str,
    *,
    team_id: str,
    messages: list[Message] | None = None,
) -> TeamRunRecord:
    return TeamRunRecord(
        run_id=run_id,
        team_id=team_id,
        status=RunStatus.completed,
        messages=messages
        or [
            Message(role="user", content=f"{run_id} team question"),
            Message(role="assistant", content=f"{run_id} team answer"),
        ],
    )


def _session(
    session_id: str,
    *,
    agent_id: str = "test_agent",
    runs: list[AnyRunRecord] | None = None,
    metadata: dict[str, Any] | None = None,
    summary: ScopeSummary | None = None,
) -> ConversationSession:
    return ConversationSession(
        session_id=session_id,
        kind="agent",
        owner_id=agent_id,
        runs=runs or [],
        metadata=metadata,
        summary=summary,
        created_at=1,
        updated_at=1,
    )


def _team_session(
    session_id: str,
    *,
    team_id: str,
    runs: list[AnyRunRecord] | None = None,
    metadata: dict[str, Any] | None = None,
    summary: ScopeSummary | None = None,
) -> ConversationSession:
    return ConversationSession(
        session_id=session_id,
        kind="team",
        owner_id=team_id,
        runs=runs or [],
        metadata=metadata,
        summary=summary,
        created_at=1,
        updated_at=1,
    )


def _agent(
    *,
    agent_id: str = "test_agent",
    name: str = "Test Agent",
    model: Model | None = None,
    db: object | None = None,
    num_history_runs: int | None = None,
    num_history_messages: int | None = None,
) -> Agent:
    return Agent(
        id=agent_id,
        name=name,
        model=model or FakeModel(id="fake-model", provider="fake"),
        db=db,
        add_history_to_context=True,
        num_history_runs=num_history_runs,
        num_history_messages=num_history_messages,
        store_history_messages=False,
    )


def _hook_runtime_context(
    *,
    config: Config,
    runtime_paths: RuntimePaths,
    registry: HookRegistry,
    session_id: str,
    thread_id: str | None = "$thread",
) -> ToolRuntimeContext:
    return make_test_tool_runtime_context(
        agent_name="test_agent",
        target=MessageTarget(
            room_id="!room:localhost",
            source_thread_id=thread_id,
            resolved_thread_id=thread_id,
            reply_to_event_id=None,
            session_id=session_id,
        ),
        requester_id="@user:localhost",
        client=AsyncMock(),
        config=config,
        runtime_paths=runtime_paths,
        relations=make_relation_lookup(),
        conversation_reader=make_conversation_reader_mock(),
        hook_registry=registry,
        correlation_id="corr-compaction",
    )


def _plugin(name: str, callbacks: list[object]) -> SimpleNamespace:
    return SimpleNamespace(
        name=name,
        discovered_hooks=tuple(callbacks),
        discovered_automations=(),
        entry_config=PluginEntryConfig(path=f"./plugins/{name}"),
        plugin_order=0,
    )


def _forced_compaction_context(
    tmp_path: Path,
    *,
    session: ConversationSession,
    registry: HookRegistry | None = None,
    context_window: int = 64_000,
) -> tuple[Config, RuntimePaths, ConversationStore, HistoryScope, ToolRuntimeContext]:
    config, runtime_paths = _make_config(
        tmp_path,
        compaction=CompactionOverrideConfig(enabled=True),
        context_window=context_window,
    )
    storage = create_session_storage("test_agent", config, runtime_paths, execution_identity=None)
    scope = HistoryScope(kind="agent", scope_id="test_agent")
    session.metadata = _with_scope_state(session.metadata, scope, HistoryScopeState(force_compact_before_next_run=True))
    seed_session(storage, session)
    runtime_context = _hook_runtime_context(
        config=config,
        runtime_paths=runtime_paths,
        registry=registry or HookRegistry.empty(),
        session_id=session.session_id,
    )
    return config, runtime_paths, storage, scope, runtime_context


def folded_run_ids(storage: ConversationStore, session_id: str = "session-1") -> list[str]:
    """Return one session's top-level runs behind their scope's latest summary row, in log order."""
    runs = quote_identifier(storage.names.runs)
    summaries = quote_identifier(storage.names.summaries)
    scope = "CASE WHEN run.run_type = 'team' THEN 'team:' || run.team_id ELSE 'agent:' || run.agent_id END"
    with closing(sqlite3.connect(storage.path)) as connection:
        return [
            row[0]
            for row in connection.execute(
                f"SELECT run.run_id FROM {runs} AS run WHERE run.session_id = ? AND run.parent_run_id IS NULL "  # noqa: S608
                "AND run.status NOT IN ('PAUSED', 'CANCELLED', 'ERROR') AND run.run_index <= ("
                f"SELECT row.after_run_index FROM {summaries} AS row WHERE row.session_id = run.session_id "
                f"AND row.scope_key = {scope} ORDER BY row.id DESC LIMIT 1) ORDER BY run.run_index",
                (session_id,),
            )
        ]


def stored_run_ids(storage: ConversationStore, session_id: str = "session-1") -> list[str]:
    """Return every run row of one session, folded or not, in log order."""
    runs = quote_identifier(storage.names.runs)
    with closing(sqlite3.connect(storage.path)) as connection:
        return [
            row[0]
            for row in connection.execute(
                f"SELECT run_id FROM {runs} WHERE session_id = ? ORDER BY run_index",  # noqa: S608
                (session_id,),
            )
        ]


@dataclass(frozen=True)
class StoredSummaryRow:
    """One summary row as stored."""

    summary: str | None
    summary_model: str | None
    legacy: bool


def summary_rows_of(
    storage: ConversationStore,
    scope_key: str,
    session_id: str = "session-1",
) -> list[StoredSummaryRow]:
    """Return one scope's summary rows, oldest first."""
    table = quote_identifier(storage.names.summaries)
    with closing(sqlite3.connect(storage.path)) as connection:
        return [
            StoredSummaryRow(summary=row[0], summary_model=row[1], legacy=bool(row[2]))
            for row in connection.execute(
                f"SELECT summary, summary_model, legacy FROM {table} "  # noqa: S608
                "WHERE session_id = ? AND scope_key = ? ORDER BY id",
                (session_id, scope_key),
            )
        ]


def log_of(storage: ConversationStore, scope_key: str, session_id: str = "session-1") -> list[str]:
    """Return one scope's top-level runs and summary rows in log order, as ``run:<id>`` and ``row:<summary>``.

    A barrier row reads ``row:None``.
    """
    runs = quote_identifier(storage.names.runs)
    summaries = quote_identifier(storage.names.summaries)
    scope = "CASE WHEN run_type = 'team' THEN 'team:' || team_id ELSE 'agent:' || agent_id END"
    with closing(sqlite3.connect(storage.path)) as connection:
        entries = [
            ((run_index, 0), f"run:{run_id}")
            for run_id, run_index in connection.execute(
                f"SELECT run_id, run_index FROM {runs} WHERE session_id = ? AND parent_run_id IS NULL "  # noqa: S608
                f"AND {scope} = ?",
                (session_id, scope_key),
            )
        ]
        entries += [
            ((after_run_index, row_id), f"row:{summary}")
            for row_id, after_run_index, summary in connection.execute(
                f"SELECT id, after_run_index, summary FROM {summaries} WHERE session_id = ? AND scope_key = ?",  # noqa: S608
                (session_id, scope_key),
            )
        ]
    return [label for _key, label in sorted(entries)]


def segments_of(
    storage: ConversationStore,
    scope_key: str,
    session_id: str = "session-1",
) -> list[tuple[str, dict[str, Any]]]:
    """Return the run and decoded segment of each of one scope's mid-turn fold rows, oldest first."""
    summaries = quote_identifier(storage.names.summaries)
    with closing(sqlite3.connect(storage.path)) as connection:
        return [
            (run_id, json.loads(segment))
            for run_id, segment in connection.execute(
                f"SELECT segment_run_id, segment FROM {summaries} WHERE session_id = ? AND scope_key = ? "  # noqa: S608
                "AND segment_run_id IS NOT NULL ORDER BY id",
                (session_id, scope_key),
            )
        ]


def latest_summary_model(storage: ConversationStore, scope: HistoryScope, session_id: str = "session-1") -> str | None:
    """Return the model recorded for the scope's latest summary row."""
    rows = summary_rows_of(storage, scope.key, session_id)
    return rows[-1].summary_model if rows else None


def compact_runs(
    storage: ConversationStore,
    session: ConversationSession,
    scope: HistoryScope,
    run_ids: list[str],
    summary: str,
    *,
    summary_model: str = "summary-model",
) -> None:
    """Append one compaction chunk folding ``run_ids`` behind ``summary``, as compaction commits a chunk."""
    assert append_compaction_chunk(
        storage=storage,
        session=session,
        scope=scope,
        basis=read_chunk_basis(
            storage,
            session,
            scope,
            run_ids,
            previous_summary=_stored_summary(storage, session, scope),
        ),
        included_run_ids=run_ids,
        summary=summary,
        summary_model=summary_model,
    )


def _stored_summary(storage: ConversationStore, session: ConversationSession, scope: HistoryScope) -> str | None:
    latest = summary_rows.latest_row(storage, session_id=session.session_id, scope_key=scope.key)
    return latest.summary if latest is not None else None


def seed_summary(storage: ConversationStore, session: ConversationSession, scope: HistoryScope, summary: str) -> None:
    """Give ``session`` a summary row that sorts before its runs, as an earlier compaction would have left it."""
    latest = summary_rows.latest_row(storage, session_id=session.session_id, scope_key=scope.key)
    assert summary_rows.append_summary(
        storage,
        session_id=session.session_id,
        scope_key=scope.key,
        based_on=latest.id if latest is not None else None,
        covered=[],
        summary=summary,
        summary_model="summary-model",
    )


def store_fold(
    storage: ConversationStore,
    session_id: str,
    scope: HistoryScope,
    run_id: str,
    *,
    messages: list[dict[str, object]] | None = None,
    tools: list[dict[str, object]] | None = None,
    member_responses: list[dict[str, object]] | None = None,
    metadata: dict[str, object] | None = None,
    summary: str = "FOLD",
) -> int:
    """Append a mid-turn fold's summary row holding ``run_id``'s segment, covering no stored run."""
    storage.ensure_session_row(
        new_scope_session(session_id=session_id, scope_id=scope.scope_id, is_team=scope.kind == "team"),
    )
    latest = summary_rows.latest_row(storage, session_id=session_id, scope_key=scope.key)
    row_id = summary_rows.append_summary(
        storage,
        session_id=session_id,
        scope_key=scope.key,
        based_on=latest.id if latest is not None else None,
        covered=[],
        summary=summary,
        summary_model="summary-model",
        segment=summary_rows.Segment(
            run_id=run_id,
            metadata=metadata or {},
            messages=messages or [],
            tools=tools or [],
            member_responses=member_responses or [],
        ),
    )
    assert row_id is not None
    return row_id


# One large tool result: a few of them outgrow a 4,000-token window.
TOOL_RESULT = "r" * 4000
TEST_AGENT_SCOPE = HistoryScope(kind="agent", scope_id="test_agent")
_TEST_MODEL = ModelIdentity(id="test-model", provider="test", provider_name="test")


@dataclass
class ToolLoopRequest:
    """One request a ``ToolLoopModel`` received, with its roles and contents spelled out."""

    roles: list[str]
    contents: list[str]
    messages: list[Message]
    request: ModelRequest


class ToolLoopModel:
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
        self.native_history_route = None
        self.replay_estimator = None
        self.model_params: dict[str, Any] = {}
        self.tool_rounds = tool_rounds
        self.calls_per_round = calls_per_round
        self.usage = usage
        self.reasoning = reasoning
        self.requests: list[ToolLoopRequest] = []

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
            ToolLoopRequest(
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


@dataclass
class SummaryCalls:
    """A compaction summary call that records its inputs and reports usage; it can fail or be cancelled."""

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


def seeded_storage(config: Config, runtime_paths: RuntimePaths) -> ConversationStore:
    """Return ``test_agent``'s store whose ``session-1`` holds two completed runs."""
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
