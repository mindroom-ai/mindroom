# Adapted from Agno (https://github.com/agno-agi/agno), Copyright 2025-2026 Agno Inc., Apache License 2.0; modified by MindRoom.
"""The leader's delegation tools and what they tell the leader about a member's run.

A coordinate team offers ``delegate_task_to_member(member_id, task)``, a collaborate team
``delegate_task_to_members(task)``. ``DELEGATION_TOOLS`` declares each one's definition once, and each run binds it
to the team run it serves: a member's paused requirements are copied onto that run, naming the member, which pauses
the leader's loop after the batch.
"""

from __future__ import annotations

import asyncio
from copy import deepcopy
from dataclasses import replace
from typing import TYPE_CHECKING, Any

from pydantic import validate_call

from mindroom.llm.provider import ToolSpec
from mindroom.llm.runs import RunStatus
from mindroom.teams.prompt import TEAM_MEMBER_NOT_FOUND_TEMPLATE, render_members_roster
from mindroom.teams.spec import TeamMode
from mindroom.tool_system.tool_function import AdaptedToolkit, ToolFunction

if TYPE_CHECKING:
    from collections.abc import Callable, Coroutine, Mapping, Sequence

    from mindroom.llm.runs import RunRecord, RunRequirement, TeamRunRecord, ToolExecution
    from mindroom.teams.spec import TeamMember

# What the leader reads for a member that paused, per mode, and for one that said nothing.
MEMBER_PAUSED_RESULT = "Member '{name}' requires human input before continuing."
COLLABORATE_MEMBER_PAUSED_RESULT = "Agent {name}: Requires human input before continuing."
NO_MEMBER_RESPONSE = "No response from the member agent."

# Runs one member on the leader's task and returns its terminal record, whatever its status.
type MemberRunner = Callable[[TeamMember, str], Coroutine[Any, Any, RunRecord]]

# The delegation tool each mode offers, as the leader's model reads it; each parameter's description opens with its type.
DELEGATION_TOOLS: Mapping[TeamMode, ToolSpec] = {
    TeamMode.COORDINATE: ToolSpec(
        name="delegate_task_to_member",
        description="Use this function to delegate a task to the selected team member.",
        parameters={
            "type": "object",
            "properties": {
                "member_id": {
                    "type": "string",
                    "description": "(str) The ID of the member to delegate the task to, exactly as it appears in"
                    " <team_members>.",
                },
                "task": {
                    "type": "string",
                    "description": "(str) A clear and concise description of the task the member should achieve,"
                    " including what a good result looks like.",
                },
            },
            "required": ["member_id", "task"],
            "additionalProperties": False,
        },
        strict=None,
    ),
    TeamMode.COLLABORATE: ToolSpec(
        name="delegate_task_to_members",
        description=(
            "Use this function to send one task to every member agent and return their responses.\n"
            "One call reaches all members; do not call it once per member."
        ),
        parameters={
            "type": "object",
            "properties": {
                "task": {
                    "type": "string",
                    "description": "(str) A clear and concise description of the task to send to member agents,"
                    " including what a good result looks like.",
                },
            },
            "required": ["task"],
            "additionalProperties": False,
        },
        strict=None,
    ),
}


def delegate_functions(
    members: Sequence[TeamMember],
    mode: TeamMode,
    *,
    record: TeamRunRecord,
    run_member: MemberRunner,
) -> AdaptedToolkit:
    """Return the delegation tool for ``mode`` as a toolkit bound to the team run ``record``.

    The arguments are checked against the entrypoint's signature before it runs, so a call with a missing or mistyped
    argument fails with the validation error the model reads.
    """
    by_id = {member.spec.id: member for member in members}

    async def delegate_task_to_member(member_id: str, task: str) -> str:
        """Run the member ``member_id`` on ``task`` and return what the leader reads of its run."""
        member = by_id.get(member_id)
        if member is None:
            return TEAM_MEMBER_NOT_FOUND_TEMPLATE.format(member_id=member_id, roster=render_members_roster(members))
        child = await run_member(member, task)
        if child.status is RunStatus.paused:
            propagate_member_pause(record, member, child)
            return MEMBER_PAUSED_RESULT.format(name=member.spec.name)
        return member_result_text(child)

    async def delegate_task_to_members(task: str) -> str:
        """Run every member on ``task`` at once and return what the leader reads of their runs."""
        async with asyncio.TaskGroup() as group:
            runs = [group.create_task(run_member(member, task)) for member in members]
        return collaborate_result_text(members, [run.result() for run in runs], record)

    entrypoint = delegate_task_to_members if mode is TeamMode.COLLABORATE else delegate_task_to_member
    definition = DELEGATION_TOOLS[mode]
    function = ToolFunction(
        name=definition.name,
        description=definition.description,
        parameters=definition.parameters,
        entrypoint=validate_call(entrypoint),
    )
    return AdaptedToolkit(name="team_delegation", functions=(function,), instructions=None)


