"""The ``squad`` team turn harness: a writer and a checker behind the production team envelope, on scripted models.

``squad_turn`` builds the team's config, its scripted providers by model name, its store, and its tool runtime;
``stream_reply`` and ``blocking_reply`` run one reply through ``team_response_stream`` and ``team_response``, and
``resume_approval`` resumes a member's approval through ``continue_paused_team_run``. ``ProbeToolkit`` is the
members' tool, which records its calls and the history scope a member's call addresses.
"""

from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass
from types import SimpleNamespace
from typing import TYPE_CHECKING, Any, ClassVar

import pytest
from agno.tools import Toolkit

from mindroom import agents
from mindroom.config.approval import ApprovalRuleConfig, ToolApprovalConfig
from mindroom.config.main import Config
from mindroom.event_journal import ApprovalCall, approval_arguments_digest
from mindroom.history.session_context import create_scope_session_storage
from mindroom.history.turn_recorder import TurnRecorder
from mindroom.history.types import HistoryScope
from mindroom.llm.runs import RunRecord, TeamRunRecord
from mindroom.message_target import MessageTarget
from mindroom.teams import envelope
from mindroom.teams.envelope import continue_paused_team_run, team_response, team_response_stream
from mindroom.teams.spec import TeamMode
from mindroom.tool_system.declarations import ToolFileAccess
from mindroom.tool_system.metadata import TOOL_METADATA, TOOL_REGISTRY, ToolCategory
from mindroom.tool_system.registration import register_tool_with_metadata
from mindroom.tool_system.runtime_context import (
    build_execution_identity_from_runtime_context,
    get_tool_runtime_context,
    tool_runtime_context,
)
from tests.access_schema_support import with_current_room_member_access, with_responder_access
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
    from collections.abc import Iterator, Sequence
    from pathlib import Path

    from mindroom.constants import RuntimePaths
    from mindroom.matrix.identity import MatrixID
    from mindroom.response_turn import CompletedApprovalRun, PausedAttempt, ResponseTurnContext
    from mindroom.session_storage.store import ConversationStore
    from mindroom.tool_system.runtime_context import ToolRuntimeContext
    from mindroom.tool_system.worker_routing import ToolExecutionIdentity

ROOM_ID = "!room:localhost"
THREAD_ID = "$thread:localhost"
REQUESTER_ID = "@user:localhost"
EVENT_ID = "$current:localhost"
SESSION_ID = f"{ROOM_ID}:{THREAD_ID}"
PROBE_TOOL = "team_turn_probe"
TEAM_SCOPE = HistoryScope(kind="team", scope_id="squad")


class ProbeToolkit(Toolkit):
    """Two member tools the tests watch: one records its calls, one reports the history scope its runtime context names."""

    calls: ClassVar[list[str]] = []
    scopes: ClassVar[list[str | None]] = []

    def __init__(self) -> None:
        super().__init__(name="probe", tools=[self.touch, self.scope, self.dig])

    async def touch(self, label: str) -> str:
        """Record one call.

        Args:
            label: What to record.

        """
        type(self).calls.append(label)
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


@contextmanager
def registered_probe() -> Iterator[type[ProbeToolkit]]:
    """Register the probe toolkit and restore the registries afterwards."""
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
    try:
        yield ProbeToolkit
    finally:
        TOOL_REGISTRY.clear()
        TOOL_REGISTRY.update(original_registry)
        TOOL_METADATA.clear()
        TOOL_METADATA.update(original_metadata)


@pytest.fixture
def squad_probe() -> Iterator[type[ProbeToolkit]]:
    """Register the members' probe toolkit for the test."""
    with registered_probe() as toolkit:
        yield toolkit


