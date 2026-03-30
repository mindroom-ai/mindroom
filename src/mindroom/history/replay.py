"""Persisted replay selection and deterministic budget trimming."""

from __future__ import annotations

import hashlib
from copy import deepcopy
from dataclasses import replace
from typing import TYPE_CHECKING, cast

from agno.run.agent import RunOutput
from agno.run.base import RunStatus
from agno.run.team import TeamRunOutput
from agno.utils.message import filter_tool_calls
from pydantic import BaseModel

from mindroom.history.types import CompactionState, HistoryPolicy, HistoryScope, ReplayPlan
from mindroom.logging_config import get_logger
from mindroom.token_budget import estimate_text_tokens, stable_serialize

if TYPE_CHECKING:
    from agno.agent import Agent
    from agno.models.message import Message
    from agno.session.agent import AgentSession
    from agno.session.team import TeamSession

logger = get_logger(__name__)

_REPLAY_MESSAGE_MARKER = "mindroom_history_replay"
_SUMMARY_HEADER = "<history_context>\n<summary>\n{summary}\n</summary>\n</history_context>\n\n"


def resolve_history_scope(agent: Agent) -> HistoryScope | None:
    """Return the replay scope addressed by one live agent instance."""
    team_id = agent.team_id
    if isinstance(team_id, str) and team_id:
        return HistoryScope(kind="team", scope_id=team_id)
    agent_id = agent.id
    if isinstance(agent_id, str) and agent_id:
        return HistoryScope(kind="agent", scope_id=agent_id)
    return None


def build_replay_plan(
    *,
    session: AgentSession | TeamSession,
    scope: HistoryScope,
    state: CompactionState,
    policy: HistoryPolicy,
    max_tool_calls_from_history: int | None,
) -> ReplayPlan:
    """Build the persisted replay plan for one session scope."""
    scoped_runs = _runs_for_scope(_completed_top_level_runs(session), scope)
    visible_runs, effective_state = _apply_cutoff(scoped_runs, state, session_id=session.session_id, scope=scope)
    summary_prompt_prefix = render_summary_prompt(effective_state.summary)
    history_message_groups = _message_groups_for_policy(
        visible_runs=visible_runs,
        policy=policy,
    )
    history_messages = finalize_history_message_groups(
        history_message_groups,
        max_tool_calls_from_history=max_tool_calls_from_history,
    )
    replay_tokens = estimate_text_tokens(summary_prompt_prefix) + estimate_history_messages_tokens(history_messages)
    return ReplayPlan(
        scope=scope,
        state=effective_state,
        visible_runs=visible_runs,
        summary_prompt_prefix=summary_prompt_prefix,
        history_message_groups=history_message_groups,
        history_messages=history_messages,
        replay_tokens=replay_tokens,
        has_stored_replay_state=bool(scoped_runs) or effective_state.has_summary,
    )


def apply_oldest_first_drop_policy(
    plan: ReplayPlan,
    *,
    budget_tokens: int | None,
    max_tool_calls_from_history: int | None,
) -> ReplayPlan:
    """Apply the deterministic fallback chain when replay exceeds the budget."""
    if budget_tokens is None:
        return plan
    if budget_tokens <= 0:
        return _empty_replay_plan(plan)

    _working_groups, history_messages, replay_tokens = _trim_history_groups_to_budget(
        groups=plan.history_message_groups,
        summary_prompt_prefix=plan.summary_prompt_prefix,
        budget_tokens=budget_tokens,
        max_tool_calls_from_history=max_tool_calls_from_history,
    )
    if replay_tokens <= budget_tokens:
        return replace(plan, history_messages=history_messages, replay_tokens=replay_tokens)

    return _drop_summary_if_needed(plan, budget_tokens)


def render_summary_prompt(summary: str | None) -> str:
    """Render the deterministic persisted-summary prefix."""
    if summary is None or summary.strip() == "":
        return ""
    return _SUMMARY_HEADER.format(summary=summary.strip())


def finalize_history_message_groups(
    groups: list[list[Message]],
    *,
    max_tool_calls_from_history: int | None,
) -> list[Message]:
    """Flatten, copy, filter, and mark replay messages for one run."""
    flattened = [deepcopy(message) for group in groups for message in group]
    if not flattened:
        return []
    if max_tool_calls_from_history is not None:
        filter_tool_calls(flattened, max_tool_calls_from_history)
    return [_mark_replay_message(message) for message in flattened]