def member_result_text(child: RunRecord) -> str:
    """Return what the leader reads for a finished member run: its text, else its tool results, else a notice.

    The text is the content the run hands over (``handover_content``), so a failed member gives the reason its run
    recorded, credentials redacted. A run without text that ran tools gives their results, and one that did neither
    gives the no-response notice.
    """
    content = child.handover_content
    if content is not None and content.strip():
        return content
    if child.tools:
        return ",".join(tool.result for tool in child.tools if tool.result)
    return NO_MEMBER_RESPONSE


def collaborate_result_text(
    members: Sequence[TeamMember],
    children: Sequence[RunRecord],
    record: TeamRunRecord,
) -> str:
    """Return the collaborate result: one piece per member in member order, a blank line between pieces.

    Member order keeps the result deterministic whatever order the members finish in. A piece is the member's result
    (``member_result_text``) labeled with the member's name. A paused member contributes its pause notice, labeled the
    same way, and its requirements reach the team run.
    """
    pieces: list[str] = []
    for member, child in zip(members, children, strict=True):
        if child.status is RunStatus.paused:
            propagate_member_pause(record, member, child)
            pieces.append(COLLABORATE_MEMBER_PAUSED_RESULT.format(name=member.spec.name))
        else:
            pieces.append(f"Agent {member.spec.name}: {member_result_text(child)}")
    return "\n\n".join(pieces)


def waited_on_member(tool: ToolExecution, member: TeamMember) -> bool:
    """Return whether the leader's delegation call ``tool`` waited on ``member``, a member whose run is paused.

    It did when the delegation answered with the notice it gives when that member pauses: a coordinate call that
    named the member and answered exactly its notice, or a collaborate call whose result holds the member's piece.
    A finished member's own text that merely mentions human input never matches.
    """
    if tool.tool_name == "delegate_task_to_member":
        named = (tool.tool_args or {}).get("member_id") == member.spec.id
        return named and tool.result == MEMBER_PAUSED_RESULT.format(name=member.spec.name)
    if tool.tool_name == "delegate_task_to_members":
        return COLLABORATE_MEMBER_PAUSED_RESULT.format(name=member.spec.name) in (tool.result or "")
    return False


def propagate_member_pause(record: TeamRunRecord, member: TeamMember, child: RunRecord) -> None:
    """Copy the paused member's requirements onto the team run, naming the member, and keep its run with the team's."""
    if record.requirements is None:
        record.requirements = []
    record.requirements.extend(
        member_requirement(requirement, member, child.run_id) for requirement in child.requirements or ()
    )
    record.member_responses.append(child)


def member_requirement(requirement: RunRequirement, member: TeamMember, run_id: str) -> RunRequirement:
    """Return a copy of ``requirement`` with a call of its own, naming ``member`` and its run ``run_id``.

    The copy keeps the requirement id and tool call id, which approval cards and delegation gates key by
    (``member_id:call_id``); ``member_run_id`` is how a resume finds the member run among ``member_responses``.
    """
    return replace(
        requirement,
        tool_execution=deepcopy(requirement.tool_execution),
        member_agent_id=member.spec.id,
        member_display_name=member.spec.name,
        member_run_id=run_id,
    )


__all__ = [
    "COLLABORATE_MEMBER_PAUSED_RESULT",
    "DELEGATION_TOOLS",
    "MEMBER_PAUSED_RESULT",
    "NO_MEMBER_RESPONSE",
    "MemberRunner",
    "collaborate_result_text",
    "delegate_functions",
    "member_requirement",
    "member_result_text",
    "propagate_member_pause",
    "waited_on_member",
]
