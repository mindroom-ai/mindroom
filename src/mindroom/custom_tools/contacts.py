"""Contacts tools: ask other people's personal agents or people for contact, and manage the contacts you have."""

from __future__ import annotations

from dataclasses import replace
from typing import TYPE_CHECKING, Literal

from mindroom.background_tasks import run_blocking_until_complete
from mindroom.contacts import flow
from mindroom.requester_identity import resolve_human_requester_alias
from mindroom.tool_system.authoring import Toolkit
from mindroom.tool_system.runtime_context import get_tool_runtime_context
from mindroom.tool_system.tool_function import ToolGate

if TYPE_CHECKING:
    from mindroom.contacts.store import ContactRecord
    from mindroom.tool_system.runtime_context import ToolRuntimeContext
    from mindroom.tool_system.tool_function import ToolFunction

# The owner approves these on a card, because the model issuing the call also reads untrusted content.
_CONFIRMED = frozenset({"request_contact", "update_contact"})


def _require_context() -> ToolRuntimeContext:
    """Return the live request's context, refusing a call made outside one."""
    context = get_tool_runtime_context()
    if context is None:
        msg = "This tool only works while answering a request, and there is none."
        raise ValueError(msg)
    return context


def _asked(context: ToolRuntimeContext, record: ContactRecord) -> str:
    """Say where a request stands for the owner who just made it."""
    owner = resolve_human_requester_alias(context.requester_id, context.current_config, context.runtime_paths)
    if record.requester.owner != owner:
        return f"Contact {record.id} with {record.requester.id} is {record.status}."
    if record.status == "pending":
        return (
            f"Asked {record.target.owner} to approve contact {record.id} with {record.target.id}; "
            "you are told in this thread when they accept or decline."
        )
    return f"Contact {record.id} with {record.target.id} is {record.status}."


class ContactsTools(Toolkit):
    """Tools for the contacts of one personal agent, each acting for the owner who asks."""

    def __init__(self, agent_name: str) -> None:
        self.agent_name = agent_name
        super().__init__(
            name="contacts",
            tools=[
                self.list_contacts,
                self.request_contact,
                self.decline_contact,
                self.update_contact,
                self.remove_contact,
            ],
        )

    def offered_function(self, function: ToolFunction) -> ToolFunction:
        """Require confirmation of each request and each change of rules."""
        if function.name in _CONFIRMED:
            return replace(function, gate=ToolGate.CONFIRM)
        return function

    async def list_contacts(self) -> str:
        """List your owner's contacts with their IDs, status and your side's rules and tools, and the agents that can be asked.

        Pending requests that wait for your owner's answer show `you: target`.
        """
        try:
            return await run_blocking_until_complete(flow.list_contacts, _require_context(), self.agent_name)
        except ValueError as exc:
            return f"Error: {exc}"

    async def request_contact(
        self,
        target: str,
        reason: str,
        rules: str | None = None,
        tools: list[str] | None = None,
        party: Literal["agent", "me"] = "agent",
    ) -> str:
        """Ask to be in contact with another personal agent or a person, so their agent and yours can talk in a shared room.

        `target` is a personal agent's name or Matrix ID, or a person's Matrix ID. `reason` tells the other owner why you ask.
        `rules` are extra rules your agent follows in this contact, and `tools` are toolkit names, never function names, it may use there.
        With `party="me"` your owner asks as themself instead of through your agent, and then sets neither rules nor tools.
        Your owner approves the request on a card, then the other owner decides in their personal room;
        you are told in this thread when they accept or decline.
        """
        try:
            context = _require_context()
            record = await flow.request_contact(
                context,
                self.agent_name,
                target=target,
                reason=reason,
                rules=rules,
                tools=tools,
                party=party,
            )
        except ValueError as exc:
            return f"Error: {exc}"
        return _asked(context, record)

    async def decline_contact(self, contact_id: str) -> str:
        """Decline a pending contact request that asks your owner, and tell the thread it came from.

        The requester cannot ask again until your owner removes the declined contact.
        """
        try:
            record = await flow.decline_contact(_require_context(), self.agent_name, contact_id)
        except ValueError as exc:
            return f"Error: {exc}"
        return f"Declined contact {record.id}."

    async def update_contact(
        self,
        contact_id: str,
        rules: str | None = None,
        tools: list[str] | None = None,
    ) -> str:
        """Change the rules or tools of your owner's side of a contact; it applies from the next turn in that contact.

        Omit a field to keep it; blank `rules` remove your side's rules. `tools` replace the list and are toolkit names you have, never function names.
        Your owner approves the change on a card.
        """
        try:
            record = await flow.update_contact(
                _require_context(),
                self.agent_name,
                contact_id,
                rules=rules,
                tools=tools,
            )
        except ValueError as exc:
            return f"Error: {exc}"
        return f"Updated contact {record.id}."

    async def remove_contact(self, contact_id: str) -> str:
        """Remove a contact, which either owner may do; the agents leave its room and the owners keep it and its history."""
        try:
            record = await flow.remove_contact(_require_context(), self.agent_name, contact_id)
        except ValueError as exc:
            return f"Error: {exc}"
        return f"Removed contact {record.id}."
