"""Team turns on scripted providers: the drivers the team suites share.

``build_team_turn`` installs a ``ScriptedModel`` for each configured model name, so the members and the leader build
scripted providers and a turn runs the real member builds, ``build_team_spec``, ``run_team``, the presentation, and the
conversation store. ``TeamTurn`` holds one team's config, its scripted models, its tool runtime, and its orchestrator,
and drives ``team_response_stream`` (streamed, or collected by ``collect_team_reply``) and ``continue_paused_team_run``
as the response runner calls them. ``squad_turn`` builds the ``squad`` team of a writer and a checker that the team runtime, compaction, and history
suites share; ``ProbeToolkit`` is its members' tool.
"""

from __future__ import annotations

import asyncio
from contextlib import contextmanager
from dataclasses import dataclass, field, fields, replace
from types import SimpleNamespace
from typing import TYPE_CHECKING, Any, ClassVar
from unittest.mock import MagicMock

from agno.tools import Toolkit

from mindroom.config.approval import ApprovalRuleConfig, ToolApprovalConfig
from mindroom.config.main import Config
from mindroom.event_journal import ApprovalCall, approval_arguments_digest
from mindroom.history.session_context import create_scope_session_storage, resolve_bound_team_scope
from mindroom.history.turn_recorder import TurnRecorder
from mindroom.history.types import HistoryScope
from mindroom.llm.runs import RunRecord, TeamRunRecord
from mindroom.message_target import MessageTarget
from mindroom.response_turn import TurnSinks, collect_streamed_response_content
from mindroom.streaming import ReplacementStreamingResponse
from mindroom.teams.envelope import TeamTurnSpec, team_response_stream
from mindroom.teams.resume import continue_paused_team_run
from mindroom.teams.spec import TeamMode
from mindroom.tool_system.events import CollectedStreamPresentation, StructuredStreamChunk
from mindroom.tool_system.invocation import current_tool_call
from mindroom.tool_system.runtime_context import (
    build_execution_identity_from_runtime_context,
    get_tool_runtime_context,
    tool_runtime_context,
)
from tests.access_schema_support import with_current_room_member_access, with_responder_access
from tests.ai_user_id_helpers import team_orchestrator
from tests.approval_continuation_helpers import decided_calls
from tests.authorization_helpers import make_test_tool_runtime_context
from tests.conftest import (
    bind_runtime_paths,
    install_model_providers,
    make_conversation_reader_mock,
    make_relation_lookup,
    make_turn_context,
    test_runtime_paths,
)
from tests.fakes.scripted_model import ScriptedModel, ScriptStep, calls, tool_call, usage
from tests.scripted_agent_helpers import registered_toolkit
from tests.turn_model_helpers import team_member_models

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Iterator, Mapping, Sequence
    from pathlib import Path

    import pytest

    from mindroom.constants import RuntimePaths
    from mindroom.response_turn import CompletedApprovalRun, PausedAttempt, ResponseTurnContext
    from mindroom.session_storage.store import ConversationStore
    from mindroom.teams.envelope import TeamStreamChunk
    from mindroom.tool_system.events import ToolTraceEntry
    from mindroom.tool_system.runtime_context import ToolRuntimeContext
    from mindroom.tool_system.worker_routing import ToolExecutionIdentity

ROOM_ID = "!room:localhost"
THREAD_ID = "$thread:localhost"
REQUESTER_ID = "@user:localhost"
EVENT_ID = "$current:localhost"
SESSION_ID = f"{ROOM_ID}:{THREAD_ID}"
PROBE_TOOL = "team_turn_probe"
TEAM_SCOPE = HistoryScope(kind="team", scope_id="squad")
_SPEC_OPTIONS = frozenset(spec_field.name for spec_field in fields(TeamTurnSpec))
_SINK_OPTIONS = frozenset(sink_field.name for sink_field in fields(TurnSinks))


