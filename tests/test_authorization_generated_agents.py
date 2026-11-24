"""Administrators pass a generated agent's access only when its own access admits them."""

from __future__ import annotations

from copy import deepcopy
from typing import TYPE_CHECKING, Any
from unittest.mock import MagicMock

import pytest

from mindroom.agent_reply_membership import AgentReplyMembershipIndex
from mindroom.approval import inbound
from mindroom.approval.manager import ApprovalActionResult
from mindroom.authorization import (
    is_sender_allowed_for_agent_credential_management,
    is_sender_allowed_for_agent_reply_in_room,
    is_sender_allowed_for_entity_replies_in_room,
)
from mindroom.config.main import Config
from mindroom.constants import ROUTER_AGENT_NAME
from mindroom.delegation.lifecycle import authorize_delegation
from mindroom.requester_identity import is_platform_administrator
from mindroom.tool_system.runtime_context import tool_runtime_context
from mindroom.tool_system.worker_routing import ToolExecutionIdentity
from tests.conftest import bind_runtime_paths, runtime_paths_for, test_runtime_paths
from tests.delegation_helpers import _delegate_runtime_context
from tests.test_generated_agents import BASE, CO_OWNER, OWNER, PERSONAL_TEMPLATE, _created, _load, _personal

if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path

ADMIN = "@admin:localhost"
GUEST = "@guest:localhost"
OUTSIDER = "@outsider:localhost"
ROOM_ID = "!room:localhost"
PERSONAL = "mind_alice"
CREATED = "creator_research"


def _config(tmp_path: Path) -> Config:
    """A deployment with an administrator, one personal and one created agent, a shared agent, and two teams."""
    data: dict[str, Any] = deepcopy(BASE)
    template = deepcopy(PERSONAL_TEMPLATE)
    template["agent"]["access"] = {"users": [GUEST]}
    data["personal_agents"] = {"mind": template}
    data["administrators"] = [ADMIN]
    data["agents"]["helper"] = {"display_name": "Helper", "model": "default", "delegate_to": [PERSONAL]}
    data["teams"] = {
        "with_personal": {"display_name": "With Personal", "role": "Mixed", "agents": ["helper", PERSONAL]},
        "shared_only": {"display_name": "Shared Only", "role": "Shared", "agents": ["helper", "creator"]},
    }
    loaded = _load(tmp_path, data, entries=[_personal()], created=[_created()])
    return bind_runtime_paths(loaded, test_runtime_paths(tmp_path))


def _replies(sender_id: str, entity_name: str, config: Config) -> bool:
    return is_sender_allowed_for_agent_reply_in_room(
        sender_id,
        entity_name,
        config,
        ROOM_ID,
        runtime_paths_for(config),
        AgentReplyMembershipIndex(),
    )


async def _approves(
    sender_id: str,
    entity_name: str,
    config: Config,
    monkeypatch: pytest.MonkeyPatch,
) -> bool:
    """Resolve one approval card of ``entity_name`` and return whether its responder check admitted the sender."""

    async def resolve(_action: object, *, authorize_responder: Callable[[str], bool], **_: object) -> Any:  # noqa: ANN401
        return ApprovalActionResult(consumed=authorize_responder(entity_name))

    monkeypatch.setattr(inbound, "handle_matrix_approval_action", resolve)
    room = MagicMock()
    room.room_id = ROOM_ID
    return await inbound.handle_tool_approval_action(
        room=room,
        sender_id=sender_id,
        config=config,
        runtime_paths=runtime_paths_for(config),
        orchestrator=None,
        logger=MagicMock(),
        approval_event_id="$card",
        status="approved",
        reason=None,
        membership_index=AgentReplyMembershipIndex(),
    )


def _delegates(requester_id: str, config: Config) -> Config | str:
    paths = runtime_paths_for(config)
    identity = ToolExecutionIdentity(
        channel="matrix",
        agent_name="helper",
        requester_id=requester_id,
        room_id=ROOM_ID,
        thread_id="$thread",
        resolved_thread_id="$thread",
        session_id="parent-session",
    )
    with tool_runtime_context(
        _delegate_runtime_context(config, paths, execution_identity=identity, agent_name="helper"),
    ):
        return authorize_delegation(
            "helper",
            PERSONAL,
            "task",
            config=config,
            runtime_paths=paths,
            execution_identity=identity,
            depth=0,
        )