@dataclass
class SquadTurn:
    """One team turn's config, its scripted models by model name, its recorder, store, and runtime."""

    config: Config
    runtime_paths: RuntimePaths
    models: dict[str, ScriptedModel]
    recorder: TurnRecorder
    storage: ConversationStore
    identity: ToolExecutionIdentity
    runtime: ToolRuntimeContext
    orchestrator: SimpleNamespace

    @property
    def leader(self) -> ScriptedModel:
        """Return the leader's scripted model."""
        return self.models["default"]

    @property
    def writer(self) -> ScriptedModel:
        """Return the writer's scripted model."""
        return self.models["writer-model"]

    @property
    def checker(self) -> ScriptedModel:
        """Return the checker's scripted model."""
        return self.models["checker-model"]

    def member_ids(self) -> list[MatrixID]:
        """Return the members' Matrix ids, as a team reply names them."""
        ids = entity_ids(self.config, self.runtime_paths)
        return [ids["writer"], ids["checker"]]


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
) -> SquadTurn:
    """Build the ``squad`` team of a writer and a checker whose models are scripted providers.

    ``helper`` scripts the ``helper`` agent, outside the team, that the writer may then delegate to. A
    ``context_window`` turns on compaction with a ``summary`` model, which the test supplies.
    """
    authored = {
        "defaults": {"tools": []},
        "agents": {
            "writer": {"display_name": "Writer", "role": "Writes.", "model": "writer-model", "tools": [PROBE_TOOL]},
            "checker": {"display_name": "Checker", "role": "Checks.", "model": "checker-model", "tools": [PROBE_TOOL]},
            "helper": {"display_name": "Helper", "role": "Helps.", "model": "helper-model", "tools": [PROBE_TOOL]},
        },
        "teams": {
            "squad": {"display_name": "Squad", "role": "Answers.", "agents": ["writer", "checker"], "mode": mode},
        },
        "memory": {"backend": "none"},
        "models": {
            name: {"provider": "openai", "id": "test-model", "extra_kwargs": {"api_key": "test-key"}}
            for name in ("default", "writer-model", "checker-model", "helper-model")
        },
        "timezone": "UTC",
    }
    if helper is not None:
        authored["agents"]["writer"]["delegate_to"] = ["helper"]
    if max_tool_calls_per_turn is not None:
        authored["teams"]["squad"]["max_tool_calls_per_turn"] = max_tool_calls_per_turn
    if context_window is not None:
        for model in authored["models"].values():
            model["context_window"] = context_window
        authored["models"]["summary"] = {"provider": "openai", "id": "summary-model", "context_window": 100_000}
        authored["defaults"]["compaction"] = {"model": "summary", "reserve_tokens": 500}
    runtime_paths = test_runtime_paths(tmp_path)
    config = bind_runtime_paths(Config.model_validate(authored), runtime_paths)
    with_current_room_member_access(config)
    with_responder_access(config, "helper", users=[REQUESTER_ID])
    if approval_rules:
        config.tool_approval = ToolApprovalConfig(rules=list(approval_rules))
    models = {
        "default": ScriptedModel([list(turn) for turn in leader]),
        "writer-model": ScriptedModel([list(turn) for turn in writer]),
        "checker-model": ScriptedModel([list(turn) for turn in checker]),
        "helper-model": ScriptedModel([list(turn) for turn in helper or ()]),
    }
    monkeypatch.setattr(agents, "_build_agent_model_provider", lambda _config, _paths, name, _identity: models[name])
    monkeypatch.setattr(envelope, "build_model_provider", lambda _config, _paths, name, **_kwargs: models[name])
    runtime = make_test_tool_runtime_context(
        agent_name="squad",
        target=MessageTarget(
            room_id=ROOM_ID,
            source_thread_id=THREAD_ID,
            resolved_thread_id=THREAD_ID,
            reply_to_event_id=EVENT_ID,
            session_id=SESSION_ID,
        ),
        requester_id=REQUESTER_ID,
        client=SimpleNamespace(user_id="@mindroom_squad:localhost"),
        config=config,
        runtime_paths=runtime_paths,
        relations=make_relation_lookup(),
        conversation_reader=make_conversation_reader_mock(),
        correlation_id="correlation-1",
        membership_turn_id=EVENT_ID,
    )
    identity = build_execution_identity_from_runtime_context(runtime)
    storage = create_scope_session_storage(
        agent_name="checker",
        scope=TEAM_SCOPE,
        config=config,
        runtime_paths=runtime_paths,
        execution_identity=identity,
    )
    orchestrator = _team_orchestrator(config, runtime_paths)
    orchestrator.agent_bots = {name: SimpleNamespace(running=True) for name in ("writer", "checker", "helper")}
    return SquadTurn(
        config,
        runtime_paths,
        models,
        TurnRecorder(user_message="Hello"),
        storage,
        identity,
        runtime,
        orchestrator,
    )


