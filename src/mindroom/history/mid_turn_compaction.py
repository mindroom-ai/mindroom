"""Compact a long turn between two of its model requests.

Before every provider request the request is sized. When it would exceed the replay window minus
``reserve_tokens``, everything except the leading system messages, the current prompt, transient messages, and
queued-message notices is folded into one summary, and the turn continues from
``[system, summary, current prompt, ...]``. A request without persisted replay keeps that summary as an ordinary
message of its own run.
"""

from __future__ import annotations

import asyncio
from copy import deepcopy
from dataclasses import dataclass
from functools import partial
from typing import TYPE_CHECKING, cast

from agno.agent import Agent
from agno.db.base import BaseDb, SessionType
from agno.models.base import Model
from agno.models.message import Message
from agno.run.agent import RunOutput
from agno.run.base import RunStatus
from agno.run.team import TeamRunOutput
from agno.session.team import TeamSession
from agno.team import Team

from mindroom.agent_storage import run_session_storage_operation, save_compaction_usage
from mindroom.agno_compat_model_hooks import install_request_preparation
from mindroom.background_tasks import run_blocking_until_complete
from mindroom.constants import QUEUED_MESSAGE_NOTICE_MARKER_KEY
from mindroom.helper_usage import get_helper_usage_owner
from mindroom.history import archive
from mindroom.history.agno_compat_message_builder import built_request_session
from mindroom.history.legacy_summary_system_prompt import without_embedded_summary
from mindroom.history.policy import context_budget_after_reserve
from mindroom.history.replay import (
    HistorySummaryBudgetError,
    compaction_summary_message,
    compaction_summary_text,
    current_summary_text,
    estimate_request_messages_tokens,
    is_compaction_summary,
)
from mindroom.history.runtime import (
    compact_scope_mid_turn,
    resolve_agent_preparation_inputs,
    resolve_entity_preparation_inputs,
    summarize_run_locally,
)
from mindroom.history.session_context import resolve_history_scope
from mindroom.history.storage import new_scope_session, reconcile_compaction_state
from mindroom.history.types import HistoryScope
from mindroom.logging_config import get_logger
from mindroom.model_usage import response_context_tokens
from mindroom.native_compaction import NativeCompactionModel
from mindroom.token_budget import estimate_text_tokens, stable_serialize
from mindroom.usage_storage import COMPACTED_REQUESTS_METADATA_KEY, project_requests

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable, Sequence
    from typing import Any

    from agno.models.response import ModelResponse
    from agno.session.agent import AgentSession

    from mindroom.config.main import Config
    from mindroom.constants import RuntimePaths
    from mindroom.history.runtime import HistoryPreparationInputs
    from mindroom.history.types import CompactionLifecycle

logger = get_logger(__name__)

_HOOK_MARKER = "_mindroom_mid_turn_compaction_installed"
_BINDING_KEY = "_mindroom_mid_turn_compaction"
_PROMPT_ROLES = frozenset({"system", "developer"})


@dataclass
class _MidTurnCompaction:
    """One Agent's or Team's mid-turn compaction policy and the state of its current run."""

    target: Agent | Team
    config: Config
    runtime_paths: RuntimePaths
    entity_name: str | None
    model_name: str
    lifecycle: CompactionLifecycle | None = None
    failed_run_id: str | None = None
    # (run id, run identity) of the loop being prepared, and the message ids it started with.
    loop_key: tuple[str, int] | None = None
    loop_start_ids: frozenset[str] = frozenset()

    def compacts_run(self, run_id: str) -> bool:
        """Return whether mid-turn compaction still runs for this run; a failure disables it until the next run."""
        if self.failed_run_id != run_id:
            self.failed_run_id = None
        return self.failed_run_id is None

    def resolved_inputs(self) -> HistoryPreparationInputs:
        if isinstance(self.target, Agent):
            return resolve_agent_preparation_inputs(
                agent=self.target,
                agent_name=self.entity_name or self.target.id or "",
                full_prompt="",
                config=self.config,
                active_model_name=self.model_name,
                static_prompt_tokens=0,
            )
        return resolve_entity_preparation_inputs(
            config=self.config,
            entity_name=self.entity_name,
            static_prompt_tokens=0,
            active_model_name=self.model_name,
            active_context_window=None,
        )

    def loaded_message_ids(self, run_response: RunOutput | TeamRunOutput, messages: list[Message]) -> frozenset[str]:
        """Return the ids present when this response loop first prepared a request; they predate its responses."""
        key = (run_response.run_id or "", id(run_response))
        if self.loop_key != key:
            self.loop_key = key
            self.loop_start_ids = frozenset(message.id for message in messages if message.id)
        return self.loop_start_ids