def estimate_history_messages_tokens(messages: list[Message]) -> int:
    """Estimate the token count of already-materialized replay messages."""
    if not messages:
        return 0
    total_chars = 0
    for message in messages:
        total_chars += len(_render_message_content(message))
        if message.tool_calls:
            total_chars += len(stable_serialize(message.tool_calls))
        total_chars += _estimate_message_media_chars(message)
    return total_chars // 4


def digest_prepared_replay(summary_prompt_prefix: str, history_messages: list[Message]) -> str | None:
    """Return a stable digest of the replay payload for cache-keying."""
    if not summary_prompt_prefix and not history_messages:
        return None
    payload = {
        "summary_prompt_prefix": summary_prompt_prefix,
        "history_messages": [message.to_dict() for message in history_messages],
    }
    return hashlib.sha256(stable_serialize(payload).encode("utf-8")).hexdigest()


def is_replay_message(message: Message) -> bool:
    """Return whether one message was injected as persisted raw replay."""
    extra = message.model_extra
    return isinstance(extra, dict) and extra.get(_REPLAY_MESSAGE_MARKER) is True


def strip_replay_messages(messages: list[Message] | None) -> list[Message] | None:
    """Remove injected replay messages from an arbitrary message list."""
    if messages is None:
        return None
    filtered = [message for message in messages if not is_replay_message(message)]
    return filtered or None


def _mark_replay_message(message: Message) -> Message:
    message.from_history = True
    message.add_to_agent_memory = False
    message.temporary = True
    if message.model_extra is None:
        object.__setattr__(message, "__pydantic_extra__", {})
    extra = cast("dict[str, object]", message.model_extra)
    extra[_REPLAY_MESSAGE_MARKER] = True
    return message


def _completed_top_level_runs(session: AgentSession | TeamSession) -> list[RunOutput | TeamRunOutput]:
    skip_statuses = {RunStatus.paused, RunStatus.cancelled, RunStatus.error}
    return [
        run
        for run in session.runs or []
        if isinstance(run, (RunOutput, TeamRunOutput)) and run.parent_run_id is None and run.status not in skip_statuses
    ]


def _runs_for_scope(
    runs: list[RunOutput | TeamRunOutput],
    scope: HistoryScope,
) -> list[RunOutput | TeamRunOutput]:
    if scope.kind == "team":
        return [run for run in runs if isinstance(run, TeamRunOutput) and run.team_id == scope.scope_id]
    return [run for run in runs if isinstance(run, RunOutput) and run.agent_id == scope.scope_id]


def _apply_cutoff(
    runs: list[RunOutput | TeamRunOutput],
    state: CompactionState,
    *,
    session_id: str,
    scope: HistoryScope,
) -> tuple[list[RunOutput | TeamRunOutput], CompactionState]:
    if not state.has_summary or not state.has_cutoff:
        return runs, CompactionState(force_compact_before_next_run=state.force_compact_before_next_run)
    cutoff_index = next((index for index, run in enumerate(runs) if run.run_id == state.last_compacted_run_id), None)
    if cutoff_index is None:
        logger.warning(
            "Ignoring scoped compaction state with missing cutoff run",
            session_id=session_id,
            scope=scope.key,
            last_compacted_run_id=state.last_compacted_run_id,
        )
        return runs, CompactionState(force_compact_before_next_run=state.force_compact_before_next_run)
    return runs[cutoff_index + 1 :], state


def _message_groups_for_policy(
    *,
    visible_runs: list[RunOutput | TeamRunOutput],
    policy: HistoryPolicy,
) -> list[list[Message]]:
    if not visible_runs:
        return []
    run_groups = [_replayable_messages_for_run(run) for run in visible_runs]
    run_groups = [group for group in run_groups if group]
    if not run_groups:
        return []
    if policy.mode == "runs":
        limit = policy.limit
        return [] if limit is None or limit <= 0 else run_groups[-limit:]
    if policy.mode == "messages":
        limit = policy.limit
        return [] if limit is None or limit <= 0 else _apply_message_limit_to_groups(run_groups, limit)
    return run_groups


