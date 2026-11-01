"""Shared helpers for the history test modules."""
# ruff: noqa: D102, TC003

from __future__ import annotations

import asyncio
import json
import re
import sqlite3
from collections.abc import AsyncIterator, Sequence
from contextlib import closing
from dataclasses import dataclass, field
from pathlib import Path
from types import SimpleNamespace
from typing import TYPE_CHECKING, Any
from unittest.mock import AsyncMock

from mindroom.config.agent import AgentConfig, TeamConfig
from mindroom.config.main import Config
from mindroom.config.models import CompactionConfig, CompactionOverrideConfig, DefaultsConfig, ModelConfig
from mindroom.config.plugin import PluginEntryConfig
from mindroom.constants import (
    DEFAULT_COMPACTION_TIMEOUT_SECONDS,
    RuntimePaths,
    resolve_runtime_paths,
)
from mindroom.history import session_runs
from mindroom.history.compaction import Summarizer, SummaryModel
from mindroom.history.storage import (
    append_compaction_chunk,
    new_scope_session,
    read_chunk_basis,
    with_force_compaction,
)
from mindroom.history.summary_input import build_summary_input
from mindroom.history.types import (
    CompactionLifecycleFailure,
    CompactionLifecycleProgress,
    CompactionLifecycleStart,
    CompactionOutcome,
    HistoryPolicy,
    HistoryScope,
    ResolvedHistorySettings,
    owner_scope,
)
from mindroom.hooks import (
    HookBindings,
    HookRegistry,
)
from mindroom.llm.messages import Message
from mindroom.llm.provider import (
    Capabilities,
    ModelIdentity,
    ModelRequest,
    NativeCompactionReplay,
    StreamEvent,
    TextDelta,
    ToolCallReady,
    ToolSpec,
    TurnCompleted,
    UsageReported,
)
from mindroom.llm.runs import RunRecord, RunStatus, TeamRunRecord
from mindroom.llm.usage import MessageMetrics, TokenCounts
from mindroom.message_target import MessageTarget
from mindroom.native_compaction import NATIVE_CHECKPOINT_KEY, native_checkpoint_state
from mindroom.prompts import COMPACTION_SUMMARY_PROMPT
from mindroom.providers.assistant_turn import stamp_model
from mindroom.session_storage import store as store_module
from mindroom.session_storage import summary_rows
from mindroom.session_storage.agent_storage import create_session_storage
from mindroom.session_storage.schema import NON_VISIBLE_RUN_STATUS_SQL, quote_identifier
from mindroom.session_storage.types import ConversationSession
from mindroom.token_budget import estimate_compaction_input_tokens
from tests.authorization_helpers import (
    make_test_tool_runtime_context,
)
from tests.conftest import (
    bind_runtime_paths,
    make_conversation_reader_mock,
    make_relation_lookup,
    persist_session,
    seed_session,
    seed_session_row,
)
from tests.fakes.summary_model import summary_model

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

    import pytest

    from mindroom.history.request_messages import TurnMessages
    from mindroom.history.session_runs import DueConversation
    from mindroom.history.types import ScopeSummary
    from mindroom.llm.provider import ModelProvider
    from mindroom.llm.runs import AnyRunRecord
    from mindroom.session_storage.store import ConversationStore
    from mindroom.tool_system.runtime_context import ToolRuntimeContext


_DEFAULT_TEST_COMPACTION = CompactionConfig()


def stored_turn(turn: TurnMessages, *, requester: str) -> RunRecord:
    """Return the run that stores ``turn`` and the answer "Answered.", each message as a load reads it back."""
    messages = [*turn.in_request_order(), Message(role="assistant", content="Answered.")]
    return RunRecord(
        run_id="stored-turn",
        agent_id="agent",
        user_id=requester,
        messages=[Message.from_dict(message.to_dict()) for message in messages],
    )


def replay_settings(
    policy: HistoryPolicy | None = None,
    *,
    max_tool_calls_from_history: int | None = None,
) -> ResolvedHistorySettings:
    """Return the history settings of an entity that replays under ``policy`` (all history by default).

    The compaction config is the built-in one, as for an entity whose config and defaults author none.
    """
    return ResolvedHistorySettings(
        policy=policy or HistoryPolicy(mode="all"),
        max_tool_calls_from_history=max_tool_calls_from_history,
        compaction_config=CompactionConfig(),
        has_authored_compaction_config=False,
    )