@pytest.mark.parametrize("agent_name", [PERSONAL, CREATED])
def test_an_administrator_cannot_message_a_generated_agent(tmp_path: Path, agent_name: str) -> None:
    """Being an administrator grants nothing on a personal or created agent."""
    config = _config(tmp_path)

    assert not _replies(ADMIN, agent_name, config)


@pytest.mark.asyncio
@pytest.mark.parametrize("agent_name", [PERSONAL, CREATED])
async def test_an_administrator_cannot_answer_a_generated_agent_s_approval_card(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    agent_name: str,
) -> None:
    """An approval card follows the same rule as a message."""
    config = _config(tmp_path)

    assert not await _approves(ADMIN, agent_name, config, monkeypatch)
    assert await _approves(OWNER, agent_name, config, monkeypatch)


def test_owners_co_owners_and_template_users_are_admitted(tmp_path: Path) -> None:
    """The agent's own access decides: the owner, a co-owner, and the template's access.users."""
    config = _config(tmp_path)

    assert _replies(OWNER, PERSONAL, config)
    assert _replies(GUEST, PERSONAL, config)
    assert _replies(OWNER, CREATED, config)
    assert _replies(CO_OWNER, CREATED, config)


def test_an_administrator_the_template_admits_is_admitted(tmp_path: Path) -> None:
    """The template's access is how a deployment admits someone more, administrators included."""
    config = _config(tmp_path)
    config.agents[PERSONAL].access.users.append(ADMIN)

    assert _replies(ADMIN, PERSONAL, config)


@pytest.mark.parametrize("entity_name", ["helper", "creator", "shared_only", ROUTER_AGENT_NAME])
def test_an_administrator_still_reaches_shared_responders(tmp_path: Path, entity_name: str) -> None:
    """Shared agents, teams of shared agents, and the router admit administrators as before."""
    config = _config(tmp_path)

    assert _replies(ADMIN, entity_name, config)
    assert not _replies(OUTSIDER, entity_name, config)


@pytest.mark.parametrize("entity_name", [PERSONAL, CREATED, "with_personal"])
def test_an_ordinary_outsider_is_refused(tmp_path: Path, entity_name: str) -> None:
    """Someone neither administrator nor admitted is refused, as before."""
    config = _config(tmp_path)

    assert not _replies(OUTSIDER, entity_name, config)


def test_an_administrator_cannot_reach_a_personal_agent_through_a_team(tmp_path: Path) -> None:
    """A team with a generated member admits only whom the team's own access admits."""
    config = _config(tmp_path)

    assert not _replies(ADMIN, "with_personal", config)
    config.teams["with_personal"].access = config.agents[PERSONAL].access.model_copy(update={"users": [ADMIN]})
    assert _replies(ADMIN, "with_personal", config)


def test_an_administrator_cannot_reach_a_personal_agent_in_an_ad_hoc_team(tmp_path: Path) -> None:
    """Mentioning a shared and a personal agent together needs access to each."""
    config = _config(tmp_path)

    allowed = is_sender_allowed_for_entity_replies_in_room(
        ADMIN,
        ("helper", PERSONAL),
        config,
        ROOM_ID,
        runtime_paths_for(config),
        AgentReplyMembershipIndex(),
    )

    assert not allowed


def test_an_administrator_cannot_delegate_to_a_personal_agent(tmp_path: Path) -> None:
    """A shared agent acting for an administrator cannot hand work to someone's personal agent."""
    config = _config(tmp_path)

    assert _delegates(ADMIN, config) == f"Cannot delegate to '{PERSONAL}': that agent is not allowed to reply to you."
    assert isinstance(_delegates(OWNER, config), Config)


def test_administrator_powers_outside_conversation_stay(tmp_path: Path) -> None:
    """An administrator still administers and manages every agent's credentials."""
    config = _config(tmp_path)
    paths = runtime_paths_for(config)

    assert is_platform_administrator(ADMIN, config, paths)
    assert is_sender_allowed_for_agent_credential_management(ADMIN, PERSONAL, config, paths)
    assert is_sender_allowed_for_agent_credential_management(ADMIN, CREATED, config, paths)
