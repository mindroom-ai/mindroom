# Adapted from Agno (https://github.com/agno-agi/agno), Copyright 2025-2026 Agno Inc., Apache License 2.0; modified by MindRoom.
"""Assemble model requests from a scope's stored runs, its compaction summary, and the current turn.

Selection follows what Agno's ``get_messages`` and ``filter_tool_calls`` returned for MindRoom's replay limits, and the
placement and media rules are the ones MindRoom applied on top of Agno's message builders.
"""

from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass, replace
from itertools import dropwhile, islice
from typing import TYPE_CHECKING

from mindroom.constants import prompt_roles_for_history_storage
from mindroom.history.legacy_summary_system_prompt import system_message_embeds_summary
from mindroom.history.message_content import (
    content_with_viewed_image_omission,
    is_viewed_image,
    retained_viewed_images,
)
from mindroom.history.summary_message import compaction_summary_fields, is_scope_summary, summary_follows
from mindroom.llm.messages import Message
from mindroom.llm.runs import RunRecord, RunStatus, TeamRunRecord

if TYPE_CHECKING:
    from collections.abc import Sequence

    from mindroom.history.types import HistoryScope, ResolvedHistorySettings, ScopeSummary
    from mindroom.llm.runs import AnyRunRecord

# A paused, stopped, or failed run is not part of the conversation the model sees.
_HIDDEN_RUN_STATUSES = frozenset({RunStatus.paused, RunStatus.cancelled, RunStatus.error})
_PROMPT_ROLES = frozenset({"system", "developer"})


@dataclass(frozen=True, kw_only=True)
class TurnMessages:
    """A new turn's input around its prompt.

    ``context`` holds the Matrix messages the scope has not stored yet, sent before the prompt and stored with the run.
    ``transient`` holds turn context that is sent but never stored (``add_to_agent_memory=False``). It follows the
    prompt because the next turn replays the prompt where it sat, so this request stays a prefix of the next one and
    provider prompt caches, or routing by a conversation's opening messages, still match.
    """

    prompt: Message
    context: tuple[Message, ...] = ()
    transient: tuple[Message, ...] = ()


@dataclass(frozen=True, kw_only=True)
class RequestContext:
    """What a pre-request hook, such as mid-turn compaction, knows about the request it may rewrite in place."""

    # The running run, whose tool executions, member runs, and metadata the loop stores when the run ends.
    run: AnyRunRecord
    # The scope whose stored history the request replays, or None when it replays none and compaction stays in the run.
    scope: HistoryScope | None
    prompt_id: str
    # Ids of the messages the response loop started with, which therefore predate every response it received.
    loop_start_ids: frozenset[str]


def scope_history_messages(
    runs: Sequence[AnyRunRecord],
    settings: ResolvedHistorySettings,
    *,
    scope: HistoryScope,
    num_history_runs: int | None,
    num_history_messages: int | None,
) -> list[Message]:
    """Return the stored history one request of ``scope`` replays, oldest first.

    ``runs`` are the session's runs in stored order. The two limits are the request's own, which a replay plan may
    narrow from ``settings.policy``; ``settings`` supplies the tool-call limit and the configured system role.
    Each message is a copy marked ``from_history``, so a request never changes a stored run and is never stored again.
    Tool calls beyond ``max_tool_calls_from_history`` are left out, and so is every media item except the newest
    viewed images.
    """
    history = [
        _history_copy(message)
        for message in _selected_messages(
            runs,
            scope,
            num_history_runs=num_history_runs,
            num_history_messages=num_history_messages,
            prompt_roles=prompt_roles_for_history_storage(settings.system_message_role),
        )
    ]
    if settings.max_tool_calls_from_history is not None:
        history = filter_tool_calls(history, settings.max_tool_calls_from_history)
    return _without_history_media(history)


def _selected_messages(
    runs: Sequence[AnyRunRecord],
    scope: HistoryScope,
    *,
    num_history_runs: int | None,
    num_history_messages: int | None,
    prompt_roles: frozenset[str],
) -> list[Message]:
    """Select the scope's stored messages within the replay limits.

    The run limit applies before the message limit. A tool result whose call the message limit cut off is dropped,
    since it cannot replay without that call. Prompt roles never replay, nor do history copies a stored run still holds.
    """
    replayed = [run for run in runs if _replays_in_scope(run, scope)]
    if num_history_runs is not None:
        if num_history_runs <= 0:
            return []
        replayed = replayed[-num_history_runs:]
    messages = [
        message
        for run in replayed
        for message in run.messages
        if not message.from_history and message.role not in prompt_roles
    ]
    if num_history_messages is None:
        return messages
    newest = messages[-num_history_messages:] if num_history_messages > 0 else []
    return list(dropwhile(lambda message: message.role == "tool", newest))