@dataclass(frozen=True)
class _Layout:
    """How one request divides into kept and folded messages."""

    leading: int
    kept: tuple[Message, ...]
    folded: tuple[Message, ...]
    prompt: Message | None
    previous_run_local_summary: str | None


def install_mid_turn_compaction(
    target: Agent | Team,
    *,
    config: Config,
    runtime_paths: RuntimePaths,
    entity_name: str | None,
    model_name: str,
) -> None:
    """Compact the target's long turns before any model request that would exceed its replay window."""
    model = target.model
    if not isinstance(model, Model):
        return
    binding = _MidTurnCompaction(
        target=target,
        config=config,
        runtime_paths=runtime_paths,
        entity_name=entity_name,
        model_name=model_name,
    )
    vars(model)[_BINDING_KEY] = binding
    install_request_preparation(model, marker=_HOOK_MARKER, prepare=partial(_prepare_request, binding))


def bind_compaction_lifecycle(target: Agent | Team, lifecycle: CompactionLifecycle | None) -> None:
    """Show this reply's mid-turn compactions through the same notices as its pre-reply compaction."""
    model = target.model
    binding = vars(model).get(_BINDING_KEY) if isinstance(model, Model) else None
    if isinstance(binding, _MidTurnCompaction):
        binding.lifecycle = lifecycle


@dataclass(frozen=True)
class _Policy:
    """The resolved compaction plan and request limit for one model request."""

    inputs: HistoryPreparationInputs
    limit: int
    model: Model
    replay_model: NativeCompactionModel | None
    # Set while the provider compacts natively; the hook then only falls back to text when a request cannot fit.
    native_route: str | None


async def _prepare_request(
    binding: _MidTurnCompaction,
    messages: list[Message],
    tools: list[dict[str, Any]] | None,
    run_response: RunOutput | TeamRunOutput | None,
) -> None:
    if run_response is None or not run_response.run_id or not binding.compacts_run(run_response.run_id):
        return
    loaded_ids = binding.loaded_message_ids(run_response, messages)
    policy = _policy(binding)
    if policy is None or _request_tokens(binding, policy, messages, tools, loaded_ids) <= policy.limit:
        return
    layout = _layout(messages, run_response)
    if not layout.folded:
        return
    if policy.native_route is not None and policy.replay_model is not None:
        # The provider's own compaction could not keep this request within the window.
        logger.warning(
            "Native compaction left the request over its limit; compacting as text",
            run_id=run_response.run_id,
        )
        policy.replay_model.configure_native_compaction(threshold=None)
    session = _scoped_session(binding, run_response)
    try:
        if session is None:
            summary_message = await _summarize_run_locally(binding, policy.inputs, layout, run_response, messages)
        else:
            summary_message = await _compact_scope(binding, policy, layout, messages, run_response, session)
    except asyncio.CancelledError:
        raise
    except Exception:
        summary_message = None
        logger.exception("Mid-turn compaction failed; continuing without it", run_id=run_response.run_id)
    if summary_message is None:
        binding.failed_run_id = run_response.run_id
        return
    _finish(messages, layout, summary_message, run_response)
    _require_fit(policy, messages, tools, summary_message)


