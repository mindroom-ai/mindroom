"""A reply's run metadata reports what the subagent turns whose results it received cost, beside its own cost.

Each test runs real leader replies through the turn driver, with scripted models on priced configured models, and reads
the ``io.mindroom.ai_run`` metadata each reply publishes.
"""

from __future__ import annotations

import asyncio
import json
from dataclasses import asdict, dataclass, field, replace
from typing import TYPE_CHECKING, Any, Literal

import pytest

from mindroom.ai import ai_response, stream_agent_response
from mindroom.approval import manager as approval_manager
from mindroom.config.agent import AgentConfig, TeamConfig
from mindroom.config.approval import ApprovalRuleConfig, ToolApprovalConfig
from mindroom.config.main import Config
from mindroom.config.models import DefaultsConfig, ModelConfig, ModelPricing
from mindroom.constants import AI_RUN_METADATA_KEY, subagent_sessions_dir
from mindroom.delegation.state import DelegationChild
from mindroom.event_journal import BackgroundApprovalDecision
from mindroom.providers.identity import model_identity
from mindroom.response_turn import TurnSinks, collect_streamed_response_content
from mindroom.teams.envelope import TeamTurnSpec
from mindroom.teams.spec import TeamMode
from mindroom.tool_jobs.instances import pin_background_tool_jobs
from mindroom.tool_jobs.runtime import register_background_runtime
from mindroom.tool_system.events import CollectedStreamPresentation
from mindroom.tool_system.runtime_context import tool_runtime_context
from mindroom.tool_system.worker_routing import ToolExecutionIdentity
from tests.access_schema_support import with_responder_access
from tests.ai_user_id_helpers import team_orchestrator
from tests.conftest import make_turn_context
from tests.delegation_helpers import ScriptedAgents, _delegate_runtime_context, _runtime_paths, registered_report_tool
from tests.fakes.scripted_model import ScriptedModel, answer, calls, tool_call, usage
from tests.identity_helpers import entity_ids
from tests.team_turn_helpers import collect_team_reply
from tests.tool_job_helpers import (
    DELEGATING_REQUESTER,
    JOB_TEST_TIMEOUT,
    JobApprovalCards,
    tool_job_runtime,
    wait_for_status,
)
from tests.turn_model_helpers import team_member_models

if TYPE_CHECKING:
    from collections.abc import AsyncIterator
    from pathlib import Path

    from mindroom.constants import RuntimePaths
    from mindroom.tool_jobs.runtime import ToolJobRuntime

_IDENTITY = ToolExecutionIdentity("matrix", "leader", DELEGATING_REQUESTER, "!room:example.org", None, None, "parent")

# How a leader's subagent call runs: inside the call (``direct``), through the native driver its paused run hands the
# call to (``native``), or as a background job whose result the call waits for (``job``).
type _Mode = Literal["direct", "native", "job"]
_MODES: tuple[_Mode, ...] = ("direct", "native", "job")

# The leader's model costs 5 and 30 USD per million input and output tokens, each subagent's 1 and 2.
_LEADER_REQUEST = usage(1_000, 10)
# Two leader requests of 1,000 input and 10 output tokens at 5 and 30 per million.
_LEADER_USD = "0.0106"
_CHILD_REQUEST = usage(2_000, 100)
# One subagent request of 2,000 input and 100 output tokens at 1 and 2 per million.
_CHILD_USD = 0.0022


def _config(*, jobs: bool, mid_tools: list[str]) -> Config:
    """The leader delegates to ``mid``, which delegates to ``leaf``; the leader's model and theirs are priced.

    ``write_report``, which ``mid`` may call when its ``mid_tools`` include ``report``, asks for approval.
    """
    config = Config(
        agents={
            "leader": AgentConfig(display_name="Leader", delegate_to=["mid"]),
            "mid": AgentConfig(display_name="Mid", model="cheap", tools=mid_tools, delegate_to=["leaf"]),
            "leaf": AgentConfig(display_name="Leaf", model="cheap"),
        },
        models={
            "default": ModelConfig(provider="openai", id="gpt-6-astra", pricing=ModelPricing(input=5, output=30)),
            "cheap": ModelConfig(provider="openai", id="gpt-6.1-sol", pricing=ModelPricing(input=1, output=2)),
        },
        defaults=DefaultsConfig(tools=[]),
        memory={"backend": "none"},
        tool_approval=ToolApprovalConfig(rules=[ApprovalRuleConfig(match="write_report", action="require_approval")]),
    )
    config.background_tool_jobs.enabled = jobs
    for name in ("mid", "leaf"):
        with_responder_access(config, name, users=[DELEGATING_REQUESTER])
    return config