class ProbeToolkit(Toolkit):
    """The member tools the team suites watch: calls are recorded, and a waiting call runs until it is cancelled."""

    calls: ClassVar[list[str]] = []
    scopes: ClassVar[list[str | None]] = []
    active_models: ClassVar[list[str | None]] = []
    waiting: ClassVar[asyncio.Event] = asyncio.Event()

    def __init__(self) -> None:
        super().__init__(name="probe", tools=[self.touch, self.scope, self.dig, self.model])

    async def touch(self, label: str, wait: bool = False) -> str:
        """Record one call; a waiting call then runs until it is cancelled.

        Args:
            label: What to record.
            wait: Whether the call keeps running after it is recorded.

        """
        type(self).calls.append(label)
        if wait:
            type(self).waiting.set()
            await asyncio.Event().wait()
        return f"touched {label}"

    async def scope(self) -> str:
        """Report the history scope the running member stores to."""
        call = current_tool_call()
        key = call.context.history_scope.key if call is not None and call.context.history_scope is not None else None
        type(self).scopes.append(key)
        return key or "no scope"

    async def dig(self) -> str:
        """Return one large result, so a member's tool loop outgrows its context window."""
        return "r" * 4000

    async def model(self) -> str:
        """Report the model the running turn's tool context names."""
        context = get_tool_runtime_context()
        name = context.active_model_name if context is not None else None
        type(self).active_models.append(name)
        return name or "no model"


@contextmanager
def registered_probe() -> Iterator[type[ProbeToolkit]]:
    """Register the probe toolkit as ``PROBE_TOOL`` with cleared records, and restore the registries afterwards."""
    ProbeToolkit.calls.clear()
    ProbeToolkit.scopes.clear()
    ProbeToolkit.active_models.clear()
    ProbeToolkit.waiting = asyncio.Event()
    with registered_toolkit(PROBE_TOOL, ProbeToolkit):
        yield ProbeToolkit


def authored_team_config(
    agents_: Mapping[str, Mapping[str, Any]],
    *,
    teams: Mapping[str, Mapping[str, Any]] | None = None,
    models: Sequence[str] = ("default",),
) -> dict[str, Any]:
    """Return an authored config of these agents and teams, each model name a test provider, without default tools."""
    return {
        "defaults": {"tools": []},
        "agents": {name: dict(agent) for name, agent in agents_.items()},
        "teams": {name: dict(team) for name, team in (teams or {}).items()},
        "memory": {"backend": "none"},
        "models": {
            name: {"provider": "openai", "id": "test-model", "extra_kwargs": {"api_key": "test-key"}} for name in models
        },
        "timezone": "UTC",
    }


def delegate(call_id: str, member_id: str, task: str) -> list[ScriptStep]:
    """Return a leader turn that hands ``task`` to one member in coordinate mode."""
    return calls(tool_call("delegate_task_to_member", call_id, member_id=member_id, task=task), metrics=usage(40, 4))


def rendered(chunk: TeamStreamChunk) -> str:
    """Return the text one streamed chunk shows."""
    return chunk.content if isinstance(chunk, StructuredStreamChunk) else chunk


def scripted_team_stream(*chunks: str, completes: bool = False) -> MagicMock:
    """Return a ``team_response_stream`` stand-in that yields ``chunks``, whose calls name each turn's spec and context.

    A completing stream records its turn completed, as the turn driver it stands in for does before its last chunk.
    """

    async def stream(_spec: TeamTurnSpec, _ctx: ResponseTurnContext, *, sinks: TurnSinks) -> AsyncIterator[str]:
        if completes:
            assert sinks.turn_recorder is not None
            sinks.turn_recorder.record_completed(
                run_id=None,
                run_metadata=None,
                assistant_text="".join(chunks),
                completed_tools=(),
            )
        for chunk in chunks:
            yield chunk

    return MagicMock(side_effect=stream)


async def collect_team_reply(
    spec: TeamTurnSpec,
    ctx: ResponseTurnContext,
    *,
    sinks: TurnSinks,
    tool_trace: list[ToolTraceEntry] | None = None,
) -> str:
    """Collect one team turn's stream into the text it delivers whole, as the response runner collects it.

    ``tool_trace`` receives the collected tool trace, as the runner's does.
    """
    body, collected_trace = await collect_streamed_response_content(
        team_response_stream(spec, ctx, sinks=sinks),
        presentation=CollectedStreamPresentation(
            show_tool_calls=ctx.show_tool_calls,
            track_hidden_tools=True,
            replaces_text=ReplacementStreamingResponse.replaces_text,
        ),
    )
    if tool_trace is not None:
        tool_trace.extend(collected_trace)
    return body