def _finish(
    messages: list[Message],
    layout: _Layout,
    summary_message: Message,
    run_response: RunOutput | TeamRunOutput,
) -> None:
    _rewrite(messages, layout, summary_message)
    _carry_request_usage(run_response, layout.folded)


def _scoped_session(
    binding: _MidTurnCompaction,
    run_response: RunOutput | TeamRunOutput,
) -> AgentSession | TeamSession | None:
    """Return the run's own session when the request replays persisted history, else None (run-local)."""
    target = binding.target
    if not target.add_history_to_context or not isinstance(target.db, BaseDb):
        return None
    session = built_request_session(target)
    return session if session is not None and session.session_id == run_response.session_id else None


async def _compact_scope(
    binding: _MidTurnCompaction,
    policy: _Policy,
    layout: _Layout,
    messages: list[Message],
    run_response: RunOutput | TeamRunOutput,
    session: AgentSession | TeamSession,
) -> Message | None:
    """Archive the scope's visible runs and a snapshot of this run behind a new summary, on Agno's own session."""
    storage = binding.target.db
    assert isinstance(storage, BaseDb)
    scope = (
        HistoryScope(kind="team", scope_id=binding.target.id or "")
        if isinstance(binding.target, Team)
        else resolve_history_scope(binding.target)
    )
    assert scope is not None
    await run_blocking_until_complete(partial(_prepare_scope_session, storage, session, scope))
    snapshot = _snapshot(run_response, layout, messages)
    snapshot.run_id = archive.snapshot_run_id(run_response.run_id or "")
    snapshot.metadata = deepcopy(run_response.metadata)
    snapshot.created_at = run_response.created_at
    try:
        await compact_scope_mid_turn(
            storage=storage,
            session=session,
            scope=scope,
            resolved_inputs=policy.inputs,
            snapshot=snapshot,
            before_tokens=estimate_request_messages_tokens(messages, replay_model=policy.replay_model),
            config=binding.config,
            runtime_paths=binding.runtime_paths,
            compaction_lifecycle=binding.lifecycle,
        )
    except asyncio.CancelledError:
        # A snapshot that committed has archived this turn's folded messages; the request must not resend them.
        if await _archived(storage, session, snapshot):
            _finish(messages, layout, _scope_summary_message(session), run_response)
        raise
    if not await _archived(storage, session, snapshot):
        return None
    return _scope_summary_message(session)


def _prepare_scope_session(storage: BaseDb, session: AgentSession | TeamSession, scope: HistoryScope) -> None:
    # A first turn's session row does not exist until Agno writes it when the run ends.
    if storage.get_session(session_id=session.session_id, session_type=_session_type(session)) is None:
        storage.upsert_session(
            new_scope_session(session_id=session.session_id, scope_id=scope.scope_id, is_team=scope.kind == "team"),
        )
    reconcile_compaction_state(storage, session, scope)


def _session_type(session: AgentSession | TeamSession) -> SessionType:
    return SessionType.TEAM if isinstance(session, TeamSession) else SessionType.AGENT


async def _archived(storage: BaseDb, session: AgentSession | TeamSession, snapshot: RunOutput | TeamRunOutput) -> bool:
    run_id = snapshot.run_id or ""
    archived = await run_blocking_until_complete(
        partial(archive.archived_run_ids, storage, session_id=session.session_id, run_ids=[run_id]),
    )
    return run_id in archived


def _scope_summary_message(session: AgentSession | TeamSession) -> Message:
    summary = current_summary_text(session)
    assert summary is not None
    return compaction_summary_message(summary, from_history=True)


def _policy(binding: _MidTurnCompaction) -> _Policy | None:
    """Return the limit text compaction keeps requests under, or None when it cannot run for this request."""
    inputs = binding.resolved_inputs()
    plan = inputs.execution_plan
    if not (plan.authored_compaction_enabled and plan.text_compaction_available) or plan.replay_window_tokens is None:
        return None
    model = cast("Model", binding.target.model)
    replay_model = model if isinstance(model, NativeCompactionModel) else None
    native = replay_model.native_compaction if replay_model is not None else None
    limit = context_budget_after_reserve(plan.replay_window_tokens, plan.reserve_tokens)
    return _Policy(
        inputs=inputs,
        limit=limit,
        model=model,
        replay_model=replay_model,
        native_route=native.route if native is not None else None,
    )


