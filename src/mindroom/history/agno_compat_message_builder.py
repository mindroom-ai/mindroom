"""Vendored Agno roleful-input and historical-media patch.

Agno Agent preserves ``list[Message]`` input as roleful provider messages, while
Agno Team currently flattens that same shape through ``get_text_from_message``.
This throwaway monkey-patch mirrors the Agent message-builder path until Agno
Team has the same upstream behavior.
Both builders, and the entry points that resume paused runs, also remove
ordinary inline payloads from persisted history while retaining marked, bounded
tool images for replay, and place the scope's compaction summary before replayed
history.
"""

from __future__ import annotations

import threading
from collections.abc import Awaitable, Callable
from typing import TYPE_CHECKING, Any, cast

from agno.agent import _messages as agent_messages
from agno.models.message import Message
from agno.run.messages import RunMessages
from agno.session.agent import AgentSession
from agno.session.team import TeamSession
from agno.team import _messages as team_messages
from agno.team import _run as team_run
from agno.utils.log import log_warning

from mindroom.history.legacy_summary_system_prompt import system_message_embeds_summary
from mindroom.history.message_content import project_history_media_for_replay
from mindroom.history.replay import compaction_summary_message, current_summary_text, is_compaction_summary

if TYPE_CHECKING:
    from agno.agent import Agent
    from agno.team import Team

# AGNO_COMPAT: Team input loses message roles.
# Reason: Team flattens roleful Message input into a single user message.
# Upstream issue: https://github.com/agno-agi/agno/issues/9942
# Upstream PR: https://github.com/agno-agi/agno/pull/9943
# Remove when: The pinned Agno release preserves roles, user_message, and extra_messages
# for sync and async Team input. That fix alone does not replace historical-media filtering.
# Coverage: tests/test_agno_compat_message_builder.py::test_team_list_message_patch_preserves_roleful_input_through_formatter;
# tests/test_agno_compat_message_builder.py::test_team_list_message_patch_preserves_additional_input_separately.

# AGNO_COMPAT: Historical-media filtering requires private message builders.
# Reason: MindRoom omits ordinary persisted inline media while retaining a newest-first,
# aggregate-bounded set of viewed tool images and disclosing replay omissions.
# Approval continuations rebuild history through separate private entry points; wrapping
# them after Agno re-reads offloaded media, as for new runs, replays the same history.
# Upstream issue: No matching issue identified; this is an application replay policy
# that currently requires wrapping Agno's private Agent/Team message builders.
# Upstream PR: None identified; the roleful-input PR above does not cover this behavior.
# Remove when: A supported message-preparation hook can apply the same history filter
# to new and continued runs; retain the filtering policy when removing private builder interception.
# Coverage: tests/test_agno_compat_message_builder.py::test_persisted_history_media_is_not_replayed;
# tests/test_agno_compat_message_builder.py::test_agent_continuation_does_not_replay_persisted_history_media;
# tests/test_agno_compat_message_builder.py::test_team_continuation_does_not_replay_persisted_history_media;
# tests/test_agno_compat_message_builder.py::test_resumed_approval_run_replays_the_history_the_paused_request_saw;
# tests/test_agno_compat_message_builder.py::test_resumed_approval_run_filters_offloaded_history_after_reading_it_back;
# tests/test_agno_compat_message_builder.py::test_inline_media_cleanup_strips_every_kind_only_from_history;
# tests/test_agno_compat_message_builder.py::test_viewed_image_replay_keeps_only_newest_four_and_discloses_omissions;
# tests/test_agno_compat_message_builder.py::test_history_viewed_image_projection_enforces_aggregate_byte_limit.

# AGNO_COMPAT: Agno renders the session summary only inside its system message.
# Reason: Agno appends session.summary to the system prompt when add_session_summary_to_context is on, so every
# compaction rewrites the cached prompt prefix, and a string system_message (minimal agents) never shows it.
# MindRoom turns that flag off and places the summary as the first history message in every new and continued
# request instead.
# Upstream issue: Tracking gap; no issue identified for a history-positioned session summary.
# Upstream PR: None identified.
# Remove when: Agno can replay the session summary as a history message for new and continued runs; the summary
# must still replay exactly once and be counted once.
# Coverage: tests/test_history_summary_message.py.

_PATCHED = False
_PATCH_LOCK = threading.Lock()
type _RolefulInput = list[Message]
type _RunMessagesBuilder = Callable[..., RunMessages]
type _AsyncRunMessagesBuilder = Callable[..., Awaitable[RunMessages]]


def _is_roleful_message_list(input_message: object) -> bool:
    return isinstance(input_message, list) and bool(input_message) and isinstance(input_message[0], Message)


def _append_input_messages(run_messages: RunMessages, input_messages: list[Any]) -> None:
    roleful_messages: list[Message] = []
    for input_message in input_messages:
        if isinstance(input_message, Message):
            message = input_message
        else:
            try:
                message = Message.model_validate(input_message)
            except Exception as exc:
                log_warning(f"Failed to validate message: {exc}")
                continue
        roleful_messages.append(message)
    if not roleful_messages:
        return

    additional_input = list(run_messages.extra_messages or [])
    run_messages.messages.extend(roleful_messages)
    if roleful_messages[-1].role == "user":
        run_messages.user_message = roleful_messages[-1]
        roleful_history = roleful_messages[:-1]
    else:
        roleful_history = roleful_messages
    run_messages.extra_messages = [*roleful_history, *additional_input]