@dataclass
class TeamTurn:
    """One team's config, scripted models by model name, tool runtime, orchestrator, and its last turn's recorder.

    Each ``stream`` or ``respond`` records into a new ``recorder`` of its own message, as the runner records each turn.
    """

    config: Config
    runtime_paths: RuntimePaths
    models: dict[str, ScriptedModel]
    # The execution identity every entry passes; None runs the turn without one.
    identity: ToolExecutionIdentity | None
    runtime: ToolRuntimeContext
    orchestrator: SimpleNamespace
    member_names: tuple[str, ...]
    team_name: str | None
    mode: TeamMode
    scope: HistoryScope
    recorder: TurnRecorder = field(init=False)

    @property
    def storage(self) -> ConversationStore:
        """The store of the team's history scope."""
        return create_scope_session_storage(
            scope=self.scope,
            config=self.config,
            runtime_paths=self.runtime_paths,
            execution_identity=self.identity,
        )

    def ctx(self, **overrides: Any) -> ResponseTurnContext:  # noqa: ANN401
        """Return the turn context the runner passes for a reply to ``EVENT_ID`` in the thread."""
        ctx = make_turn_context(
            self.team_name or "team",
            session_id=SESSION_ID,
            run_id="run-1",
            correlation_id="correlation-1",
            reply_to_event_id=EVENT_ID,
            room_id=ROOM_ID,
            thread_id=THREAD_ID,
            requester_id=REQUESTER_ID,
        )
        return replace(ctx, **overrides)

    def stored_runs(
        self,
        session_id: str = SESSION_ID,
        *,
        run_id: str | None = None,
    ) -> tuple[TeamRunRecord, list[RunRecord]]:
        """Return the session's one team row and every member row, or the row of ``run_id`` and its member rows.

        Without ``run_id`` no member row is filtered out, so a stray row is still visible to negative checks.
        """
        session = self.storage.load_session(session_id)
        assert session is not None
        [record] = [run for run in session.runs if isinstance(run, TeamRunRecord) and run_id in (None, run.run_id)]
        members = [
            run
            for run in session.runs
            if isinstance(run, RunRecord) and (run_id is None or run.parent_run_id == record.run_id)
        ]
        return record, members

    def turn_inputs(
        self,
        message: str,
        ctx: ResponseTurnContext | None,
        options: dict[str, Any],
    ) -> tuple[TeamTurnSpec, ResponseTurnContext, TurnSinks]:
        """Split one reply's options into its team, its turn inputs, and its sinks, as the runner builds them.

        ``user_id`` names the requester; every option that names neither the team nor a sink is a turn input.
        """
        self.recorder = TurnRecorder(user_message=message)
        spec_options = {name: options.pop(name) for name in _SPEC_OPTIONS & options.keys()}
        sinks = TurnSinks(
            turn_recorder=self.recorder,
            **{name: options.pop(name) for name in _SINK_OPTIONS & options.keys()},
        )
        if "user_id" in options:
            options["requester_id"] = options.pop("user_id")
        turn_ctx = replace(
            ctx or self.ctx(),
            **{"prompt": message, "execution_identity": self.identity} | options,
        )
        spec = TeamTurnSpec(
            **{
                "agent_names": list(self.member_names),
                "mode": self.mode,
                "orchestrator": self.orchestrator,
                "member_model_names": team_member_models(
                    self.config,
                    self.runtime_paths,
                    self.team_name,
                    self.member_names,
                    room_id=turn_ctx.room_id,
                    thread_id=turn_ctx.thread_id,
                ),
                "configured_team_name": self.team_name,
            }
            | spec_options,
        )
        return spec, turn_ctx, sinks

    async def stream(
        self,
        *,
        message: str = "Hello",
        ctx: ResponseTurnContext | None = None,
        chunks: list[TeamStreamChunk] | None = None,
        **kwargs: Any,  # noqa: ANN401
    ) -> list[TeamStreamChunk]:
        """Stream one team reply as the response runner does and return its chunks.

        ``chunks`` collects them as they arrive, so a reply that pauses, which raises, keeps the chunks it sent first.
        """
        spec, turn_ctx, sinks = self.turn_inputs(message, ctx, kwargs)
        collected = [] if chunks is None else chunks
        with tool_runtime_context(self.runtime):
            async for chunk in team_response_stream(spec, turn_ctx, sinks=sinks):
                collected.append(chunk)
        return collected

    async def respond(
        self,
        *,
        message: str = "Hello",
        ctx: ResponseTurnContext | None = None,
        tool_trace: list[ToolTraceEntry] | None = None,
        **kwargs: Any,  # noqa: ANN401
    ) -> str:
        """Run one team reply collected into its text, as a reply delivered without streaming is.

        ``tool_trace`` receives the reply's collected tool trace.
        """
        spec, turn_ctx, sinks = self.turn_inputs(message, ctx, kwargs)
        with tool_runtime_context(self.runtime):
            return await collect_team_reply(spec, turn_ctx, sinks=sinks, tool_trace=tool_trace)

    async def reply(self, *, stream: bool, **kwargs: Any) -> str:  # noqa: ANN401
        """Run one reply on either path and return the text it ended with: the last streamed chunk, or the reply."""
        if not stream:
            return await self.respond(**kwargs)
        chunks = await self.stream(**kwargs)
        return rendered(chunks[-1]) if chunks else ""

    async def resume(
        self,
        paused: PausedAttempt,
        *,
        approved: bool,
        approvals: Sequence[ApprovalCall],
        session_id: str = SESSION_ID,
        **kwargs: Any,  # noqa: ANN401
    ) -> CompletedApprovalRun | PausedAttempt:
        """Resume ``paused`` with every saved approval call approved or denied, as an approval from ``session_id`` does.

        The leader and the members run on the models the pause pinned, as the runner resumes them, in the team's scope
        unless ``history_scope`` names the scope the approval retained, or None for an approval that retained none.
        """
        call_ids = [call.tool_call_id for call in approvals]
        reasons = dict.fromkeys(call_ids, "Declined by requester")
        decided = decided_calls(approvals, dict.fromkeys(call_ids, approved), reasons)
        kwargs.setdefault("model_name", paused.runtime_model_name or "default")
        kwargs.setdefault("member_model_names", dict(paused.team_member_model_names) or None)
        kwargs.setdefault("show_tool_calls", False)
        kwargs.setdefault("tool_trace_collector", [])
        kwargs.setdefault("progress", None)
        kwargs.setdefault("history_scope", self.scope)
        with tool_runtime_context(self.runtime):
            return await continue_paused_team_run(
                member_names=self.member_names,
                mode=self.mode,
                config=self.config,
                runtime_paths=self.runtime_paths,
                execution_identity=self.identity,
                session_id=session_id,
                run_id=paused.run_id,
                user_id=REQUESTER_ID,
                configured_team_name=self.team_name or self.member_names[0],
                refresh_scheduler=None,
                approval_calls=decided,
                prior_tool_trace=paused.tool_trace,
                prior_presentation_state=paused.response_presentation_state,
                **kwargs,
            )


