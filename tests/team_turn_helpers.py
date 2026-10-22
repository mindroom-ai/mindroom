"""Team turns on scripted providers: the team runtime seam and the drivers the team suites share.

``install_team_models`` is the team runtime seam. Every provider the production builders build, the members' and the
leader's, is the ``ScriptedModel`` of its configured model name, so a turn runs the real member builds,
``build_team_spec``, ``run_team``, the presentation, and the conversation store. ``TeamTurn`` holds one team's config,
its scripted models, its tool runtime, and its orchestrator, and drives ``team_response``, ``team_response_stream``, and
``continue_paused_team_run`` as the response runner calls them.
"""

from __future__ import annotations

import asyncio
from contextlib import contextmanager
from dataclasses import dataclass, field, replace
from types import SimpleNamespace
from typing import TYPE_CHECKING, Any, ClassVar

from agno.tools import Toolkit

from mindroom import agents
from mindroom.config.main import Config
from mindroom.history.session_context import (
    create_scope_session_storage,
    resolve_bound_history_owner,
    resolve_bound_team_scope_context,
)
from mindroom.history.turn_recorder import TurnRecorder
from mindroom.llm.runs import RunRecord, TeamRunRecord
from mindroom.message_target import MessageTarget
from mindroom.teams import envelope
from mindroom.teams.envelope import continue_paused_team_run, team_response, team_response_stream
from mindroom.teams.spec import TeamMode
from mindroom.tool_system.declarations import ToolFileAccess
from mindroom.tool_system.events import StructuredStreamChunk
from mindroom.tool_system.metadata import TOOL_METADATA, TOOL_REGISTRY, ToolCategory
from mindroom.tool_system.registration import register_tool_with_metadata
from mindroom.tool_system.runtime_context import (
    build_execution_identity_from_runtime_context,
    get_tool_runtime_context,
    tool_runtime_context,
)
from tests.access_schema_support import with_current_room_member_access
from tests.ai_user_id_helpers import _team_orchestrator
from tests.authorization_helpers import make_test_tool_runtime_context
from tests.conftest import (
    bind_runtime_paths,
    make_conversation_reader_mock,
    make_relation_lookup,
    make_turn_context,
    test_runtime_paths,
)
from tests.fakes.scripted_model import ScriptedModel, ScriptStep, calls, tool_call, usage
from tests.identity_helpers import entity_ids

if TYPE_CHECKING:
    from collections.abc import Iterator, Mapping, Sequence
    from pathlib import Path

    import pytest

    from mindroom.constants import RuntimePaths
    from mindroom.event_journal import ApprovalCall
    from mindroom.history.types import HistoryScope
    from mindroom.matrix.identity import MatrixID
    from mindroom.response_turn import CompletedApprovalRun, PausedAttempt, ResponseTurnContext
    from mindroom.session_storage.store import ConversationStore
    from mindroom.teams.envelope import TeamStreamChunk
    from mindroom.tool_system.runtime_context import ToolRuntimeContext
    from mindroom.tool_system.worker_routing import ToolExecutionIdentity

ROOM_ID = "!room:localhost"
THREAD_ID = "$thread:localhost"
REQUESTER_ID = "@user:localhost"
EVENT_ID = "$current:localhost"
SESSION_ID = f"{ROOM_ID}:{THREAD_ID}"
PROBE_TOOL = "team_turn_probe"


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
        context = get_tool_runtime_context()
        key = context.history_scope.key if context is not None and context.history_scope is not None else None
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
    original_registry = TOOL_REGISTRY.copy()
    original_metadata = TOOL_METADATA.copy()

    @register_tool_with_metadata(
        name=PROBE_TOOL,
        file_access=ToolFileAccess.NONE,
        display_name="Team Turn Probe",
        description="Records the calls a team member makes.",
        category=ToolCategory.DEVELOPMENT,
    )
    def probe_factory() -> type[ProbeToolkit]:
        return ProbeToolkit

    ProbeToolkit.calls.clear()
    ProbeToolkit.scopes.clear()
    ProbeToolkit.active_models.clear()
    ProbeToolkit.waiting = asyncio.Event()
    try:
        yield ProbeToolkit
    finally:
        TOOL_REGISTRY.clear()
        TOOL_REGISTRY.update(original_registry)
        TOOL_METADATA.clear()
        TOOL_METADATA.update(original_metadata)


def install_team_models(monkeypatch: pytest.MonkeyPatch, models: Mapping[str, ScriptedModel]) -> None:
    """Install the team runtime seam: each member's and the leader's provider is the scripted model of its model name.

    ``models`` is read at every build, so a test may replace a model between a pause and its resume.
    """
    monkeypatch.setattr(agents, "_build_agent_model_provider", lambda _config, _paths, name, _identity: models[name])
    monkeypatch.setattr(envelope, "build_model_provider", lambda _config, _paths, name, **_kwargs: models[name])


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