def _strip_history_inline_media(run_messages: RunMessages) -> RunMessages:
    """Strip historical media except bounded viewed images marked for replay."""
    history_indices = [index for index, message in enumerate(run_messages.messages) if message.from_history]
    projected = project_history_media_for_replay([run_messages.messages[index] for index in history_indices])
    for index, message in zip(history_indices, projected, strict=True):
        run_messages.messages[index] = message
    return run_messages


def _insert_session_summary(run_messages: RunMessages, session: AgentSession | TeamSession) -> None:
    """Place the scope's summary directly after the leading prompt messages, once."""
    summary = current_summary_text(session)
    if summary is None or any(is_compaction_summary(m) and m.from_history for m in run_messages.messages):
        return
    index = next(
        (i for i, message in enumerate(run_messages.messages) if message.role not in {"system", "developer"}),
        len(run_messages.messages),
    )
    run_messages.messages.insert(index, compaction_summary_message(summary, from_history=True))


def _prepare_history(
    run_messages: RunMessages,
    target: object,
    kwargs: dict[str, object],
    *,
    continuation: bool,
) -> RunMessages:
    """Apply MindRoom's replay policy to one built request."""
    session = kwargs.get("session")
    if isinstance(session, (AgentSession, TeamSession)):
        replays_history = kwargs.get("add_history_to_context")
        if continuation and replays_history is None:
            replays_history = cast("Agent | Team", target).add_history_to_context
        if replays_history and not (continuation and system_message_embeds_summary(run_messages.messages)):
            _insert_session_summary(run_messages, session)
    return _strip_history_inline_media(run_messages)


def _with_history_policy(builder: _RunMessagesBuilder, *, continuation: bool) -> _RunMessagesBuilder:
    """Wrap one synchronous run-message builder with MindRoom's replay policy."""

    def build(target: object, *args: object, **kwargs: object) -> RunMessages:
        return _prepare_history(builder(target, *args, **kwargs), target, kwargs, continuation=continuation)

    return build


def _with_history_policy_async(builder: _AsyncRunMessagesBuilder, *, continuation: bool) -> _AsyncRunMessagesBuilder:
    """Wrap one asynchronous run-message builder with MindRoom's replay policy."""

    async def build(target: object, *args: object, **kwargs: object) -> RunMessages:
        return _prepare_history(await builder(target, *args, **kwargs), target, kwargs, continuation=continuation)

    return build


def apply_patch() -> None:
    """Patch Agno Agent and Team run-message builders once per interpreter."""
    global _PATCHED
    if _PATCHED:
        return
    with _PATCH_LOCK:
        if _PATCHED:
            return

        original_team_get_run_messages = cast("_RunMessagesBuilder", team_messages._get_run_messages)
        original_team_aget_run_messages = cast("_AsyncRunMessagesBuilder", team_messages._aget_run_messages)

        def _get_run_messages(team: object, *args: object, **kwargs: object) -> RunMessages:
            input_message = kwargs.get("input_message")
            if not _is_roleful_message_list(input_message):
                run_messages = original_team_get_run_messages(team, *args, **kwargs)
                return _prepare_history(run_messages, team, kwargs, continuation=False)

            passthrough_kwargs = {**kwargs, "input_message": None}
            run_messages = original_team_get_run_messages(team, *args, **passthrough_kwargs)
            _append_input_messages(run_messages, cast("_RolefulInput", input_message))
            return _prepare_history(run_messages, team, kwargs, continuation=False)

        async def _aget_run_messages(team: object, *args: object, **kwargs: object) -> RunMessages:
            input_message = kwargs.get("input_message")
            if not _is_roleful_message_list(input_message):
                run_messages = await original_team_aget_run_messages(team, *args, **kwargs)
                return _prepare_history(run_messages, team, kwargs, continuation=False)

            passthrough_kwargs = {**kwargs, "input_message": None}
            run_messages = await original_team_aget_run_messages(team, *args, **passthrough_kwargs)
            _append_input_messages(run_messages, cast("_RolefulInput", input_message))
            return _prepare_history(run_messages, team, kwargs, continuation=False)

        team_messages._get_run_messages = cast("Any", _get_run_messages)
        team_messages._aget_run_messages = cast("Any", _aget_run_messages)
        agent_messages.get_run_messages = cast(
            "Any",
            _with_history_policy(agent_messages.get_run_messages, continuation=False),
        )
        agent_messages.aget_run_messages = cast(
            "Any",
            _with_history_policy_async(agent_messages.aget_run_messages, continuation=False),
        )
        agent_messages.get_continue_run_messages = cast(
            "Any",
            _with_history_policy(agent_messages.get_continue_run_messages, continuation=True),
        )
        agent_messages.aget_continue_run_messages = cast(
            "Any",
            _with_history_policy_async(agent_messages.aget_continue_run_messages, continuation=True),
        )
        team_run._get_continue_run_messages = cast(
            "Any",
            _with_history_policy(team_run._get_continue_run_messages, continuation=True),
        )
        team_run._aget_continue_run_messages = cast(
            "Any",
            _with_history_policy_async(team_run._aget_continue_run_messages, continuation=True),
        )
        _PATCHED = True
