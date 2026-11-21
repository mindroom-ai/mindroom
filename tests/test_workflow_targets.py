"""Tests for the agents a workflow may run: the `workflow` delegation grant.

Level: pure, with a stand-in for the bots' room lists. `authorize_delegation` answers for a requester in a room; the
job runtime's own check (`ToolJobRuntimeCoordinator._grant`) is covered in `tests/test_workflow_agent_nodes.py`.
"""

from __future__ import annotations

from dataclasses import asdict, replace
from types import SimpleNamespace
from typing import TYPE_CHECKING

import pytest

from mindroom.agent_reply_membership import AgentReplyMembershipIndex
from mindroom.config.agent import AgentConfig, TeamConfig
from mindroom.config.main import Config
from mindroom.config.models import DefaultsConfig, ModelConfig
from mindroom.delegation.lifecycle import (
    MAX_DELEGATION_DEPTH,
    DelegationPendingError,
    authorize_delegation,
    caller_grants_child,
    delegation_grant,
    prepare_child_turn,
)
from mindroom.delegation.state import DelegationChild
from mindroom.orchestration.tool_job_runtime import ToolJobRuntimeCoordinator
from mindroom.tool_jobs.runtime import BackgroundJob
from mindroom.tool_system.runtime_context import tool_runtime_context
from mindroom.tool_system.worker_routing import ToolExecutionIdentity
from tests.access_schema_support import with_responder_access
from tests.delegation_helpers import _delegate_runtime_context, _runtime_paths

if TYPE_CHECKING:
    from pathlib import Path

pytestmark = pytest.mark.usefixtures("enforce_turn_authorization")

_ROOM = "!room:example.org"
_ALICE = "@alice:example.org"
_IDENTITY = ToolExecutionIdentity("matrix", "dev", _ALICE, _ROOM, "$thread", "$thread", "session")
# The rooms each agent's bot is in. `stranger` answers Alice anywhere and is not in the run's room.
# `late` has not finished its first sync, so its bot does not know its rooms yet.
_ROOMS = {
    "dev": [_ROOM],
    "helper": [_ROOM],
    "reviewer": [_ROOM],
    "secret": [_ROOM],
    "stranger": ["!other:example.org"],
    "late": [],
}


def _config(*, workflow: bool = True) -> Config:
    """`dev` has the workflow toolkit and may delegate to `helper`; Alice may address every agent but `secret`."""
    agents = {
        "dev": AgentConfig(display_name="Dev", tools=["workflow"] if workflow else [], delegate_to=["helper"]),
        **{
            name: AgentConfig(display_name=name.title())
            for name in ("helper", "reviewer", "secret", "stranger", "late")
        },
    }
    config = Config(
        agents=agents,
        defaults=DefaultsConfig(tools=[]),
        memory={"backend": "none"},
        models={"default": ModelConfig(provider="openai", id="default-model")},
    )
    for name in ("dev", "helper", "reviewer", "stranger", "late"):
        with_responder_access(config, name, users=[_ALICE])
    return with_responder_access(config, "secret", users=["@bob:example.org"])


def _authorize(
    tmp_path: Path,
    target: str,
    *,
    grant: str = "workflow",
    config: Config | None = None,
    **options: object,
) -> Config | str:
    """Ask as the workflow of `dev`, for Alice, in the room, with each bot's own list of the rooms it is in."""
    config, paths = config or _config(), _runtime_paths(tmp_path)
    bots = {
        name: SimpleNamespace(client=SimpleNamespace(rooms=dict.fromkeys(rooms)), first_sync_complete=name != "late")
        for name, rooms in _ROOMS.items()
    }
    context = _delegate_runtime_context(config, paths, execution_identity=_IDENTITY, agent_name="dev")
    with tool_runtime_context(replace(context, orchestrator=SimpleNamespace(agent_bots=bots))):  # type: ignore[arg-type]
        return authorize_delegation(
            "dev",
            target,
            "label",
            **{"config": config, "runtime_paths": paths, "execution_identity": _IDENTITY, "depth": 0, **options},
            grant=grant,  # type: ignore[arg-type]
        )


def test_self_is_allowed_without_delegate_to(tmp_path: Path) -> None:
    """A copy of the caller needs only the caller's `workflow` toolkit."""
    assert isinstance(_authorize(tmp_path, "dev"), Config)
    assert "Cannot delegate to 'dev'" in str(_authorize(tmp_path, "dev", config=_config(workflow=False)))


