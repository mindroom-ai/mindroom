"""Who may take each contact action, by each way an owner takes it.

An owner acts on the Connections page, through the tools of their personal room's agent, or through the tools of
another of their agents. Alice's agent has asked Bob's agent, and one table says what each owner's action does.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

from mindroom.contacts import flow
from tests.contacts.test_accept import _matrix, _portal, _thread_posts
from tests.contacts.test_tools import BOB_ROOM, _context, _delivered, _pending, _runtime, _setup, _stored
from tests.test_generated_agents import CO_OWNER, OWNER, STRANGER

if TYPE_CHECKING:
    from pathlib import Path

    from mindroom.config.main import Config
    from mindroom.constants import RuntimePaths
    from tests.contacts.test_tools import _Runtime

PAGE, PERSONAL_AGENT, OTHER_AGENT = "page", "personal agent", "other agent"
# The agent each owner acts through on a tool path; Alice has no second agent.
AGENTS = {
    (PERSONAL_AGENT, CO_OWNER): "mind_bob",
    (PERSONAL_AGENT, OWNER): "mind_alice",
    (OTHER_AGENT, CO_OWNER): "work_bob",
}
DONE = {"accept": "active", "decline": "declined", "update": "pending", "remove": "removed"}
NOT_ASKED = f"^Only {CO_OWNER} may {{action}} this contact$"
# fmt: off
GRID = [
    # Bob, whose agent was asked, decides the request on the page or through his personal room's agent.
    *((via, CO_OWNER, action, None) for via in (PAGE, PERSONAL_AGENT) for action in DONE),
    # Through another of his agents he can change or remove his side, but only his personal room's agent decides.
    (OTHER_AGENT, CO_OWNER, "accept", f"^Only mind_bob, the agent of {CO_OWNER}'s personal room, may accept this contact$"),
    (OTHER_AGENT, CO_OWNER, "decline", f"^Only mind_bob, the agent of {CO_OWNER}'s personal room, may decline this contact$"),
    (OTHER_AGENT, CO_OWNER, "update", None),
    (OTHER_AGENT, CO_OWNER, "remove", None),
    # Alice, whose agent asked, cannot decide it, but can change her side or withdraw the request.
    *((via, OWNER, action, NOT_ASKED.format(action=action)) for via in (PAGE, PERSONAL_AGENT) for action in ("accept", "decline")),
    *((via, OWNER, action, None) for via in (PAGE, PERSONAL_AGENT) for action in ("update", "remove")),
    # Mallory holds no side, so the contact does not exist for her, and Bob's agent does not act for her.
    *((PAGE, STRANGER, action, r"^There is no contact ") for action in DONE),
    *((PERSONAL_AGENT, STRANGER, action, f"^Only {CO_OWNER} may use the contacts of mind_bob$") for action in DONE),
]
# fmt: on


def _actor(config: Config, paths: RuntimePaths, runtime: _Runtime, via: str, owner: str) -> flow.ContactActor:
    if via == PAGE:
        return _portal(config, paths, runtime, owner)
    agent_name = AGENTS.get((via, owner), "mind_bob")
    return flow.tool_actor(_context(config, paths, agent_name, owner, runtime), agent_name)


@pytest.mark.asyncio
@pytest.mark.parametrize(("via", "owner", "action", "refused"), GRID)
async def test_each_owner_takes_on_each_path_only_the_actions_the_table_allows(
    tmp_path: Path,
    via: str,
    owner: str,
    action: str,
    refused: str | None,
) -> None:
    """Each owner's action on a pending request changes it as the table says, and a refusal changes nothing.

    Only a decision on the page answers the request's thread, as a tool decision is answered where it was made.
    """
    config, paths = _setup(tmp_path, work=True)
    runtime = _runtime()
    record = await _pending(config, paths, runtime)

    async def act() -> None:
        actor = _actor(config, paths, runtime, via, owner)
        if action == "accept":
            await flow.accept_contact(actor, record.id, rules=None, tools=None)
        elif action == "decline":
            await flow.decline_contact(actor, record.id)
        elif action == "update":
            await flow.update_contact(actor, record.id, rules="Be brief.", tools=None)
        else:
            await flow.remove_contact(actor, record.id)

    with _matrix(), _delivered("$decided") as send:
        if refused is None:
            await act()
        else:
            with pytest.raises(ValueError, match=refused):
                await act()

    (stored,) = _stored(paths)
    assert stored.status == ("pending" if refused else DONE[action])
    if action == "update" and refused is None:
        side = stored.owner_side(owner)
        assert side is not None
        assert stored.side(side).rules == "Be brief."
    answered = via == PAGE and action in {"accept", "decline"} and refused is None
    assert bool(_thread_posts(send, BOB_ROOM)) == answered