@dataclass
class TeamTurn:
    """One team's config, scripted models by model name, tool runtime, orchestrator, and turn recorder."""

    config: Config
    runtime_paths: RuntimePaths
    models: dict[str, ScriptedModel]
    identity: ToolExecutionIdentity
    runtime: ToolRuntimeContext
    orchestrator: SimpleNamespace
    member_names: tuple[str, ...]
    team_name: str | None
    mode: TeamMode
    scope: HistoryScope
    recorder: TurnRecorder = field(default_factory=lambda: TurnRecorder(user_message="Hello"))

    @property
    def storage(self) -> ConversationStore:
        """The store of the team's history scope."""
        owner = resolve_bound_history_owner(self.member_names)
        assert owner is not None
        return create_scope_session_storage(
            agent_name=owner,
            scope=self.scope,
            config=self.config,
            runtime_paths=self.runtime_paths,
            execution_identity=self.identity,
        )

    def member_ids(self) -> list[MatrixID]:
        """Return the members' Matrix ids, as the streaming entry receives them."""
        ids = entity_ids(self.config, self.runtime_paths)
        return [ids[name] for name in self.member_names]

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

    def stored_runs(self, session_id: str = SESSION_ID) -> tuple[TeamRunRecord, list[RunRecord]]:
        """Return the session's one team row and its member rows."""
        session = self.storage.load_session(session_id, "team")
        assert session is not None
        [record] = [run for run in session.runs if isinstance(run, TeamRunRecord)]
        return record, [run for run in session.runs if isinstance(run, RunRecord)]

    async def stream(
        self,
        *,
        message: str = "Hello",
        ctx: ResponseTurnContext | None = None,
        **kwargs: Any,  # noqa: ANN401
    ) -> list[TeamStreamChunk]:
        """Stream one team reply as the response runner does and return its chunks."""
        kwargs.setdefault("mode", self.mode)
        kwargs.setdefault("user_id", REQUESTER_ID)
        kwargs.setdefault("agent_ids", self.member_ids())
        with tool_runtime_context(self.runtime):
            return [
                chunk
                async for chunk in team_response_stream(
                    message=message,
                    orchestrator=self.orchestrator,
                    execution_identity=self.identity,
                    ctx=ctx or self.ctx(),
                    configured_team_name=self.team_name,
                    turn_recorder=self.recorder,
                    **kwargs,
                )
            ]

    async def respond(
        self,
        *,
        message: str = "Hello",
        ctx: ResponseTurnContext | None = None,
        **kwargs: Any,  # noqa: ANN401
    ) -> str:
        """Run one blocking team reply and return its text."""
        kwargs.setdefault("mode", self.mode)
        kwargs.setdefault("user_id", REQUESTER_ID)
        kwargs.setdefault("agent_names", list(self.member_names))
        with tool_runtime_context(self.runtime):
            return await team_response(
                message=message,
                orchestrator=self.orchestrator,
                execution_identity=self.identity,
                ctx=ctx or self.ctx(),
                configured_team_name=self.team_name,
                turn_recorder=self.recorder,
                **kwargs,
            )

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
        **kwargs: Any,  # noqa: ANN401
    ) -> CompletedApprovalRun | PausedAttempt:
        """Resume ``paused`` with every saved approval call approved or denied, as an approval reaction does."""
        call_ids = [call.tool_call_id for call in approvals]
        kwargs.setdefault("decisions", dict.fromkeys(call_ids, approved))
        kwargs.setdefault("denial_reasons", dict.fromkeys(call_ids, "Declined by requester"))
        kwargs.setdefault("model_name", "default")
        kwargs.setdefault("show_tool_calls", False)
        kwargs.setdefault("progress", None)
        with tool_runtime_context(self.runtime):
            return await continue_paused_team_run(
                member_names=self.member_names,
                mode=self.mode,
                config=self.config,
                runtime_paths=self.runtime_paths,
                execution_identity=self.identity,
                session_id=SESSION_ID,
                run_id=paused.run_id,
                user_id=REQUESTER_ID,
                configured_team_name=self.team_name or self.member_names[0],
                refresh_scheduler=None,
                approval_calls=approvals,
                history_scope=self.scope,
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
    """Build one team of ``members`` over ``authored``, every configured model scripted by ``scripts`` or left empty.

    ``team_name`` names the configured team the turn runs as, or None for an ad hoc team of ``members``; every agent
    of the config runs, and the requester may address every responder in the room.
    """
    runtime_paths = test_runtime_paths(tmp_path)
    config = bind_runtime_paths(Config.model_validate(authored), runtime_paths)
    with_current_room_member_access(config)
    models = {name: ScriptedModel([list(turn) for turn in (scripts or {}).get(name, ())]) for name in config.models}
    install_team_models(monkeypatch, models)
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
    bound = resolve_bound_team_scope_context(
        member_names=members,
        config=config,
        team_name=team_name,
        execution_identity=identity,
    )
    assert bound is not None
    orchestrator = _team_orchestrator(config, runtime_paths)
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
        scope=bound.scope,
    )


__all__ = [
    "EVENT_ID",
    "PROBE_TOOL",
    "REQUESTER_ID",
    "ROOM_ID",
    "SESSION_ID",
    "THREAD_ID",
    "ProbeToolkit",
    "TeamTurn",
    "authored_team_config",
    "build_team_turn",
    "delegate",
    "install_team_models",
    "registered_probe",
    "rendered",
]