def build_team_turn(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    authored: Mapping[str, Any],
    *,
    members: Sequence[str],
    team_name: str | None = None,
    scripts: Mapping[str, Sequence[Sequence[ScriptStep]]] | None = None,
    mode: TeamMode = TeamMode.COORDINATE,
) -> TeamTurn:
    """Build one team of ``members`` over ``authored``, each non-compaction model scripted by ``scripts`` or empty.

    The compaction model builds as the test installed it before, as a ``summary_calls`` fixture does; without such an
    install, building it raises. ``team_name`` names the configured team the turn runs as, or None for an ad hoc team of ``members``; every agent
    of the config runs, and the requester may address every responder in the room.
    """
    runtime_paths = test_runtime_paths(tmp_path)
    config = bind_runtime_paths(Config.model_validate(authored), runtime_paths)
    with_current_room_member_access(config)
    compaction = config.defaults.compaction
    compaction_model = compaction.model if compaction is not None else None
    models = {
        name: ScriptedModel([list(turn) for turn in (scripts or {}).get(name, ())])
        for name in config.models
        if name != compaction_model
    }
    install_model_providers(monkeypatch, models)
    runtime = make_test_tool_runtime_context(
        agent_name=team_name or members[0],
        target=MessageTarget(
            room_id=ROOM_ID,
            source_thread_id=THREAD_ID,
            resolved_thread_id=THREAD_ID,
            reply_to_event_id=EVENT_ID,
            session_id=SESSION_ID,
        ),
        requester_id=REQUESTER_ID,
        client=SimpleNamespace(user_id=f"@mindroom_{team_name or members[0]}:localhost"),
        config=config,
        runtime_paths=runtime_paths,
        relations=make_relation_lookup(),
        conversation_reader=make_conversation_reader_mock(),
        correlation_id="correlation-1",
        membership_turn_id=EVENT_ID,
    )
    identity = build_execution_identity_from_runtime_context(runtime)
    scope = resolve_bound_team_scope(
        member_names=members,
        config=config,
        team_name=team_name,
        requester_user_id=identity.requester_id,
    )
    orchestrator = team_orchestrator(config, runtime_paths)
    orchestrator.agent_bots = {name: SimpleNamespace(running=True) for name in config.agents}
    return TeamTurn(
        config=config,
        runtime_paths=runtime_paths,
        models=models,
        identity=identity,
        runtime=runtime,
        orchestrator=orchestrator,
        member_names=tuple(members),
        team_name=team_name,
        mode=mode,
        scope=scope,
    )