def turn_context() -> ResponseTurnContext:
    """Return the turn context of one reply to the requester's message in the squad's thread."""
    return make_turn_context(
        "squad",
        session_id=SESSION_ID,
        run_id="run-1",
        correlation_id="correlation-1",
        reply_to_event_id=EVENT_ID,
        room_id=ROOM_ID,
        thread_id=THREAD_ID,
        requester_id=REQUESTER_ID,
    )


def delegate(call_id: str, member_id: str, task: str) -> list[ScriptStep]:
    """Return a leader turn that delegates ``task`` to one member."""
    return calls(tool_call("delegate_task_to_member", call_id, member_id=member_id, task=task), metrics=usage(40, 4))


async def stream_reply(
    team: SquadTurn,
    *,
    mode: TeamMode = TeamMode.COORDINATE,
    ctx: ResponseTurnContext | None = None,
    **kwargs: Any,  # noqa: ANN401
) -> list[Any]:
    """Stream one team reply as the response runner does and return its chunks; ``ctx`` defaults to ``turn_context``'s."""
    with tool_runtime_context(team.runtime):
        return [
            chunk
            async for chunk in team_response_stream(
                agent_ids=team.member_ids(),
                message="Hello",
                orchestrator=team.orchestrator,
                execution_identity=team.identity,
                ctx=ctx or turn_context(),
                mode=mode,
                user_id=REQUESTER_ID,
                configured_team_name="squad",
                turn_recorder=team.recorder,
                **kwargs,
            )
        ]


async def blocking_reply(team: SquadTurn, *, mode: TeamMode = TeamMode.COORDINATE, **kwargs: Any) -> str:  # noqa: ANN401
    """Run one blocking team reply and return its text."""
    with tool_runtime_context(team.runtime):
        return await team_response(
            agent_names=["writer", "checker"],
            mode=mode,
            message="Hello",
            orchestrator=team.orchestrator,
            execution_identity=team.identity,
            ctx=turn_context(),
            user_id=REQUESTER_ID,
            configured_team_name="squad",
            turn_recorder=team.recorder,
            **kwargs,
        )


def stored_runs(team: SquadTurn) -> tuple[TeamRunRecord, list[RunRecord]]:
    """Return the stored team run and its member runs."""
    session = team.storage.load_session(SESSION_ID, "team")
    assert session is not None
    [record] = [run for run in session.runs if isinstance(run, TeamRunRecord)]
    members = [run for run in session.runs if isinstance(run, RunRecord)]
    return record, members


def member_approval(call_id: str, label: str) -> ApprovalCall:
    """Return the approval of the checker's ``touch`` call with ``label``."""
    return ApprovalCall(
        tool_call_id=call_id,
        tool_name="touch",
        invoking_agent="checker",
        toolkit_name=PROBE_TOOL,
        expires_at_ns=2**62,
        arguments_digest=approval_arguments_digest({"label": label}),
    )


async def resume_approval(
    team: SquadTurn,
    paused: PausedAttempt,
    *,
    approved: bool,
    calls_: Sequence[ApprovalCall],
) -> CompletedApprovalRun | PausedAttempt:
    """Approve or deny ``calls_`` and resume the paused team run as a saved approval does."""
    with tool_runtime_context(team.runtime):
        return await continue_paused_team_run(
            member_names=("writer", "checker"),
            mode=TeamMode.COORDINATE,
            config=team.config,
            runtime_paths=team.runtime_paths,
            execution_identity=team.identity,
            session_id=SESSION_ID,
            run_id=paused.run_id,
            user_id=REQUESTER_ID,
            configured_team_name="squad",
            model_name="default",
            decisions=dict.fromkeys((call.tool_call_id for call in calls_), approved),
            denial_reasons=dict.fromkeys((call.tool_call_id for call in calls_), "Declined by requester"),
            refresh_scheduler=None,
            approval_calls=calls_,
            history_scope=TEAM_SCOPE,
            prior_tool_trace=paused.tool_trace,
            prior_presentation_state=paused.response_presentation_state,
            show_tool_calls=False,
            progress=None,
        )