@dataclass
class _Conversation:
    """One requester's conversation with the leader, whose replies run through the turn driver."""

    tmp_path: Path
    monkeypatch: pytest.MonkeyPatch
    mode: _Mode
    config: Config = field(init=False)
    paths: RuntimePaths = field(init=False)
    agents: ScriptedAgents = field(init=False)
    runtime: ToolJobRuntime | None = None
    turns: int = 0

    async def open(self, *, mid_tools: tuple[str, ...] = (), **scripts: list) -> None:
        """Script each subagent's turns by agent name."""
        self.config = _config(jobs=self.mode == "job", mid_tools=list(mid_tools))
        self.paths = _runtime_paths(self.tmp_path)
        entity_ids(self.config, self.paths)
        if self.mode == "job":
            pin_background_tool_jobs(self.config, self.paths)
            self.runtime = await tool_job_runtime(self.paths.storage_root)
            register_background_runtime(self.paths, self.runtime)
        self.agents = ScriptedAgents(
            {name: self._model(name, turns) for name, turns in scripts.items()},
        )
        self.agents.install(self.monkeypatch)

    def _model(self, agent_name: str, turns: list) -> ScriptedModel:
        """Return a scripted model that reports itself as the configured model ``agent_name`` runs on."""
        model_name = self.config.agents[agent_name].model or "default"
        return ScriptedModel(list(turns), identity=model_identity(self.config.models[model_name]))

    async def reply(self, *steps: list, blocking: bool = False) -> dict[str, Any]:
        """Run one leader reply whose model takes ``steps`` and return the run metadata it publishes.

        A ``blocking`` reply is collected as API and headless replies are, and its metadata read once it ended.
        """
        self.turns += 1
        self.agents.models = {**self.agents.models, "leader": self._model("leader", list(steps))}
        context = replace(
            _delegate_runtime_context(self.config, self.paths, execution_identity=_IDENTITY),
            membership_turn_id=f"$turn{self.turns}",
        )
        ctx = replace(
            make_turn_context(
                "leader",
                session_id=_IDENTITY.session_id,
                run_id=f"reply{self.turns}",
                room_id=_IDENTITY.room_id,
                requester_id=_IDENTITY.requester_id,
            ),
            prompt="Delegate",
            execution_identity=_IDENTITY,
            supports_native_tool_approval=self.mode == "native",
        )
        collected: dict[str, Any] = {}
        with tool_runtime_context(context):
            if blocking:
                result = await asyncio.wait_for(ai_response(ctx, self.paths, self.config), JOB_TEST_TIMEOUT)
                return result.run_metadata[AI_RUN_METADATA_KEY]
            await asyncio.wait_for(
                collect_streamed_response_content(
                    stream_agent_response(
                        ctx,
                        self.paths,
                        self.config,
                        sinks=TurnSinks(run_metadata_collector=collected),
                    ),
                    presentation=CollectedStreamPresentation(show_tool_calls=False, track_hidden_tools=True),
                ),
                JOB_TEST_TIMEOUT,
            )
        return collected[AI_RUN_METADATA_KEY]

    def saved_costs(self) -> dict[str, float | None]:
        """Return the cost each subagent's saved turn records, by agent name."""
        children = (
            DelegationChild.from_dict(json.loads(path.read_text())["child"])
            for path in subagent_sessions_dir(self.paths).glob("*.json")
        )
        return {child.child_agent_name: child.cost_usd for child in children}

    async def close(self) -> None:
        if self.runtime is not None:
            await self.runtime.shutdown()


@pytest.fixture
async def conversation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    request: pytest.FixtureRequest,
) -> AsyncIterator[_Conversation]:
    """Open the conversation in the mode the test names, and stop its job runtime afterwards."""
    conversation = _Conversation(tmp_path, monkeypatch, request.param)
    yield conversation
    await conversation.close()