def squad_turn(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    leader: Sequence[Sequence[ScriptStep]],
    writer: Sequence[Sequence[ScriptStep]] = (),
    checker: Sequence[Sequence[ScriptStep]] = (),
    helper: Sequence[Sequence[ScriptStep]] | None = None,
    mode: str = "coordinate",
    approval_rules: Sequence[ApprovalRuleConfig] = (),
    context_window: int | None = None,
    max_tool_calls_per_turn: int | None = None,
) -> TeamTurn:
    """Build the ``squad`` team of a writer and a checker whose models are scripted providers.

    The leader's model is ``default``, the writer's ``writer-model``, the checker's ``checker-model``, and every
    member offers ``PROBE_TOOL``. ``helper`` scripts the ``helper`` agent, outside the team, that the writer may then
    delegate to. A ``context_window`` turns on compaction with a ``summary`` model, which the test supplies.
    """
    authored = authored_team_config(
        {
            "writer": {"display_name": "Writer", "role": "Writes.", "model": "writer-model", "tools": [PROBE_TOOL]},
            "checker": {"display_name": "Checker", "role": "Checks.", "model": "checker-model", "tools": [PROBE_TOOL]},
            "helper": {"display_name": "Helper", "role": "Helps.", "model": "helper-model", "tools": [PROBE_TOOL]},
        },
        teams={"squad": {"display_name": "Squad", "role": "Answers.", "agents": ["writer", "checker"], "mode": mode}},
        models=("default", "writer-model", "checker-model", "helper-model"),
    )
    if helper is not None:
        authored["agents"]["writer"]["delegate_to"] = ["helper"]
    if max_tool_calls_per_turn is not None:
        authored["teams"]["squad"]["max_tool_calls_per_turn"] = max_tool_calls_per_turn
    if context_window is not None:
        for model in authored["models"].values():
            model["context_window"] = context_window
        authored["models"]["summary"] = {"provider": "openai", "id": "summary-model", "context_window": 100_000}
        authored["defaults"]["compaction"] = {"model": "summary", "reserve_tokens": 500}
    team = build_team_turn(
        tmp_path,
        monkeypatch,
        authored,
        members=("writer", "checker"),
        team_name="squad",
        scripts={"default": leader, "writer-model": writer, "checker-model": checker, "helper-model": helper or ()},
        mode=TeamMode(mode),
    )
    with_responder_access(team.config, "helper", users=[REQUESTER_ID])
    if approval_rules:
        team.config.tool_approval = ToolApprovalConfig(rules=list(approval_rules))
    return team


def member_approval(call_id: str, label: str, *, agent: str = "checker") -> ApprovalCall:
    """Return the approval of a squad member's ``touch`` call with ``label``, the checker's unless ``agent`` names another."""
    return ApprovalCall(
        tool_call_id=call_id,
        tool_name="touch",
        invoking_agent=agent,
        toolkit_name=PROBE_TOOL,
        expires_at_ns=2**62,
        arguments_digest=approval_arguments_digest({"label": label}),
    )


__all__ = [
    "EVENT_ID",
    "PROBE_TOOL",
    "REQUESTER_ID",
    "ROOM_ID",
    "SESSION_ID",
    "TEAM_SCOPE",
    "THREAD_ID",
    "ProbeToolkit",
    "TeamTurn",
    "authored_team_config",
    "build_team_turn",
    "collect_team_reply",
    "delegate",
    "member_approval",
    "registered_probe",
    "rendered",
    "scripted_team_stream",
    "squad_turn",
]