def _require_fit(
    policy: _Policy,
    messages: list[Message],
    tools: list[dict[str, Any]] | None,
    summary_message: Message,
) -> None:
    """Refuse to send a rewritten request whose kept messages and summary still exceed the limit."""
    tokens = estimate_request_messages_tokens(messages, replay_model=policy.replay_model) + _tool_tokens(tools)
    if tokens <= policy.limit:
        return
    summary_tokens = estimate_text_tokens(str(summary_message.content))
    raise HistorySummaryBudgetError(
        summary_tokens=summary_tokens,
        available_tokens=max(0, policy.limit - (tokens - summary_tokens)),
    )


def _request_tokens(
    binding: _MidTurnCompaction,
    policy: _Policy,
    messages: list[Message],
    tools: list[dict[str, Any]] | None,
    loaded_ids: frozenset[str],
) -> int:
    """Size the next request from the latest response this loop received, else by estimate.

    A native response that created a checkpoint is billed for the transcript it replaced, so native routes always
    estimate their projected request.
    """
    if policy.native_route is not None:
        return estimate_request_messages_tokens(
            messages,
            replay_model=policy.replay_model,
            native_route=policy.native_route,
        ) + _tool_tokens(tools)
    anchor = next(
        (
            index
            for index in range(len(messages) - 1, -1, -1)
            if messages[index].role == "assistant" and messages[index].id not in loaded_ids
        ),
        None,
    )
    if anchor is not None:
        configured = binding.config.models.get(binding.model_name)
        reported = response_context_tokens(
            messages[anchor],
            provider=policy.model.get_provider(),
            configured_provider=configured.provider if configured is not None else None,
            model_id=policy.model.id,
        )
        if reported is not None:
            return reported + estimate_request_messages_tokens(messages[anchor:], replay_model=policy.replay_model)
    return estimate_request_messages_tokens(messages, replay_model=policy.replay_model) + _tool_tokens(tools)


def _tool_tokens(tools: list[dict[str, Any]] | None) -> int:
    return estimate_text_tokens(stable_serialize(tools)) if tools else 0


def _layout(messages: list[Message], run_response: RunOutput | TeamRunOutput) -> _Layout:
    leading = next(
        (index for index, message in enumerate(messages) if message.role not in _PROMPT_ROLES),
        len(messages),
    )
    prompt = _current_prompt(messages[leading:], run_response)
    kept: list[Message] = []
    folded: list[Message] = []
    previous: str | None = None
    for message in messages[leading:]:
        if message is prompt or not message.add_to_agent_memory or _is_queued_notice(message):
            kept.append(message)
        elif is_compaction_summary(message) and not message.from_history:
            previous = compaction_summary_text(message)
        else:
            folded.append(message)
    return _Layout(
        leading=leading,
        kept=tuple(kept),
        folded=tuple(folded),
        prompt=prompt,
        previous_run_local_summary=previous,
    )


def _current_prompt(messages: Sequence[Message], run_response: RunOutput | TeamRunOutput) -> Message | None:
    """Return the turn's current user message: the last input message, else Agno's own user message."""
    content = run_response.input.input_content if run_response.input is not None else None
    if isinstance(content, list) and content:
        last = content[-1]
        last_id = last.id if isinstance(last, Message) else last.get("id") if isinstance(last, dict) else None
        match = next((message for message in reversed(messages) if last_id and message.id == last_id), None)
        if match is not None:
            return match
    return next((message for message in messages if not message.from_history and message.role == "user"), None)


def _is_queued_notice(message: Message) -> bool:
    return isinstance(message.provider_data, dict) and bool(message.provider_data.get(QUEUED_MESSAGE_NOTICE_MARKER_KEY))


