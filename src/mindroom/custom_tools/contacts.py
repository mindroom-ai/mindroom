"""Contacts tools: ask other people's personal agents or people for contact, and manage the contacts you have."""

from __future__ import annotations

from dataclasses import replace
from typing import TYPE_CHECKING, Literal

from mindroom.background_tasks import run_blocking_until_complete
from mindroom.contacts import flow
from mindroom.tool_system.authoring import Toolkit
from mindroom.tool_system.runtime_context import require_tool_runtime_context
from mindroom.tool_system.tool_function import ToolGate

if TYPE_CHECKING:
    from mindroom.contacts.store import ContactRecord
    from mindroom.tool_system.tool_function import ToolFunction

_WRITE_ACTIONS: dict[str, tuple[tuple[str, ...], frozenset[str]]] = {
    "request": (("target", "reason"), frozenset({"target", "reason", "rules", "tools", "party"})),
    "accept": (("contact_id",), frozenset({"contact_id", "rules", "tools"})),
    "decline": (("contact_id",), frozenset({"contact_id"})),
    "update": (("contact_id",), frozenset({"contact_id", "rules", "tools"})),
    "remove": (("contact_id",), frozenset({"contact_id"})),
}


def _asked(owner: str, record: ContactRecord, *, posted: bool) -> str:
    """Say where a request stands for the owner who just made it; only a posted request notice asked anyone.

    A person who asked has no agent to tell in this thread, so they learn of an acceptance from the room invitation.
    """
    if posted and record.target.kind == "remote_agent":
        return (
            f"Asked {record.target.id}, an agent of another MindRoom deployment, for contact {record.id}; "
            "you are told in this thread when its owner accepts or declines."
        )
    if posted and record.requester.owner == owner:
        answer = (
            "you are told in this thread when they accept or decline"
            if record.requester.kind == "agent"
            else "you are invited to the contact room when they accept"
        )
        return f"Asked {record.target.owner} to approve contact {record.id} with {record.target.id}; {answer}."
    other = record.target if record.requester.owner == owner else record.requester
    room = f" in the contact room {record.room_id}" if record.status == "active" else ""
    return f"Contact {record.id} with {other.id} already exists and is {record.status}{room}; it is returned unchanged."


def _validate_write_params(action: str, params: dict[str, object]) -> str | None:
    spec = _WRITE_ACTIONS.get(action)
    if spec is None:
        return f"Error: Unsupported action {action!r}. Expected one of: {', '.join(_WRITE_ACTIONS)}."
    required, allowed = spec
    unexpected = [field for field, value in params.items() if value is not None and field not in allowed]
    if unexpected:
        return f"Error: Parameter(s) {', '.join(unexpected)} cannot be used with action {action!r}."
    missing = [field for field in required if params[field] is None]
    if missing:
        return f"Error: Parameter(s) {', '.join(missing)} required for action {action!r}."
    if params["party"] not in (None, "agent", "me"):
        return f"Error: Invalid party {params['party']!r}. Expected 'agent' or 'me'."
    return None


class ContactsTools(Toolkit):
    """Tools for the contacts of one personal agent, each acting for the owner who asks."""

    def __init__(self, agent_name: str) -> None:
        self.agent_name = agent_name
        super().__init__(
            name="contacts",
            tools=[
                self.contacts_read,
                self.contacts_write,
            ],
        )

    def offered_function(self, function: ToolFunction) -> ToolFunction:
        """Require confirmation of every contact write."""
        # The owner approves writes on a card, because the model issuing the call also reads untrusted content.
        if function.name == "contacts_write":
            return replace(function, gate=ToolGate.CONFIRM)
        return function

    async def contacts_read(self) -> str:
        """List your owner's contacts with their IDs, status, room, and your side's rules and tools, and the agents that can be asked.

        Takes no arguments. Pending requests that wait for your owner's answer show `you: target`.
        """
        try:
            return await run_blocking_until_complete(
                flow.list_contacts,
                require_tool_runtime_context(),
                self.agent_name,
            )
        except ValueError as exc:
            return f"Error: {exc}"

    async def _dispatch_write(
        self,
        action: Literal["request", "accept", "decline", "update", "remove"],
        *,
        contact_id: str | None,
        target: str | None,
        reason: str | None,
        rules: str | None,
        tools: list[str] | None,
        party: Literal["agent", "me"] | None,
    ) -> str:
        context = require_tool_runtime_context()
        if action == "request":
            assert target is not None
            assert reason is not None
            actor, record, posted = await flow.request_contact(
                context,
                self.agent_name,
                target=target,
                reason=reason,
                rules=rules,
                tools=tools,
                party="agent" if party is None else party,
            )
            return _asked(actor.owner, record, posted=posted) + flow.own_side_terms(actor, record)
        assert contact_id is not None
        actor = flow.tool_actor(context, self.agent_name)
        if action == "accept":
            record = await flow.accept_contact(actor, contact_id, rules=rules, tools=tools)
            return f"Accepted. Contact room: {record.room_id}." + flow.own_side_terms(actor, record)
        if action == "decline":
            record = await flow.decline_contact(actor, contact_id)
            return f"Declined contact {record.id}."
        if action == "update":
            record = await flow.update_contact(actor, contact_id, rules=rules, tools=tools)
            return f"Updated contact {record.id}." + flow.own_side_terms(actor, record)
        record = await flow.remove_contact(actor, contact_id)
        return f"Removed contact {record.id}."

    async def contacts_write(
        self,
        action: Literal["request", "accept", "decline", "update", "remove"],
        contact_id: str | None = None,
        target: str | None = None,
        reason: str | None = None,
        rules: str | None = None,
        tools: list[str] | None = None,
        party: Literal["agent", "me"] | None = None,
    ) -> str:
        """Request, answer, update, or remove a contact; your owner approves every write on a card.

        Actions:
        - `request`: Ask `target` (personal agent name/Matrix ID, person Matrix ID, or remote deployment agent Matrix ID) for contact with `reason`, optional `rules`, `tools` (toolkit names, never function names), and `party` (`"agent"` default or `"me"` as owner without rules/tools).
        - `accept`: Accept pending request `contact_id` to open a contact room; optional `rules` and `tools` (omit a field to keep what is set).
        - `decline`: Decline pending request `contact_id` and notify the requesting thread; requester cannot ask again until the declined contact is removed.
        - `update`: Change your side's `rules` or `tools` on `contact_id` from the next turn; omit a field to keep it, blank `rules` clear them, `tools` replaces the list.
        - `remove`: End `contact_id` and leave its room; only the declining owner removes a local decline, while your owner may remove a remote decline.
        """
        params: dict[str, object] = {
            "contact_id": contact_id,
            "target": target,
            "reason": reason,
            "rules": rules,
            "tools": tools,
            "party": party,
        }
        if (error := _validate_write_params(action, params)) is not None:
            return error
        try:
            return await self._dispatch_write(
                action,
                contact_id=contact_id,
                target=target,
                reason=reason,
                rules=rules,
                tools=tools,
                party=party,
            )
        except ValueError as exc:
            return f"Error: {exc}"
