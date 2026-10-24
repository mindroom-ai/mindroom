# Adapted from Agno (https://github.com/agno-agi/agno), Copyright 2025-2026 Agno Inc., Apache License 2.0; modified by MindRoom.
"""The leader's delegation tools and what they tell the leader about a member's run.

A coordinate team offers ``delegate_task_to_member(member_id, task)``, a collaborate team
``delegate_task_to_members(task)``. Both are authored as Agno functions, as every MindRoom tool is, and both close
over the team run they serve: a member's paused requirements are copied onto that run, naming the member, which
pauses the leader's loop after the batch.
"""

from __future__ import annotations

import asyncio
import json
from copy import deepcopy
from dataclasses import replace
from typing import TYPE_CHECKING, Any

from mindroom.llm.runs import RunStatus
from mindroom.teams.prompt import TEAM_MEMBER_NOT_FOUND_TEMPLATE, render_members_roster
from mindroom.teams.spec import TeamMode
from mindroom.tool_system.agno_toolkits import Function, adapt_function
from mindroom.tool_system.tool_function import AdaptedToolkit

if TYPE_CHECKING:
    from collections.abc import Callable, Coroutine, Sequence

    from mindroom.llm.runs import RunRecord, TeamRunRecord, ToolExecution
    from mindroom.teams.spec import TeamMember

# What the leader reads for a member that paused, per mode, and for one that said nothing.
MEMBER_PAUSED_RESULT = "Member '{name}' requires human input before continuing."
COLLABORATE_MEMBER_PAUSED_RESULT = "Agent {name}: Requires human input before continuing."
NO_MEMBER_RESPONSE = "No response from the member agent."

# Runs one member on the leader's task and returns its terminal record, whatever its status.
type MemberRunner = Callable[[TeamMember, str], Coroutine[Any, Any, RunRecord]]


def delegate_functions(
    members: Sequence[TeamMember],
    mode: TeamMode,
    *,
    record: TeamRunRecord,
    run_member: MemberRunner,
    stream: bool,
) -> AdaptedToolkit:
    """Return the delegation tool for ``mode`` as a toolkit bound to the team run ``record``.

    ``stream`` says whether the leader's requests stream, which selects the rules of a member's result.
    """
    by_id = {member.spec.id: member for member in members}

    async def delegate_task_to_member(member_id: str, task: str) -> str:
        """Use this function to delegate a task to the selected team member.

        Args:
            member_id (str): The ID of the member to delegate the task to, exactly as it appears in <team_members>.
            task (str): A clear and concise description of the task the member should achieve, including what a good result looks like.

        Returns:
            str: The result of the delegated task.

        """
        member = by_id.get(member_id)
        if member is None:
            return TEAM_MEMBER_NOT_FOUND_TEMPLATE.format(member_id=member_id, roster=render_members_roster(members))
        child = await run_member(member, task)
        if child.status is RunStatus.paused:
            propagate_member_pause(record, member, child)
            return MEMBER_PAUSED_RESULT.format(name=member.spec.name)
        return member_result_text(child, stream=stream)

    async def delegate_task_to_members(task: str) -> str:
        """Use this function to send one task to every member agent and return their responses.
        One call reaches all members; do not call it once per member.

        Args:
            task (str): A clear and concise description of the task to send to member agents, including what a good result looks like.

        Returns:
            str: The result of the delegated task.

        """  # noqa: D205 - the two-line summary is the tool description the model reads.
        async with asyncio.TaskGroup() as group:
            runs = [group.create_task(run_member(member, task)) for member in members]
        return collaborate_result_text(members, [run.result() for run in runs], record, stream=stream)

    entrypoint = delegate_task_to_members if mode is TeamMode.COLLABORATE else delegate_task_to_member
    function = adapt_function(Function.from_callable(entrypoint, name=entrypoint.__name__))
    return AdaptedToolkit(name="team_delegation", functions=(function,), instructions=None)


def member_result_text(child: RunRecord, *, stream: bool) -> str:
    """Return what a coordinate leader reads for a finished member run, by the rules of the leader's stream mode.

    The rules read the content the run hands over (``handover_content``), so a failed member gives the reason its
    run recorded, credentials redacted.
    """
    # TODO(simplify): The rules differ by stream mode: without streaming, a run with tools but no content reads
    # ``null`` and a blank run without tools reads empty. Use the streamed rules in both modes. Models would notice.
    return _streamed_result(child) if stream else _blocking_result(child)


def _streamed_result(child: RunRecord) -> str:
    """Return the member's text, else its tool results, else the no-response notice."""
    content = child.handover_content
    if isinstance(content, str) and content.strip():
        return content
    if isinstance(content, dict):
        return _json_text(content)
    if child.tools:
        return _tool_results(child)
    return NO_MEMBER_RESPONSE


def _blocking_result(child: RunRecord) -> str:
    """Return the blocking result: the no-response notice only for a run with neither content nor tools.

    A run whose text is blank gives its tool results, which without tools leaves nothing; any other content, None
    among it, is JSON.
    """
    content = child.handover_content
    if content is None and not child.tools:
        return NO_MEMBER_RESPONSE
    if isinstance(content, str):
        return content if content.strip() else _tool_results(child)
    return _json_text(content)


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

    Streaming, a piece is the member's result (``_streamed_result``); blocking, it is the result labeled with the
    member's name (``_labeled_result``). A paused member contributes its pause notice and its requirements reach the
    team run.
    """
    # TODO(simplify): Pieces join with no separator, and a finished member's piece is labeled only without streaming,
    # by a third result rule (``_labeled_result``). Label every piece by one rule and separate pieces with blank lines.
    # Models would notice.
    pieces: list[str] = []
    for member, child in zip(members, children, strict=True):
        if child.status is RunStatus.paused:
            propagate_member_pause(record, member, child)
            pieces.append(COLLABORATE_MEMBER_PAUSED_RESULT.format(name=member.spec.name))
        elif stream:
            pieces.append(_streamed_result(child))
        else:
            pieces.append(f"Agent {member.spec.name}: {_labeled_result(child)}")
    return "".join(pieces)


def _labeled_result(child: RunRecord) -> str:
    """Return ``_blocking_result``, but blank text gives all tool results, empty ones too, else ``No Response``."""
    content = child.handover_content
    if isinstance(content, str) and not content.strip():
        return ",".join(tool.result or "" for tool in child.tools) if child.tools else "No Response"
    return _blocking_result(child)


def _json_text(content: object) -> str:
    return json.dumps(content, indent=2, ensure_ascii=False)


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
    """Copy the paused member's requirements onto the team run, naming the member, and keep its run with the team's.

    The copies keep the requirement ids and tool call ids, which approval cards and delegation gates key by
    (``member_id:call_id``); ``member_run_id`` is how a resume finds the member run among ``member_responses``.
    """
    if record.requirements is None:
        record.requirements = []
    for requirement in child.requirements or ():
        record.requirements.append(
            replace(
                requirement,
                tool_execution=deepcopy(requirement.tool_execution),
                member_agent_id=member.spec.id,
                member_agent_name=member.spec.name,
                member_run_id=child.run_id,
            ),
        )
    record.member_responses.append(child)


__all__ = [
    "COLLABORATE_MEMBER_PAUSED_RESULT",
    "MEMBER_PAUSED_RESULT",
    "NO_MEMBER_RESPONSE",
    "MemberRunner",
    "collaborate_result_text",
    "delegate_functions",
    "member_result_text",
    "propagate_member_pause",
    "waited_on_member",
]