_ALL_HISTORY_SETTINGS = replay_settings()


def summary_tool_work(
    runs: Sequence[AnyRunRecord],
    history_settings: ResolvedHistorySettings,
) -> tuple[list[dict[str, Any]], list[tuple[str, str]]]:
    """Return the tool calls, and each tool result's call ID and text, that a compaction summary of ``runs`` reads."""
    text, _included = build_summary_input(
        previous_summary=None,
        compacted_runs=runs,
        max_input_tokens=100_000,
        history_settings=history_settings,
        token_estimator=estimate_compaction_input_tokens,
    )
    calls = [call for block in re.findall(r"<tool_calls>\n(.*?)\n</tool_calls>", text) for call in json.loads(block)]
    results = re.findall(r'<message role="tool"[^>]*? tool_call_id="([^"]*)"[^>]*>\n(.*?)\n</message>', text, re.DOTALL)
    return calls, results


def checkpointed_answer(item: dict[str, object], basis: str) -> Message:
    """Return an assistant message carrying a provider checkpoint, as native compaction records one."""
    return Message(
        role="assistant",
        content="answer after the checkpoint",
        provider_data={
            "mindroom_native_compaction": {
                "route": "another-model-route",
                "threshold": 50_000,
                "checkpoint_prefix": True,
                "items": [item],
                "basis": basis,
            },
        },
    )


def record_native_checkpoint(
    message: Message,
    items: Sequence[dict[str, Any]],
    settings: NativeCompactionReplay | None,
) -> None:
    """Record on ``message`` the native-compaction state a completed response with output ``items`` stores."""
    message.provider_data = {
        **(message.provider_data or {}),
        NATIVE_CHECKPOINT_KEY: native_checkpoint_state(items, settings),
    }


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


def _included_run_count(previous_summary: str | None, compacted_runs: Sequence[AnyRunRecord], budget: int) -> int:
    """Return how many of ``compacted_runs`` one summary input of ``budget`` tokens holds."""
    return len(
        build_summary_input(
            previous_summary=previous_summary,
            compacted_runs=compacted_runs,
            max_input_tokens=budget,
            history_settings=_ALL_HISTORY_SETTINGS,
            token_estimator=estimate_compaction_input_tokens,
        )[1],
    )


async def _reported(outcome: CompactionOutcome) -> CompactionOutcome:
    """Stand in for the reply lifecycle's success report."""
    return outcome


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
        hooks=HookBindings(registry=registry),
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
    session.metadata = with_force_compaction(session.metadata)
    persist_session(storage, session)
    runtime_context = _hook_runtime_context(
        config=config,
        runtime_paths=runtime_paths,
        registry=registry or HookRegistry.empty(),
        session_id=session.session_id,
    )
    return config, runtime_paths, storage, scope, runtime_context


def folded_run_ids(storage: ConversationStore, session_id: str = "session-1") -> list[str]:
    """Return one session's visible top-level runs behind their scope's latest summary row, in log order."""
    runs = quote_identifier(storage.names.log_runs)
    summaries = quote_identifier(storage.names.log_summaries)
    with closing(sqlite3.connect(storage.path)) as connection:
        return [
            row[0]
            for row in connection.execute(
                f"SELECT run.run_id FROM {runs} AS run WHERE run.session_id = ? AND run.parent_run_id IS NULL "  # noqa: S608
                f"AND run.status NOT IN ({NON_VISIBLE_RUN_STATUS_SQL}) AND run.seq <= ("
                f"SELECT row.through FROM {summaries} AS row WHERE row.session_id = run.session_id "
                "AND row.scope_key = run.scope_key ORDER BY row.id DESC LIMIT 1) ORDER BY run.seq",
                (session_id,),
            )
        ]


def stored_run_ids(storage: ConversationStore, session_id: str = "session-1") -> list[str]:
    """Return every run row of one session, folded or not, in log order."""
    runs = quote_identifier(storage.names.log_runs)
    with closing(sqlite3.connect(storage.path)) as connection:
        return [
            row[0]
            for row in connection.execute(
                f"SELECT run_id FROM {runs} WHERE session_id = ? ORDER BY seq",  # noqa: S608
                (session_id,),
            )
        ]


