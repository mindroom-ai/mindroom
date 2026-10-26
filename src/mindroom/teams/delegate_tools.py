# Adapted from Agno (https://github.com/agno-agi/agno), Copyright 2025-2026 Agno Inc., Apache License 2.0; modified by MindRoom.
"""The leader's delegation tools and what they tell the leader about a member's run.

A coordinate team offers ``delegate_task_to_member(member_id, task)``, a collaborate team
``delegate_task_to_members(task)``. ``DELEGATION_TOOLS`` declares each one's definition once, and each run binds it
to the team run it serves: a member's paused requirements are copied onto that run, naming the member, which pauses
the leader's loop after the batch. The call reads the member's pause notice until the member's resumed run ends and
its result answers the call (``answer_delegation``).
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
from mindroom.tool_system.invocation import current_tool_call
from mindroom.tool_system.tool_function import AdaptedToolkit, ToolFunction

if TYPE_CHECKING:
    from collections.abc import Callable, Coroutine, Mapping, Sequence

    from mindroom.llm.runs import RunRecord, RunRequirement, TeamRunRecord, ToolExecution
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
        requires_confirmation=None,
        external_execution=None,
        approval_type=None,
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
        requires_confirmation=None,
        external_execution=None,
        approval_type=None,
    ),
}


def delegate_functions(
    members: Sequence[TeamMember],
    mode: TeamMode,
    *,
    record: TeamRunRecord,
    run_member: MemberRunner,
    stream: bool,
) -> AdaptedToolkit:
    """Return the delegation tool for ``mode`` as a toolkit bound to the team run ``record``.

    ``stream`` says whether the turn's reply streams, which selects the rules of a member's result. The
    arguments are checked against the entrypoint's signature before it runs, so a call with a missing or mistyped
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
            _running_call(record).child_run_id = child.run_id
            return MEMBER_PAUSED_RESULT.format(name=member.spec.name)
        return member_result_text(child, stream=stream)

    async def delegate_task_to_members(task: str) -> str:
        """Run every member on ``task`` at once and return what the leader reads of their runs."""
        async with asyncio.TaskGroup() as group:
            runs = [group.create_task(run_member(member, task)) for member in members]
        return collaborate_result_text(members, [run.result() for run in runs], record, stream=stream)

    entrypoint = delegate_task_to_members if mode is TeamMode.COLLABORATE else delegate_task_to_member
    definition = DELEGATION_TOOLS[mode]
    function = ToolFunction(
        name=definition.name,
        description=definition.description,
        parameters=definition.parameters,
        entrypoint=validate_call(entrypoint),
    )
    return AdaptedToolkit(name="team_delegation", functions=(function,), instructions=None)


def member_result_text(child: RunRecord, *, stream: bool) -> str:
    """Return what a coordinate leader reads for a finished member run, by the rules of the leader's stream mode.

    The rules read the content the run hands over (``handover_content``), so a failed member gives the reason its
    run recorded, credentials redacted.
    """
    # TODO(simplify): The rules differ by stream mode: without streaming, a run whose text is blank and that ran no
    # tools reads empty. Use the streamed rules in both modes. Models would notice.
    return _streamed_result(child) if stream else _blocking_result(child)


def _streamed_result(child: RunRecord) -> str:
    """Return the member's text, else its tool results, else the no-response notice."""
    content = child.handover_content
    if content is not None and content.strip():
        return content
    if child.tools:
        return _tool_results(child)
    return NO_MEMBER_RESPONSE


def _blocking_result(child: RunRecord) -> str:
    """Return the streamed result, except that a blank run without tools reads empty."""
    content = child.handover_content
    if content is not None and not content.strip() and not child.tools:
        return ""
    return _streamed_result(child)


def _tool_results(child: RunRecord) -> str:
    return ",".join(tool.result for tool in child.tools or () if tool.result)


def collaborate_result_text(
    members: Sequence[TeamMember],
    children: Sequence[RunRecord],
    record: TeamRunRecord,
    *,
    stream: bool,
) -> str:
    """Return the collaborate result: one piece per member in member order, joined with no separator.

    Member order keeps the result deterministic whatever order the members finish in. Streaming, a piece is the
    member's result (``_streamed_result``); blocking, it is the result labeled with the member's name
    (``_labeled_result``). A paused member contributes its labeled pause notice and its requirements reach the team
    run.
    """
    # TODO(simplify): Pieces join with no separator, and a finished member's piece is labeled only without streaming,
    # by a third result rule (``_labeled_result``). Label every piece by one rule and separate pieces with blank lines.
    # Models would notice.
    pieces: list[str] = []
    for member, child in zip(members, children, strict=True):
        if child.status is RunStatus.paused:
            propagate_member_pause(record, member, child)
            pieces.append(_labeled_piece(member.spec.name, _COLLABORATE_PAUSED_NOTICE))
        elif stream:
            pieces.append(_streamed_result(child))
        else:
            pieces.append(_labeled_piece(member.spec.name, _labeled_result(child)))
    return "".join(pieces)


def _labeled_result(child: RunRecord) -> str:
    """Return ``_streamed_result``, but blank text gives all tool results, empty ones too, else ``No Response``."""
    content = child.handover_content
    if content is not None and not content.strip():
        return ",".join(tool.result or "" for tool in child.tools) if child.tools else "No Response"
    return _streamed_result(child)


