# Adapted from Agno (https://github.com/agno-agi/agno), Copyright 2025-2026 Agno Inc., Apache License 2.0; modified by MindRoom.
"""The leader's delegation tools and what they tell the leader about a member's run.

A coordinate team offers ``delegate_task_to_member(member_id, task)``, a collaborate team
``delegate_task_to_members(task)``. ``DELEGATION_TOOLS`` declares each one's definition once, and each run binds it
to the team run it serves: a paused member run joins that run's ``member_responses``, whose paused calls the team run
then waits on, naming the member, which pauses the leader's loop after the batch. The call reads the member's pause
notice until the member's resumed run ends and its result answers the call (``answer_delegation``).
"""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING, Any

from mindroom.llm.provider import ToolSpec
from mindroom.llm.runs import RunStatus
from mindroom.teams.prompt import TEAM_MEMBER_NOT_FOUND_TEMPLATE, render_members_roster
from mindroom.teams.spec import TeamMode
from mindroom.tool_system.invocation import current_tool_call
from mindroom.tool_system.tool_function import AdaptedToolkit, ToolFunction
from mindroom.tool_system.tool_schema import validated_entrypoint

if TYPE_CHECKING:
    from collections.abc import Callable, Coroutine, Mapping, Sequence

    from mindroom.llm.runs import RunRecord, TeamRunRecord, ToolExecution
    from mindroom.teams.spec import TeamMember

# What a coordinate call holds while its member is paused, and what the leader reads for a member that said nothing.
MEMBER_PAUSED_RESULT = "Member '{name}' requires human input before continuing."
NO_MEMBER_RESPONSE = "No response from the member agent."
# What a paused member's piece of a collaborate result says, under the member's label (``_labeled_piece``).
_COLLABORATE_PAUSED_NOTICE = "Requires human input before continuing."

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
    by_id = {member.id: member for member in members}

    async def delegate_task_to_member(member_id: str, task: str) -> str:
        """Run the member ``member_id`` on ``task`` and return what the leader reads of its run."""
        member = by_id.get(member_id)
        if member is None:
            return TEAM_MEMBER_NOT_FOUND_TEMPLATE.format(member_id=member_id, roster=render_members_roster(members))
        child = await run_member(member, task)
        if child.status is RunStatus.paused:
            # The team run waits on the paused member run's calls (``TeamRunRecord.requirements``).
            record.member_responses.append(child)
            _running_call(record).child_run_id = child.run_id
            return MEMBER_PAUSED_RESULT.format(name=member.name)
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
        entrypoint=validated_entrypoint(entrypoint),
    )
    return AdaptedToolkit(name="team_delegation", functions=(function,), instructions=None)


def member_result_text(child: RunRecord) -> str:
    """Return what the leader reads for a finished member run: its text, else its tool results, else a notice.

    The text is the content the run hands over (``handover_content``): a resumed run's is what it wrote before its
    pause and since, and a failed member gives the reason its run recorded, credentials redacted. A run without text
    that ran tools gives their results, and one that did neither gives the no-response notice.
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
    same way, and the team run waits on its paused calls.
    """
    pieces: list[str] = []
    for member, child in zip(members, children, strict=True):
        if child.status is RunStatus.paused:
            record.member_responses.append(child)
            pieces.append(_labeled_piece(member.name, _COLLABORATE_PAUSED_NOTICE))
        else:
            pieces.append(_labeled_piece(member.name, member_result_text(child)))
    return "\n\n".join(pieces)


def _labeled_piece(name: str, text: str) -> str:
    """Return one member's piece of a collaborate result under the member's name."""
    return f"Agent {name}: {text}"


def answer_delegation(
    tools: Sequence[ToolExecution],
    member: TeamMember,
    run: RunRecord,
) -> ToolExecution | None:
    """Answer the leader's delegation call that waited on ``member``'s paused run, which ended as ``run``.

    The call still holds the pause notice it gave, under the display name ``run`` stored when it started, so a member
    renamed since is still found. A coordinate call names the member's run and reads its result as a call whose
    member never paused reads it (``member_result_text``). A collaborate call holds one piece per member: the member's
    pause notice becomes its result under the same label, and every other piece stays as it was, so the leader keeps
    the result of a member that finished before the pause or in an earlier resume. Return the answered call, or None
    when no call waits on the run.
    """
    result = member_result_text(run)
    name = run.agent_display_name or member.name
    coordinate = _waiting_coordinate_call(tools, run, MEMBER_PAUSED_RESULT.format(name=name))
    if coordinate is not None:
        coordinate.result = result
        return coordinate
    notice = _labeled_piece(name, _COLLABORATE_PAUSED_NOTICE)
    for tool in tools:
        if tool.tool_name == DELEGATION_TOOLS[TeamMode.COLLABORATE].name and tool.result and notice in tool.result:
            tool.result = tool.result.replace(notice, _labeled_piece(name, result), 1)
            return tool
    return None


def _waiting_coordinate_call(tools: Sequence[ToolExecution], run: RunRecord, notice: str) -> ToolExecution | None:
    """Return the coordinate call that waits on ``run``, a member's paused run, or None.

    A waiting call names the run it started in ``child_run_id`` and still holds ``notice``, its pause notice.
    """
    return next(
        (
            tool
            for tool in tools
            if tool.tool_name == DELEGATION_TOOLS[TeamMode.COORDINATE].name
            and tool.child_run_id == run.run_id
            and tool.result == notice
        ),
        None,
    )


def _running_call(record: TeamRunRecord) -> ToolExecution:
    """Return the leader's entry of the delegation call being run, which the loop records before it runs the call.

    A provider may reuse a call id in a later request, so the call is the latest entry under its id.
    """
    call = current_tool_call()
    assert call is not None, "a delegation runs only as a call of its team run's loop"
    return next(tool for tool in reversed(record.tools or ()) if tool.tool_call_id == call.context.call_id)


__all__ = [
    "DELEGATION_TOOLS",
    "MEMBER_PAUSED_RESULT",
    "NO_MEMBER_RESPONSE",
    "MemberRunner",
    "answer_delegation",
    "collaborate_result_text",
    "delegate_functions",
    "member_result_text",
]