def _snapshot(
    run_response: RunOutput | TeamRunOutput,
    layout: _Layout,
    messages: Sequence[Message],
) -> RunOutput | TeamRunOutput:
    """Return the run's folded messages, with its prompt for context, as one completed run to summarize."""
    folded_ids = {id(message) for message in layout.folded if not message.from_history}
    run_messages = [
        message.model_copy(deep=True) for message in messages if id(message) in folded_ids or message is layout.prompt
    ]
    if isinstance(run_response, TeamRunOutput):
        return TeamRunOutput(
            run_id=run_response.run_id,
            team_id=run_response.team_id,
            session_id=run_response.session_id,
            status=RunStatus.completed,
            messages=run_messages,
        )
    return RunOutput(
        run_id=run_response.run_id,
        agent_id=run_response.agent_id,
        session_id=run_response.session_id,
        status=RunStatus.completed,
        messages=run_messages,
    )


async def _summarize_run_locally(
    binding: _MidTurnCompaction,
    inputs: HistoryPreparationInputs,
    layout: _Layout,
    run_response: RunOutput | TeamRunOutput,
    messages: Sequence[Message],
) -> Message:
    scope = HistoryScope(
        kind="team" if isinstance(run_response, TeamRunOutput) else "agent",
        scope_id=binding.target.id or "",
    )
    summary = await summarize_run_locally(
        resolved_inputs=inputs,
        previous_summary=layout.previous_run_local_summary,
        snapshot=_snapshot(run_response, layout, messages),
        scope=scope,
        config=binding.config,
        runtime_paths=binding.runtime_paths,
        on_response=_summary_usage_recorder(binding, run_response),
    )
    return compaction_summary_message(summary, from_history=False)


def _summary_usage_recorder(
    binding: _MidTurnCompaction,
    run_response: RunOutput | TeamRunOutput,
) -> Callable[[Model, ModelResponse], Awaitable[None]] | None:
    """Record each summary response under the reply's usage owner, else in the target's own session storage."""
    owner = get_helper_usage_owner()
    storage = binding.target.db if isinstance(binding.target.db, BaseDb) else None
    if owner is None and (storage is None or not run_response.session_id):
        logger.warning("Mid-turn summary usage has no storage owner", run_id=run_response.run_id)
        return None

    async def record(model: Model, response: ModelResponse) -> None:
        if response.response_usage is None:
            return
        save = partial(
            save_compaction_usage,
            requester_id=run_response.user_id,
            model_provider=model.get_provider(),
            model=model.id,
            metrics=response.response_usage.to_dict(),
        )
        if owner is not None:
            await run_session_storage_operation(
                owner.storage_factory,
                partial(save, session_id=owner.session_id, initial_session=owner.initial_session),
            )
            return
        assert storage is not None
        assert run_response.session_id is not None
        # The run's own session row may not exist yet while its first turn is still running.
        initial_session = new_scope_session(
            session_id=run_response.session_id,
            scope_id=binding.target.id or "",
            is_team=isinstance(run_response, TeamRunOutput),
        )
        await run_blocking_until_complete(
            partial(save, storage, session_id=run_response.session_id, initial_session=initial_session),
        )

    return record


def _rewrite(messages: list[Message], layout: _Layout, summary_message: Message) -> None:
    leading = [without_embedded_summary(message) for message in messages[: layout.leading]]
    messages[:] = [*leading, summary_message, *layout.kept]


def _carry_request_usage(run_response: RunOutput | TeamRunOutput, folded: Sequence[Message]) -> None:
    """Keep the usage of folded requests in the run, since their messages leave it."""
    requests = project_requests([message.to_dict() for message in folded if message.role == "assistant"])
    if not requests:
        return
    metadata = dict(run_response.metadata or {})
    carried = metadata.get(COMPACTED_REQUESTS_METADATA_KEY)
    metadata[COMPACTED_REQUESTS_METADATA_KEY] = [*(carried if isinstance(carried, list) else []), *requests]
    run_response.metadata = metadata