def _drop_summary_if_needed(plan: ReplayPlan, budget_tokens: int) -> ReplayPlan:
    summary_prompt_prefix = plan.summary_prompt_prefix
    if summary_prompt_prefix:
        summary_tokens = estimate_text_tokens(summary_prompt_prefix)
        if summary_tokens <= budget_tokens:
            return replace(
                plan,
                history_message_groups=[],
                history_messages=[],
                replay_tokens=summary_tokens,
            )
        logger.warning(
            "History summary exceeds replay budget; dropping all persisted replay",
            scope=plan.scope.key,
            budget_tokens=budget_tokens,
            summary_tokens=summary_tokens,
        )
    return _empty_replay_plan(plan)


def _empty_replay_plan(plan: ReplayPlan) -> ReplayPlan:
    return replace(
        plan,
        summary_prompt_prefix="",
        history_message_groups=[],
        history_messages=[],
        replay_tokens=0,
    )


def _trim_history_groups_to_budget(
    *,
    groups: list[list[Message]],
    summary_prompt_prefix: str,
    budget_tokens: int,
    max_tool_calls_from_history: int | None,
) -> tuple[list[list[Message]], list[Message], int]:
    working_groups = [list(group) for group in groups]
    history_messages, replay_tokens = _build_budgeted_history_messages(
        working_groups,
        summary_prompt_prefix=summary_prompt_prefix,
        max_tool_calls_from_history=max_tool_calls_from_history,
    )
    while len(working_groups) > 1 and replay_tokens > budget_tokens:
        working_groups.pop(0)
        history_messages, replay_tokens = _build_budgeted_history_messages(
            working_groups,
            summary_prompt_prefix=summary_prompt_prefix,
            max_tool_calls_from_history=max_tool_calls_from_history,
        )

    while working_groups and replay_tokens > budget_tokens:
        if not working_groups[0]:
            working_groups.pop(0)
        else:
            working_groups[0].pop(0)
            _drop_leading_tool_messages(working_groups)
        history_messages, replay_tokens = _build_budgeted_history_messages(
            working_groups,
            summary_prompt_prefix=summary_prompt_prefix,
            max_tool_calls_from_history=max_tool_calls_from_history,
        )
    return working_groups, history_messages, replay_tokens


def _build_budgeted_history_messages(
    groups: list[list[Message]],
    *,
    summary_prompt_prefix: str,
    max_tool_calls_from_history: int | None,
) -> tuple[list[Message], int]:
    history_messages = finalize_history_message_groups(
        groups,
        max_tool_calls_from_history=max_tool_calls_from_history,
    )
    replay_tokens = estimate_text_tokens(summary_prompt_prefix) + estimate_history_messages_tokens(history_messages)
    return history_messages, replay_tokens


def _replayable_messages_for_run(run: RunOutput | TeamRunOutput) -> list[Message]:
    messages: list[Message] = []
    for message in run.messages or []:
        if message.from_history:
            continue
        if message.role == "system":
            continue
        messages.append(message)
    return messages


def _apply_message_limit_to_groups(groups: list[list[Message]], message_limit: int) -> list[list[Message]]:
    indexed_messages = [(group_index, message) for group_index, group in enumerate(groups) for message in group]
    limited = indexed_messages[-message_limit:]
    while limited and limited[0][1].role == "tool":
        limited.pop(0)
    rebuilt: dict[int, list[Message]] = {}
    for group_index, message in limited:
        rebuilt.setdefault(group_index, []).append(message)
    return [rebuilt[group_index] for group_index in sorted(rebuilt)]


def _drop_leading_tool_messages(groups: list[list[Message]]) -> None:
    while groups and groups[0] and groups[0][0].role == "tool":
        groups[0].pop(0)
    while groups and not groups[0]:
        groups.pop(0)


def _estimate_message_media_chars(message: Message) -> int:
    media_chars = 0
    for media_value in (
        message.images,
        message.audio,
        message.videos,
        message.files,
        message.audio_output,
        message.image_output,
        message.video_output,
        message.file_output,
    ):
        if media_value is None:
            continue
        media_chars += len(stable_serialize(_media_payload_snapshot(media_value)))
    return media_chars


def _media_payload_snapshot(media_value: object) -> object:
    if isinstance(media_value, list):
        return [_media_payload_snapshot(item) for item in media_value]
    if isinstance(media_value, BaseModel):
        payload = cast("dict[str, object]", media_value.model_dump(exclude_none=True))
        payload.pop("content", None)
        return payload
    return media_value


def _render_message_content(message: Message) -> str:
    content = message.compressed_content if message.compressed_content is not None else message.content
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "\n".join(stable_serialize(part) for part in content)
    if content is None:
        return ""
    return stable_serialize(content)
