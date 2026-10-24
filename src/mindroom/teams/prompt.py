# Adapted from Agno (https://github.com/agno-agi/agno), Copyright 2025-2026 Agno Inc., Apache License 2.0; modified by MindRoom.
"""The team leader's system message, laid out as Agno 3.0.9's team builder rendered it for MindRoom's teams.

MindRoom gives a team no description, role, or instructions of its own, so the leader prompt is the ``<team>`` block
(the capability sentence, the member roster, and the mode's ``<delegation>`` block) followed by the tool
instructions, the provider family's note, and the ``<additional_context>`` block. The texts are Agno's verbatim: they
are part of every leader request's prompt-cache prefix.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from mindroom.llm.messages import Message
from mindroom.teams.spec import TeamMode

if TYPE_CHECKING:
    from collections.abc import Sequence

    from mindroom.teams.spec import TeamMember

_TEAM_OPENING_PROMPT = (
    "You coordinate this team to fulfill the user's request. "
    "You have a team of specialists, listed below. "
    "Delegate to members when their expertise or tools are needed; "
    "answer directly — including with your own tools — when they are not.\n"
)

_TEAM_COORDINATE_DELEGATION_TEMPLATE = (
    "You work in coordinate mode: you hand sub-tasks to members with "
    "`delegate_task_to_member` and write the answer yourself.\n\n"
    "- Match each sub-task to the member whose role and description fit it best. When sub-tasks do not "
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
    "You work in broadcast mode: one call puts the same task to every member, then you write the "
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
    "Member with ID {member_id} not found in the team or any subteams. "
    "Please choose the correct member from the list of members:\n\n{roster}"
)


def render_members_roster(members: Sequence[TeamMember]) -> str:
    """Return the ``<team_members>`` entries: one ``<member>`` element per member with its id, name, and role."""
    return "".join(
        f'<member id="{member.spec.id}" name="{member.spec.name}">\n  Role: {member.role}\n</member>\n'
        for member in members
    )


def _delegation_block(mode: TeamMode) -> str:
    content = "\n<delegation>\n"
    if mode is TeamMode.COLLABORATE:
        content += _TEAM_COLLABORATE_DELEGATION_TEMPLATE + _TEAM_MEMBERS_CONTEXT_NOTE
    else:
        content += _TEAM_COORDINATE_DELEGATION_TEMPLATE + _TEAM_MEMBERS_CONTEXT_NOTE + _TEAM_MEMBER_IDS_NOTE
    return content + "</delegation>\n"


def render_leader_system_message(
    members: Sequence[TeamMember],
    mode: TeamMode,
    *,
    tool_instructions: Sequence[str],
    model_note: str | None,
    additional_context: str,
) -> Message:
    """Return the leader's system message for ``members`` in ``mode``.

    ``tool_instructions`` are the leader's toolkit instructions in offer order, ``model_note`` the provider family's
    text (``agent_loop.prompt.model_note_for``), and ``additional_context`` the per-turn context MindRoom adds: the
    date context and any appended system enrichment.
    """
    content = (
        "<team>\n"
        f"{_TEAM_OPENING_PROMPT}"
        "\n<team_members>\n"
        f"{render_members_roster(members)}"
        "</team_members>\n"
        f"{_delegation_block(mode)}"
        "</team>\n\n"
    )
    content += "".join(f"{instruction}\n" for instruction in tool_instructions)
    content += model_note or ""
    content += f"<additional_context>\n{additional_context.strip()}\n</additional_context>\n\n"
    return Message(role="system", content=content.strip())


__all__ = [
    "TEAM_MEMBER_NOT_FOUND_TEMPLATE",
    "render_leader_system_message",
    "render_members_roster",
]