def _labeled_piece(name: str, text: str) -> str:
    """Return one member's piece of a collaborate result under the member's name."""
    return f"Agent {name}: {text}"


def answer_delegation(
    tools: Sequence[ToolExecution],
    member: TeamMember,
    run: RunRecord,
    *,
    stream: bool,
) -> ToolExecution | None:
    """Answer the leader's delegation call that waited on ``member``'s paused run, which ended as ``run``.

    The call still holds the pause notice it gave, under the display name ``run`` stored when it started, so a member
    renamed since is still found. A coordinate call names the member and reads the member's result as a call whose
    member never paused reads it (``member_result_text``). A collaborate call holds one piece per member: the member's
    pause notice becomes its result under the same label, and every other piece stays as it was, so the leader keeps
    the result of a member that finished before the pause or in an earlier resume. ``stream`` selects the result
    rules. Return the answered call, or None when no call waits on the run.
    """
    # TODO(simplify): A resumed member that wrote nothing hands over its carried pause notice ("I have tools to
    # execute, but I need confirmation.") after its call ran. Hand over only what the resume added
    # (``new_handover_content``) or a completion note. The leader model would notice.
    result = member_result_text(run, stream=stream)
    name = run.agent_display_name or member.spec.name
    coordinate = _waiting_coordinate_call(tools, member.spec.id, run, MEMBER_PAUSED_RESULT.format(name=name))
    if coordinate is not None:
        coordinate.result = result
        return coordinate
    notice = _labeled_piece(name, _COLLABORATE_PAUSED_NOTICE)
    for tool in tools:
        if tool.tool_name == DELEGATION_TOOLS[TeamMode.COLLABORATE].name and tool.result and notice in tool.result:
            tool.result = tool.result.replace(notice, _labeled_piece(name, result), 1)
            return tool
    return None


def _waiting_coordinate_call(
    tools: Sequence[ToolExecution],
    member_id: str,
    run: RunRecord,
    notice: str,
) -> ToolExecution | None:
    """Return the coordinate call that waits on ``run``, a paused run of the member ``member_id``, or None.

    A waiting call names the member and still holds ``notice``, its pause notice; it names the run it started in
    ``child_run_id``, which tells two calls to one member apart.
    """
    waiting = [
        tool
        for tool in tools
        if tool.tool_name == DELEGATION_TOOLS[TeamMode.COORDINATE].name
        and (tool.tool_args or {}).get("member_id") == member_id
        and tool.result == notice
    ]
    # LEGACY_COMPAT: Coordinate calls of a streamed Agno-runtime turn that all name the member run that ended last.
    # Legacy format: in a team row a streamed turn paused on the Agno runtime, every ``delegate_task_to_member``
    # entry holds in ``child_run_id`` the run of the member whose delegation ended last, so a waiting call may name a
    # sibling's run, and a call whose member finished may name the paused member's run. Blocking turns stored null.
    # Last legacy release: v2026.10.498, the last release on the Agno runtime; replacement: v2026.10.499, whose team
    # runs stored ``child_run_id`` null (the rule below).
    # Handling: a link counts only on a call that still waits on the member, so a finished sibling's call keeps its
    # answer; a run no waiting call links reads as the null-link rows below do.
    # Coverage: tests/teams/test_continue_team.py::test_a_row_a_streamed_agno_turn_paused_answers_each_call_with_its_own_member_s_result
    linked = next((tool for tool in waiting if tool.child_run_id == run.run_id), None)
    if linked is not None:
        return linked
    # LEGACY_COMPAT: Coordinate calls that wait on a paused member without naming the member's run.
    # Legacy format: a paused team row's waiting ``delegate_task_to_member`` entry whose ``child_run_id`` is null.
    # Last legacy release: v2026.10.574, whose team runs stored ``child_run_id`` null on every delegation call, as
    # blocking turns on the Agno runtime did; replacement: the next release names the paused member's run in
    # ``child_run_id`` of the call that waits on it.
    # Handling: the first call that waits on the member answers its paused run, so an approval saved before the
    # upgrade still answers it with the member's result; two such calls to one member are answered in the order the
    # member's runs resume.
    # Coverage: tests/teams/test_continue_team.py::test_an_approved_member_call_resumes_into_the_team_approved_rows,
    #   tests/teams/test_delegate_tools.py::test_a_saved_coordinate_call_without_its_member_run_waits_on_the_member_it_names,
    #   tests/teams/test_delegate_tools.py::test_two_saved_calls_to_one_member_are_answered_in_the_order_its_runs_resume
    return next(iter(waiting), None)


def _running_call(record: TeamRunRecord) -> ToolExecution:
    """Return the leader's entry of the delegation call being run, which the loop records before it runs the call.

    A provider may reuse a call id in a later request, so the call is the latest entry under its id.
    """
    call = current_tool_call()
    assert call is not None, "a delegation runs only as a call of its team run's loop"
    return next(tool for tool in reversed(record.tools or ()) if tool.tool_call_id == call.context.call_id)


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
    "DELEGATION_TOOLS",
    "MEMBER_PAUSED_RESULT",
    "NO_MEMBER_RESPONSE",
    "MemberRunner",
    "answer_delegation",
    "collaborate_result_text",
    "delegate_functions",
    "member_requirement",
    "member_result_text",
    "propagate_member_pause",
]