@dataclass(frozen=True)
class StoredSummaryRow:
    """One summary row as stored; a barrier has no text."""

    summary: str | None
    legacy: bool


def summary_rows_of(
    storage: ConversationStore,
    scope_key: str,
    session_id: str = "session-1",
) -> list[StoredSummaryRow]:
    """Return one scope's summary rows, oldest first."""
    table = quote_identifier(storage.names.log_summaries)
    with closing(sqlite3.connect(storage.path)) as connection:
        return [
            StoredSummaryRow(summary=row[0], legacy=bool(row[1]))
            for row in connection.execute(
                f"SELECT summary, legacy FROM {table} WHERE session_id = ? AND scope_key = ? ORDER BY id",  # noqa: S608
                (session_id, scope_key),
            )
        ]


def log_of(storage: ConversationStore, scope_key: str, session_id: str = "session-1") -> list[str]:
    """Return one scope's top-level runs and summary rows in log order, as ``run:<id>`` and ``row:<summary>``.

    A barrier row reads ``row:None``.
    """
    runs = quote_identifier(storage.names.log_runs)
    summaries = quote_identifier(storage.names.log_summaries)
    with closing(sqlite3.connect(storage.path)) as connection:
        entries = [
            ((seq, 0), f"run:{run_id}")
            for run_id, seq in connection.execute(
                f"SELECT run_id, seq FROM {runs} WHERE session_id = ? AND parent_run_id IS NULL AND scope_key = ?",  # noqa: S608
                (session_id, scope_key),
            )
        ]
        entries += [
            ((through, row_id), f"row:{summary}")
            for row_id, through, summary in connection.execute(
                f"SELECT id, through, summary FROM {summaries} WHERE session_id = ? AND scope_key = ?",  # noqa: S608
                (session_id, scope_key),
            )
        ]
    return [label for _key, label in sorted(entries)]


@dataclass(frozen=True)
class StoredFold:
    """One mid-turn fold's summary row: its running run and what it flagged in that run's rows, as stored."""

    run_id: str
    messages: list[dict[str, Any]]
    member_run_ids: list[str]


def folds_of(storage: ConversationStore, scope_key: str, session_id: str = "session-1") -> list[StoredFold]:
    """Return each of one scope's mid-turn fold rows, oldest first, with the message and member rows it flagged."""
    names = storage.names
    runs, messages = quote_identifier(names.log_runs), quote_identifier(names.log_messages)
    summaries = quote_identifier(names.log_summaries)
    with closing(sqlite3.connect(storage.path)) as connection:
        return [
            StoredFold(
                run_id=run_id,
                messages=[
                    json.loads(data)
                    for (data,) in connection.execute(
                        f"SELECT data FROM {messages} WHERE folded_by = ? ORDER BY ord",  # noqa: S608
                        (row_id,),
                    )
                ],
                member_run_ids=[
                    member_id
                    for (member_id,) in connection.execute(
                        f"SELECT run_id FROM {runs} WHERE folded_by = ? ORDER BY seq",  # noqa: S608
                        (row_id,),
                    )
                ],
            )
            for row_id, run_id in connection.execute(
                f"SELECT id, fold_run_id FROM {summaries} WHERE session_id = ? AND scope_key = ? "  # noqa: S608
                "AND fold_run_id IS NOT NULL ORDER BY id",
                (session_id, scope_key),
            ).fetchall()
        ]


def compact_runs(
    storage: ConversationStore,
    session: ConversationSession,
    run_ids: list[str],
    summary: str,
) -> None:
    """Append one compaction chunk folding ``run_ids`` behind ``summary`` in ``session``'s scope, as compaction does."""
    latest = summary_rows.latest_row(storage, session_id=session.session_id, scope_key=owner_scope(session).key)
    assert append_compaction_chunk(
        storage=storage,
        session=session,
        basis=read_chunk_basis(
            storage,
            session,
            run_ids,
            previous_summary=latest.summary if latest is not None else None,
        ),
        included_run_ids=run_ids,
        summary=summary,
    )


