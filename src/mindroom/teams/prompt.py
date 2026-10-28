# Adapted from Agno (https://github.com/agno-agi/agno), Copyright 2025-2026 Agno Inc., Apache License 2.0; modified by MindRoom.
"""The team leader's system prompt, rendered like an agent's: its identity, the ``<team>`` block, then the turn.

The team's identity (its name, its purpose, and how conversation history reaches it), the ``<team>`` block (the
capability sentence, the member roster with each member's authored role, and the mode's ``<delegation>`` block), and
the leader's tool instructions are the stable part; the date and session context with the turn's system enrichment
are the turn part (``agent_loop.prompt``).
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from mindroom.agent_loop.prompt import PromptSpec, render_system_messages
from mindroom.teams.spec import TeamMode

if TYPE_CHECKING:
    from collections.abc import Sequence

    from mindroom.llm.messages import Message
    from mindroom.teams.spec import TeamMember

_TEAM_OPENING_PROMPT = (
    "You lead this team to fulfill the user's request. Its members are listed below. "
    "Delegate to members when their expertise or tools are needed; answer directly when they are not.\n"
)

_TEAM_COORDINATE_DELEGATION_TEMPLATE = (
    "You work in coordinate mode: you hand sub-tasks to members with "
    "`delegate_task_to_member` and write the answer yourself.\n\n"
    "- Match each sub-task to the member whose role fits it best. When sub-tasks do not "
    "depend on each other, delegate them in the same turn instead of one per turn.\n"
    "- A member's output is evidence, not your answer. When a member fails, refuses, or returns "
    "nothing, say so plainly and name what it reported — never supply a cause, source, or finding "
    "the member did not state.\n"
    "- If a response is off-target, re-delegate with clearer instructions or try a better-suited "
    "member. If it still misses, answer with what you have and say what is missing — do not work "
    "through the roster.\n"
    "- Write one answer. Resolve contradictions, add structure, and fill gaps only where you can "
    "state the basis for it. Never concatenate member outputs.\n"
)

_TEAM_COLLABORATE_DELEGATION_TEMPLATE = (
    "You work in collaborate mode: one call puts the same task to every member, then you write the "
    "answer yourself.\n\n"
    "- Call `delegate_task_to_members` exactly once. One call reaches every member; do not call it "
    "once per member.\n"
    "- Write one whole question each member can answer from its own vantage point, not a sub-task "
    "for one of them.\n"
    "- A member's output is evidence, not your answer. When a member fails, refuses, or returns "
    "nothing, say so plainly and name what it reported — never supply a cause, source, or finding "
    "the member did not state.\n"
    "- Say each finding once: where members agree that is one finding, not three; where they "
    "conflict, reconcile it and say which view you took. Integrate the strongest contributions "
    "thematically — never list the responses in sequence.\n"
)

_TEAM_MEMBERS_CONTEXT_NOTE = (
    "\nMembers do not see this conversation. Each one gets only the text you write for it, so "
    "carry over every name, number and earlier answer it needs, and say what a good result looks "
    "like.\n"
)

_TEAM_MEMBER_IDS_NOTE = "Member ids are the ids shown in the roster above, used exactly as written.\n"

TEAM_MEMBER_NOT_FOUND_TEMPLATE = (
    "Member with ID {member_id} is not in this team. Choose a member from the list of members:\n\n{roster}"
)


def render_members_roster(members: Sequence[TeamMember]) -> str:
    """Return the ``<team_members>`` entries: one ``<member>`` per member, with its id, name, and authored role."""
    return "".join(
        f'<member id="{member.id}" name="{member.name}">\n  Role: {member.role}\n</member>\n' for member in members
    )


def render_leader_system_messages(
    members: Sequence[TeamMember],
    mode: TeamMode,
    *,
    identity: str,
    markdown: bool,
    tool_instructions: Sequence[str],
    turn_context: str,
) -> tuple[Message, ...]:
    """Return the leader's system messages for ``members`` in ``mode``.

    ``identity`` is the team's rendered identity (``TEAM_LEADER_IDENTITY_CONTEXT_TEMPLATE``, or its
    ``OPENAI_COMPAT_`` form in the OpenAI-compatible API), ``markdown`` whether the prompt states the chat's Markdown
    output format, as an agent's does, ``tool_instructions`` the leader's toolkit instructions in offer order, and
    ``turn_context`` the turn part: the date and session context and any appended system enrichment.
    """
    delegation = (
        _TEAM_COLLABORATE_DELEGATION_TEMPLATE + _TEAM_MEMBERS_CONTEXT_NOTE
        if mode is TeamMode.COLLABORATE
        else _TEAM_COORDINATE_DELEGATION_TEMPLATE + _TEAM_MEMBERS_CONTEXT_NOTE + _TEAM_MEMBER_IDS_NOTE
    )
    team = (
        "<team>\n"
        f"{_TEAM_OPENING_PROMPT}"
        "\n<team_members>\n"
        f"{render_members_roster(members)}"
        "</team_members>\n"
        f"\n<delegation>\n{delegation}</delegation>\n"
        "</team>"
    )
    return render_system_messages(
        PromptSpec(
            role=identity,
            markdown=markdown,
            instructions=(team,),
            tool_instructions=tuple(tool_instructions),
            turn_context=turn_context,
        ),
    )


__all__ = [
    "TEAM_MEMBER_NOT_FOUND_TEMPLATE",
    "render_leader_system_messages",
    "render_members_roster",
]