def _delegate(call_id: str, agent_name: str, **arguments: object) -> list:
    """Return a turn that calls ``run_subagent`` for ``agent_name``."""
    return calls(
        tool_call("run_subagent", call_id, agent_name=agent_name, task="Do the work", **arguments),
        metrics=_LEADER_REQUEST,
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("conversation", _MODES, indirect=True)
async def test_a_reply_reports_its_subagent_s_cost_beside_its_own(conversation: _Conversation) -> None:
    """A reply that ran a subagent on a priced model reports the subagent's cost apart from its own."""
    await conversation.open(mid=[answer("Mid answer", _CHILD_REQUEST)])

    metadata = await conversation.reply(_delegate("delegate", "mid"), answer("Parent done", _LEADER_REQUEST))

    assert metadata["cost"] == {"usd": _LEADER_USD, "subagents_usd": f"{_CHILD_USD:g}"}


@pytest.mark.asyncio
@pytest.mark.parametrize("conversation", ["direct"], indirect=True)
async def test_a_reply_read_after_it_ended_reports_its_subagent_s_cost(conversation: _Conversation) -> None:
    """A reply collected whole, as an API or headless reply is, reports its subagent's cost too."""
    await conversation.open(mid=[answer("Mid answer", _CHILD_REQUEST)])

    metadata = await conversation.reply(
        _delegate("delegate", "mid"),
        answer("Parent done", _LEADER_REQUEST),
        blocking=True,
    )

    assert metadata["cost"] == {"usd": _LEADER_USD, "subagents_usd": f"{_CHILD_USD:g}"}


@pytest.mark.asyncio
@pytest.mark.parametrize("conversation", _MODES, indirect=True)
async def test_a_subagent_s_own_subagents_count_toward_its_cost(conversation: _Conversation) -> None:
    """A subagent's cost includes the cost of the subagents it ran, so the reply reports both."""
    await conversation.open(
        mid=[
            calls(tool_call("run_subagent", "nested", agent_name="leaf", task="Dig"), metrics=_CHILD_REQUEST),
            answer("Mid answer", _CHILD_REQUEST),
        ],
        leaf=[answer("Leaf answer", _CHILD_REQUEST)],
    )

    metadata = await conversation.reply(_delegate("delegate", "mid"), answer("Parent done", _LEADER_REQUEST))

    # Mid's two requests and leaf's one: leaf's cost is part of mid's, which the reply received.
    assert metadata["cost"] == {"usd": _LEADER_USD, "subagents_usd": f"{3 * _CHILD_USD:g}"}
    assert conversation.saved_costs() == pytest.approx({"mid": 3 * _CHILD_USD, "leaf": _CHILD_USD})


@pytest.mark.asyncio
@pytest.mark.parametrize("conversation", ["job"], indirect=True)
async def test_a_background_subagent_counts_on_the_reply_that_waited_for_its_result(
    conversation: _Conversation,
) -> None:
    """A background subagent counts on the reply that first read its result, not on the one that started it."""
    await conversation.open(mid=[answer("Mid answer", _CHILD_REQUEST)])

    started = await conversation.reply(
        _delegate("delegate", "mid", background_after=0),
        answer("Started", _LEADER_REQUEST),
    )
    assert conversation.runtime is not None
    [job_id] = list(conversation.runtime._entries)
    await asyncio.wait_for(wait_for_status(conversation.runtime, job_id, "completed"), JOB_TEST_TIMEOUT)
    waited = await conversation.reply(
        calls(tool_call("job", "wait", action="wait", job_id=job_id), metrics=_LEADER_REQUEST),
        answer("Got it", _LEADER_REQUEST),
    )
    reread = await conversation.reply(
        calls(tool_call("job", "again", action="wait", job_id=job_id), metrics=_LEADER_REQUEST),
        answer("Still got it", _LEADER_REQUEST),
    )

    assert started["cost"] == {"usd": _LEADER_USD}
    assert waited["cost"] == {"usd": _LEADER_USD, "subagents_usd": f"{_CHILD_USD:g}"}
    assert reread["cost"] == {"usd": _LEADER_USD}


@pytest.mark.asyncio
@pytest.mark.parametrize("conversation", ["job"], indirect=True)
async def test_a_subagent_resumed_after_an_approval_counts_the_subagents_it_runs_then(
    conversation: _Conversation,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A background subagent that waited for an approval still counts the subagent it runs once it resumes."""
    with registered_report_tool():
        await conversation.open(
            mid_tools=("report",),
            mid=[
                calls(tool_call("write_report", "report"), metrics=_CHILD_REQUEST),
                calls(tool_call("run_subagent", "nested", agent_name="leaf", task="Dig"), metrics=_CHILD_REQUEST),
                answer("Mid answer", _CHILD_REQUEST),
            ],
            leaf=[answer("Leaf answer", _CHILD_REQUEST)],
        )
        assert conversation.runtime is not None
        cards = JobApprovalCards(asyncio.get_running_loop().create_future(), conversation.runtime.journal)
        monkeypatch.setattr(approval_manager, "get_approval_store", lambda: cards)
        try:
            await conversation.reply(
                _delegate("delegate", "mid", background_after=0),
                answer("Started", _LEADER_REQUEST),
            )
            [job_id] = list(conversation.runtime._entries)
            await asyncio.wait_for(cards.posted.wait(), JOB_TEST_TIMEOUT)
            cards.decision.set_result(BackgroundApprovalDecision("approved", None))
            await asyncio.wait_for(wait_for_status(conversation.runtime, job_id, "completed"), JOB_TEST_TIMEOUT)
            waited = await conversation.reply(
                calls(tool_call("job", "wait", action="wait", job_id=job_id), metrics=_LEADER_REQUEST),
                answer("Got it", _LEADER_REQUEST),
            )
        finally:
            if not cards.decision.done():
                cards.decision.cancel()

    # Mid's three requests and leaf's one.
    assert conversation.saved_costs() == pytest.approx({"mid": 4 * _CHILD_USD, "leaf": _CHILD_USD})
    assert waited["cost"] == {"usd": _LEADER_USD, "subagents_usd": f"{4 * _CHILD_USD:g}"}


@pytest.mark.asyncio
async def test_a_team_reply_reports_what_its_members_subagents_cost(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A team reply whose member ran a subagent reports that subagent's cost beside the team's own."""
    config = _config(jobs=False, mid_tools=[])
    config.teams = {"squad": TeamConfig(display_name="Squad", role="Delegates.", agents=["leader"], mode="coordinate")}
    paths = _runtime_paths(tmp_path)
    entity_ids(config, paths)
    priced = model_identity(config.models["default"])
    ScriptedAgents(
        {
            "leader": ScriptedModel(
                [_delegate("delegate", "mid"), answer("Member done", _LEADER_REQUEST)],
                identity=priced,
            ),
            "mid": ScriptedModel(
                [answer("Mid answer", _CHILD_REQUEST)],
                identity=model_identity(config.models["cheap"]),
            ),
        },
        team=ScriptedModel(
            [
                calls(tool_call("delegate_task_to_member", "member", member_id="leader", task="Delegate")),
                answer("Team done"),
            ],
            identity=priced,
        ),
    ).install(monkeypatch)
    identity = replace(_IDENTITY, agent_name="squad")
    orchestrator = team_orchestrator(config, paths)
    orchestrator.running_entity_names = lambda: frozenset({"leader"})
    spec = TeamTurnSpec(
        agent_names=["leader"],
        mode=TeamMode.COORDINATE,
        orchestrator=orchestrator,
        member_model_names=team_member_models(config, paths, "squad", ["leader"], room_id=identity.room_id),
        configured_team_name="squad",
    )
    ctx = replace(
        make_turn_context(
            "squad",
            session_id=identity.session_id,
            room_id=identity.room_id,
            requester_id=identity.requester_id,
        ),
        prompt="Delegate",
        execution_identity=identity,
    )
    collected: dict[str, Any] = {}
    context = _delegate_runtime_context(config, paths, execution_identity=identity, agent_name="squad")
    with tool_runtime_context(context):
        await asyncio.wait_for(
            collect_team_reply(spec, ctx, sinks=TurnSinks(run_metadata_collector=collected)),
            JOB_TEST_TIMEOUT,
        )

    assert collected[AI_RUN_METADATA_KEY]["cost"] == {"usd": _LEADER_USD, "subagents_usd": f"{_CHILD_USD:g}"}


@pytest.mark.asyncio
@pytest.mark.parametrize("conversation", ["direct"], indirect=True)
async def test_a_reply_without_subagents_reports_only_its_own_cost(conversation: _Conversation) -> None:
    """A reply that ran no subagent has no subagent cost."""
    await conversation.open()

    metadata = await conversation.reply(answer("Hello", _LEADER_REQUEST))

    assert metadata["cost"] == {"usd": "0.0053"}


def test_a_child_snapshot_keeps_its_cost_and_one_saved_without_it_reads_as_unknown() -> None:
    """A child's cost survives its saved snapshot, and a child saved before it had one reads as of unknown cost."""
    child = DelegationChild(
        delegation_id="d1",
        parent_tool_call_id="call",
        caller_agent_name="leader",
        child_agent_name="mid",
        task="Do the work",
        session_id="s1",
        run_id="r1",
        model_name="cheap",
        depth=1,
        execution_identity={},
        subagent_id="d1",
        agent_mode="standard",
        requested_model_name="cheap",
        persona=None,
        cost_usd=0.25,
    )
    snapshot = asdict(child)

    assert DelegationChild.from_dict(snapshot) == child
    del snapshot["cost_usd"]
    assert DelegationChild.from_dict(snapshot).cost_usd is None