def _replays_in_scope(run: AnyRunRecord, scope: HistoryScope) -> bool:
    """Return whether a run is a finished top-level run of the scope: a team run of its team, an agent run of its agent."""
    if run.parent_run_id is not None or run.status in _HIDDEN_RUN_STATUSES:
        return False
    if scope.kind == "team":
        return isinstance(run, TeamRunRecord) and run.team_id == scope.scope_id
    return isinstance(run, RunRecord) and run.agent_id == scope.scope_id


def _history_copy(message: Message) -> Message:
    """Return a copy of a stored message marked as replayed history.

    Stored runs are shared by every reader of a session, so the copy shares nothing a request edits in place:
    block-list content is copied deeply because provider formatting may edit those blocks, and every other field is
    only ever replaced.
    """
    if isinstance(message.content, list):
        copied = deepcopy(message)
        copied.from_history = True
        return copied
    return replace(message, from_history=True)


def filter_tool_calls(messages: Sequence[Message], max_tool_calls: int) -> list[Message]:
    """Return ``messages`` keeping only the newest ``max_tool_calls`` tool results and the calls they answer.

    Only results that carry a call id count toward the window. An assistant message keeps only its calls in the window,
    as a copy, and is dropped when it has neither a call nor content left. Messages holding at most ``max_tool_calls``
    results are returned unchanged.
    """
    results = [message for message in messages if message.role == "tool"]
    if len(results) <= max_tool_calls:
        return list(messages)
    kept_ids = set(islice((result.tool_call_id for result in reversed(results) if result.tool_call_id), max_tool_calls))
    filtered: list[Message] = []
    for message in messages:
        if message.role == "tool":
            if message.tool_call_id in kept_ids:
                filtered.append(message)
        elif message.role == "assistant" and message.tool_calls:
            kept = deepcopy(message)
            kept.tool_calls = [call for call in kept.tool_calls or [] if call.get("id") in kept_ids] or None
            if kept.tool_calls or kept.content:
                filtered.append(kept)
        else:
            filtered.append(message)
    return filtered


def _without_history_media(history: Sequence[Message]) -> list[Message]:
    """Drop the media of replayed history except the newest viewed images, noting the viewed images left out.

    Attachments replay as the text that names them; only images ``view_file`` attached replay, within a count and a
    byte cap.
    """
    retained = retained_viewed_images(history)
    projected: list[Message] = []
    for message_index, message in enumerate(history):
        if not (message.images or message.audio or message.videos or message.files):
            projected.append(message)
            continue
        images = message.images or ()
        kept = [image for image_index, image in enumerate(images) if (message_index, image_index) in retained]
        omitted = sum(is_viewed_image(image) for image in images) - len(kept)
        copy = replace(message, images=kept or None, audio=None, videos=None, files=None)
        if omitted:
            copy = replace(
                copy,
                content=content_with_viewed_image_omission(message, omitted),
                compressed_content=None,
            )
        projected.append(copy)
    return projected


def build_request(
    *,
    system: Sequence[Message],
    summary: ScopeSummary | None,
    history: Sequence[Message],
    turn: TurnMessages,
) -> list[Message]:
    """Return a new run's request: the system messages, the scope summary, the replayed history, then the turn.

    Pass ``summary=None`` when the run replays no stored history; a run that replays the summary but no runs passes
    it with empty ``history``.
    """
    return _with_scope_summary([*system, *history, *turn.context, turn.prompt, *turn.transient], summary)


def build_resume_request(
    *,
    paused: Sequence[Message],
    summary: ScopeSummary | None,
    history: Sequence[Message],
) -> list[Message]:
    """Return the request that resumes a paused run: its stored system message, the summary, history, then the rest.

    ``paused`` holds the paused run's stored messages, which never include history copies. Its stored system message is
    reused, so a signed reasoning block in the paused request stays valid against an unchanged prefix. A run paused
    while Agno rendered the summary inside that system message keeps that single summary.
    """
    system = next((message for message in paused if message.role == "system"), None)
    messages = [
        *([] if system is None else [system]),
        *history,
        *(message for message in paused if message is not system),
    ]
    if system_message_embeds_summary(messages):
        return messages
    return _with_scope_summary(messages, summary)


def _with_scope_summary(messages: list[Message], summary: ScopeSummary | None) -> list[Message]:
    """Insert the scope summary once, as the first message after the prompt messages.

    When that first message is a prompt a mid-turn compaction placed the summary after, the summary stays after it; a
    turn that replays ahead of that run, such as an approval completed after it, was built on the summary instead.
    """
    text = summary.text.strip() if summary is not None else ""
    if not text or any(is_scope_summary(message) for message in messages):
        return messages
    index = next((index for index, message in enumerate(messages) if message.role not in _PROMPT_ROLES), len(messages))
    if index < len(messages) and summary_follows(messages[index]):
        index += 1
    return [*messages[:index], Message(**compaction_summary_fields(text, run_local=False)), *messages[index:]]


__all__ = [
    "RequestContext",
    "TurnMessages",
    "build_request",
    "build_resume_request",
    "filter_tool_calls",
    "scope_history_messages",
]