def test_agent_in_delegate_to_is_allowed_wherever_its_bot_is(tmp_path: Path) -> None:
    """What `run_subagent` may run, a workflow may run."""
    assert isinstance(_authorize(tmp_path, "helper"), Config)


def test_room_agent_joined_and_addressable_is_allowed(tmp_path: Path) -> None:
    """An agent that is in the run's room and answers the requester there runs without a `delegate_to` entry."""
    assert isinstance(_authorize(tmp_path, "reviewer"), Config)
    assert "Cannot delegate to 'reviewer'" in str(_authorize(tmp_path, "reviewer", grant="delegate"))


def test_agent_the_requester_may_address_globally_but_not_in_this_room_is_refused(tmp_path: Path) -> None:
    """Being allowed to talk to an agent somewhere else gives a run in this room nothing."""
    assert "Cannot delegate to 'stranger'" in str(_authorize(tmp_path, "stranger"))


def test_joined_agent_the_requester_may_not_address_is_refused(tmp_path: Path) -> None:
    """An agent in the room that does not answer the requester does not run for the requester's workflow either."""
    assert _authorize(tmp_path, "secret") == "Cannot delegate to 'secret': that agent is not allowed to reply to you."


def test_agent_whose_bot_is_unknown_is_refused(tmp_path: Path) -> None:
    """No list of a bot's rooms is no proof that it is in the run's room."""
    config = _config()
    config.agents["absent"] = AgentConfig(display_name="Absent")
    with_responder_access(config, "absent", users=[_ALICE])

    assert "Cannot delegate to 'absent'" in str(_authorize(tmp_path, "absent", config=config))


def test_agent_whose_bot_has_not_synced_yet_is_not_ready_instead_of_refused(tmp_path: Path) -> None:
    """A bot that is still starting cannot say which rooms it is in: the answer is to ask again, not a refusal."""
    with pytest.raises(DelegationPendingError, match="'late'"):
        _authorize(tmp_path, "late")
    assert "Cannot delegate to 'late'" in str(_authorize(tmp_path, "late", grant="delegate"))


def test_depth_limit_and_model_check_are_kept(tmp_path: Path) -> None:
    """The checks every delegation gets apply to a workflow's agent too."""
    too_deep = _authorize(tmp_path, "dev", depth=MAX_DELEGATION_DEPTH)
    unknown_model = _authorize(tmp_path, "dev", model="opus5")

    assert too_deep == "Cannot delegate: the maximum delegation depth was reached."
    assert unknown_model == "Cannot delegate: Unknown model 'opus5'. Available models: default."


def test_delegate_grant_is_unchanged(tmp_path: Path) -> None:
    """`run_subagent` still needs the `delegate_to` entry, also for the caller itself."""
    assert isinstance(_authorize(tmp_path, "helper", grant="delegate"), Config)
    assert _authorize(tmp_path, "dev", grant="delegate") == "Cannot delegate to 'dev'. Allowed subagents: helper."


def test_caller_grant_answers_only_whether_the_caller_has_the_toolkit() -> None:
    """Without a requester or a room, the grant says the caller may run workflow agents, not which."""
    assert caller_grants_child("dev", "stranger", _config(), "workflow")
    assert not caller_grants_child("dev", "dev", _config(workflow=False), "workflow")
    assert not caller_grants_child("gone", "dev", _config(), "workflow")


def _child(tmp_path: Path, target: str, previous: DelegationChild | None = None) -> DelegationChild:
    return prepare_child_turn(
        "dev",
        target,
        "label",
        owner=_IDENTITY,
        config=_config(),
        runtime_paths=_runtime_paths(tmp_path),
        depth=0,
        budget_monitor=None,
        model=None,
        agent_mode="standard",
        previous=previous,
        persona=None,
    )