def seed_summary(storage: ConversationStore, session: ConversationSession, scope: HistoryScope, summary: str) -> None:
    """Give ``session`` a summary row that sorts before its runs, as an earlier compaction would have left it."""
    seed_session_row(storage, session)
    latest = summary_rows.latest_row(storage, session_id=session.session_id, scope_key=scope.key)
    assert summary_rows.append_summary(
        storage,
        session_id=session.session_id,
        scope_key=scope.key,
        based_on=latest.id if latest is not None else None,
        covered=[],
        summary=summary,
        session=session,
    )


def store_fold(
    storage: ConversationStore,
    session_id: str,
    scope: HistoryScope,
    run_id: str,
    *,
    tool_call_ids: Sequence[str],
) -> int:
    """Append a mid-turn fold's summary row that took calls ``tool_call_ids`` out of run ``run_id``, covering no run.

    Each call becomes a folded tool-result message of the run, which gets a cancelled row when it has none, as a
    fold stores one.
    """
    if scope.kind == "team":
        run: AnyRunRecord = TeamRunRecord(run_id=run_id, session_id=session_id, team_id=scope.scope_id)
    else:
        run = RunRecord(run_id=run_id, session_id=session_id, agent_id=scope.scope_id)
    latest = summary_rows.latest_row(storage, session_id=session_id, scope_key=scope.key)
    row_id = summary_rows.append_summary(
        storage,
        session_id=session_id,
        scope_key=scope.key,
        based_on=latest.id if latest is not None else None,
        covered=[],
        summary="FOLD",
        fold=summary_rows.Fold(
            run=run,
            messages=tuple(Message(role="tool", tool_call_id=call_id) for call_id in tool_call_ids),
            member_run_ids=frozenset(),
        ),
        initial_session=new_scope_session(session_id=session_id, scope=scope),
    )
    assert row_id is not None
    return row_id


# One large tool result: a few of them outgrow a 4,000-token window.
TOOL_RESULT = "r" * 4000
TEST_AGENT_SCOPE = HistoryScope(kind="agent", scope_id="test_agent")
# One tool definition large enough to dominate a request's size unless tool search defers it.
BIG_TOOL = ToolSpec(
    name="big_tool",
    description="d" * 80_000,
    parameters={},
    strict=None,
)
_TEST_MODEL = ModelIdentity(id="test-model", provider="test")


@dataclass
class ReplayEstimate:
    """A route that sizes every explicit replay at ``tokens`` (None: no estimate), keeping the last replay it sized."""

    tokens: int | None
    uses_visual_tokens: bool = False
    messages: list[Message] = field(default_factory=list)

    def estimate_portable_replay_tokens(self, messages: list[Message]) -> int | None:
        self.messages = messages
        return self.tokens

    def portable_replay_uses_visual_tokens(self) -> bool:
        return self.uses_visual_tokens


@dataclass
class ToolLoopRequest:
    """One request a ``ToolLoopModel`` received, with its roles and contents spelled out."""

    roles: list[str]
    contents: list[str]
    messages: list[Message]
    request: ModelRequest


class ToolLoopModel:
    """Call ``probe`` for ``tool_rounds`` requests, then answer; record every request.

    Each finished turn is stamped with the model's identity, as every adapter stamps the turns it finishes.
    """

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
        stamp_model(message, self.identity)
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
            await on_usage(MessageMetrics(tokens=TokenCounts(input_tokens=7, output_tokens=3)))
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


def unreadable_conversations(unreadable: Callable[[str], bool]) -> Callable[..., DueConversation]:
    """Return a stand-in for ``session_runs._read_conversation`` that fails for each session ``unreadable`` names."""
    real = session_runs._read_conversation

    def read_conversation(
        storage: ConversationStore,
        session_id: str,
        *,
        agent_name: str,
        updated_at: int,
    ) -> DueConversation:
        if unreadable(session_id):
            msg = "corrupt session row"
            raise ValueError(msg)
        return real(storage, session_id, agent_name=agent_name, updated_at=updated_at)

    return read_conversation


def trace_store_statements(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """Return the list that collects every SQL statement the conversation store runs from now on."""
    statements: list[str] = []
    connect = store_module.connect_conversation_database

    def traced_connect(path: Path) -> sqlite3.Connection:
        connection = connect(path)
        connection.set_trace_callback(statements.append)
        return connection

    monkeypatch.setattr(store_module, "connect_conversation_database", traced_connect)
    return statements
