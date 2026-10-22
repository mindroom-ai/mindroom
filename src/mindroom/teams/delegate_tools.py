# Adapted from Agno (https://github.com/agno-agi/agno), Copyright 2025-2026 Agno Inc., Apache License 2.0; modified by MindRoom.
"""The leader's delegation tools and what they tell the leader about a member's run.

A coordinate team offers ``delegate_task_to_member(member_id, task)``, a collaborate team
``delegate_task_to_members(task)``. Both are authored as Agno functions so Agno derives their schemas, as it did for
its own, and both close over the team run they serve: a member's paused requirements are copied onto that run,
naming the member, which pauses the leader's loop after the batch.
"""

from __future__ import annotations

import asyncio
import json
from copy import deepcopy
from dataclasses import replace
from typing import TYPE_CHECKING, Any, Protocol

from mindroom.llm.runs import RunStatus
from mindroom.teams.prompt import TEAM_MEMBER_NOT_FOUND_TEMPLATE, render_members_roster
from mindroom.teams.spec import TeamMode
from mindroom.tool_system.agno_toolkits import Function, adapt_function
from mindroom.tool_system.tool_function import AdaptedToolkit

if TYPE_CHECKING:
    from collections.abc import Coroutine, Sequence

    from mindroom.llm.runs import RunRecord, TeamRunRecord
    from mindroom.teams.spec import TeamMember

# The texts Agno gave the leader for a member that paused, per mode, and for one that said nothing.
MEMBER_PAUSED_RESULT = "Member '{name}' requires human input before continuing."
COLLABORATE_MEMBER_PAUSED_RESULT = "Agent {name}: Requires human input before continuing."
NO_MEMBER_RESPONSE = "No response from the member agent."
DELEGATE_TOOL_NAMES = frozenset({"delegate_task_to_member", "delegate_task_to_members"})


class MemberRunner(Protocol):
    """Run one member on the leader's task and return its terminal record, whatever its status."""

    def __call__(self, member: TeamMember, task: str) -> Coroutine[Any, Any, RunRecord]:
        """Run ``member`` on ``task``."""
        ...


def delegate_functions(
    members: Sequence[TeamMember],
    mode: TeamMode,
    *,
    record: TeamRunRecord,
    run_member: MemberRunner,
) -> AdaptedToolkit:
    """Return the delegation tool for ``mode`` as a toolkit bound to the team run ``record``."""
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
        return member_result_text(child)

    async def delegate_task_to_members(task: str) -> str:
        """Use this function to send one task to every member agent and return their responses.
        One call reaches all members; do not call it once per member.

        Args:
            task (str): A clear and concise description of the task to send to member agents, including what a good result looks like.

        Returns:
            str: The result of the delegated task.

        """  # noqa: D205 - Agno's two-line summary, which its schema generator reads as the tool description.
        async with asyncio.TaskGroup() as group:
            runs = [group.create_task(run_member(member, task)) for member in members]
        return collaborate_result_text(members, [run.result() for run in runs], record)

    entrypoint = delegate_task_to_members if mode is TeamMode.COLLABORATE else delegate_task_to_member
    function = adapt_function(Function.from_callable(entrypoint, name=entrypoint.__name__))
    return AdaptedToolkit(name="team_delegation", functions=(function,), instructions=None)


def member_result_text(child: RunRecord) -> str:
    """Return what the leader reads for a finished member run: its text, else its tool results, else Agno's notice.

    A failed member's record holds the error text as its content, so the leader reads what went wrong, as it did
    when Agno absorbed a member error into the member's run.
    """
    content = child.content
    if isinstance(content, str) and content.strip():
        return content
    if isinstance(content, dict):
        return json.dumps(content, indent=2, ensure_ascii=False)
    if child.tools:
        return ",".join(tool.result for tool in child.tools if tool.result)
    return NO_MEMBER_RESPONSE


def collaborate_result_text(
    members: Sequence[TeamMember],
    children: Sequence[RunRecord],
    record: TeamRunRecord,
) -> str:
    """Return the collaborate result: every member's text in member order, with no separator, as Agno's stream did.

    Agno's streaming broadcast concatenated the members' output as it arrived; member order makes the result
    deterministic and equal to Agno's whenever arrival order matched it. A paused member contributes Agno's notice
    and its requirements reach the team run.
    """
    pieces: list[str] = []
    for member, child in zip(members, children, strict=True):
        if child.status is RunStatus.paused:
            propagate_member_pause(record, member, child)
            pieces.append(COLLABORATE_MEMBER_PAUSED_RESULT.format(name=member.spec.name))
        else:
            pieces.append(member_result_text(child))
    return "".join(pieces)


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
    "DELEGATE_TOOL_NAMES",
    "MEMBER_PAUSED_RESULT",
    "NO_MEMBER_RESPONSE",
    "MemberRunner",
    "collaborate_result_text",
    "delegate_functions",
    "member_result_text",
    "propagate_member_pause",
]