@pytest.mark.parametrize(
    ("target", "synced", "granted"),
    [
        ("dev", True, "allowed"),
        ("helper", True, "allowed"),
        ("reviewer", True, "allowed"),
        ("stranger", True, "denied"),
        ("secret", True, "denied"),
        # A bot that has not synced yet cannot say which rooms it is in: the job is kept, not stopped.
        ("reviewer", False, "pending"),
        ("helper", False, "allowed"),
    ],
)
def test_job_runtime_grants_the_job_of_a_runs_agent_by_the_same_rule(
    tmp_path: Path,
    target: str,
    synced: bool,
    granted: str,
) -> None:
    """The job runtime asks at admission, on every pass and before a continuation: it answers as the dispatch did."""
    config, paths = _config(), _runtime_paths(tmp_path)
    rooms = {name: SimpleNamespace(rooms=dict.fromkeys(joined)) for name, joined in _ROOMS.items()}
    bots = {name: SimpleNamespace(first_sync_complete=synced, client=client) for name, client in rooms.items()}
    coordinator = ToolJobRuntimeCoordinator(
        runtime_paths=paths,
        config_provider=lambda: config,
        bot_provider=bots.get,  # type: ignore[arg-type]
        agent_reply_memberships=AgentReplyMembershipIndex(),
        journal_provider=lambda: None,  # type: ignore[arg-type,return-value]
    )
    of_run = replace(_child(tmp_path, target), workflow_run_id="wf-" + "a" * 32, workflow_node_key="@0")
    job = BackgroundJob("job", _IDENTITY, target, 0, kind="delegation", adapter={"child": asdict(of_run)})

    assert coordinator._grant(job) == granted
    if target == "reviewer":
        # The same child with no run is an ordinary subagent, which needs the `delegate_to` entry.
        plain = replace(job, adapter={"child": asdict(_child(tmp_path, target))})
        assert coordinator._grant(plain) == "denied"


@pytest.mark.parametrize(("target", "granted"), [("dev", "allowed"), ("reviewer", "allowed"), ("secret", "denied")])
def test_job_runtime_grants_a_team_members_own_copy_to_a_requester_of_the_team(
    tmp_path: Path,
    target: str,
    granted: str,
) -> None:
    """Alice reaches `dev` only through its team: the run's copy of `dev` keeps that access, as its dispatch said.

    Any other agent of the run must answer Alice itself, under the team's bot as under the agent's own.
    """
    config, paths = _config(), _runtime_paths(tmp_path)
    config.teams["crew"] = TeamConfig(display_name="Crew", role="Builds things.", agents=["dev"])
    with_responder_access(config, "crew", users=[_ALICE])
    with_responder_access(config, "dev", users=["@bob:example.org"])
    rooms = {name: SimpleNamespace(rooms=dict.fromkeys(joined)) for name, joined in _ROOMS.items()}
    bots = {name: SimpleNamespace(first_sync_complete=True, client=client) for name, client in rooms.items()}
    coordinator = ToolJobRuntimeCoordinator(
        runtime_paths=paths,
        config_provider=lambda: config,
        bot_provider=bots.get,  # type: ignore[arg-type]
        agent_reply_memberships=AgentReplyMembershipIndex(),
        journal_provider=lambda: None,  # type: ignore[arg-type,return-value]
    )
    of_team = replace(_IDENTITY, transport_agent_name="crew")
    of_run = replace(_child(tmp_path, target), workflow_run_id="wf-" + "a" * 32, workflow_node_key="@0")
    job = BackgroundJob("job", of_team, target, 0, kind="delegation", adapter={"child": asdict(of_run)})

    assert coordinator._grant(job) == granted


def test_follow_up_turn_of_a_runs_agent_is_an_ordinary_subagent_turn(tmp_path: Path) -> None:
    """`continue_subagent` on the agent of a run's call starts a turn of its own: not the run's, and not under its grant."""
    of_run = replace(
        _child(tmp_path, "helper"),
        workflow_run_id="wf-" + "a" * 32,
        workflow_node_key="@0",
        workflow_generation=1,
    )

    follow_up = _child(tmp_path, "helper", previous=of_run)

    assert (follow_up.subagent_id, follow_up.previous_delegation_id) == (of_run.subagent_id, of_run.delegation_id)
    assert (follow_up.workflow_run_id, follow_up.workflow_node_key, follow_up.workflow_generation) == (None, None, None)
    assert (delegation_grant(of_run), delegation_grant(follow_up)) == ("workflow", "delegate")


def test_old_child_snapshot_reads_without_the_new_fields(tmp_path: Path) -> None:
    """A child saved before workflows has no run: it reads as before, and is granted as before."""
    child = _child(tmp_path, "helper")
    saved = asdict(child)
    old = {name: value for name, value in saved.items() if not name.startswith("workflow_")}
    assert len(saved) == len(old) + 3

    read = DelegationChild.from_dict(old)

    assert (read.workflow_run_id, read.workflow_node_key, read.workflow_generation) == (None, None, None)
    assert delegation_grant(read) == "delegate"
    of_run = replace(read, workflow_run_id="wf-" + "a" * 32, workflow_node_key="@0", workflow_generation=1)
    assert delegation_grant(of_run) == "workflow"
