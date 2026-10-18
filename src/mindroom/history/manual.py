"""Manual history compaction request interface."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from mindroom.history.policy import describe_compaction_unavailability, resolve_history_execution_plan
from mindroom.history.session_context import open_resolved_scope_session_context, resolve_history_scope
from mindroom.history.storage import add_pending_force_compaction_scope, read_scope_state, set_force_compaction_state
from mindroom.logging_config import get_logger

if TYPE_CHECKING:
    from agno.agent import Agent

    from mindroom.config.main import Config, ResolvedRuntimeModel
    from mindroom.config.models import CompactionConfig
    from mindroom.constants import RuntimePaths
    from mindroom.tool_system.worker_routing import ToolExecutionIdentity


_MANUAL_COMPACTION_SUCCESS_MESSAGE = "Compaction will run before the next reply in this conversation scope."
logger = get_logger(__name__)


@dataclass(frozen=True)
class _ManualCompactionRequestResult:
    """Result of scheduling compaction for the next reply."""

    message: str
    session_state: dict[str, object] | None = None


def request_compaction_before_next_reply(
    *,
    agent: Agent,
    agent_name: str,
    session_id: str,
    runtime_paths: RuntimePaths,
    config: Config,
    execution_identity: ToolExecutionIdentity | None,
    active_model_name: str | None = None,
    room_id: str | None = None,
    session_state: dict[str, object] | None = None,
    record_pending_scope_in_session_state: bool = False,
) -> _ManualCompactionRequestResult:
    """Schedule text compaction before the next reply in the current history scope."""
    with open_resolved_scope_session_context(
        scope=resolve_history_scope(agent),
        agent_name=agent_name,
        session_id=session_id,
        runtime_paths=runtime_paths,
        config=config,
        execution_identity=execution_identity,
        create_session_if_missing=True,
    ) as scope_context:
        if scope_context is None:
            return _ManualCompactionRequestResult("Error: Current agent has no history scope. Cannot compact context.")
        if scope_context.session is None:
            return _ManualCompactionRequestResult("Error: No stored session available. Cannot compact context.")

        runtime_model, compaction_config = _resolve_active_compaction_settings(
            agent=agent,
            agent_name=agent_name,
            config=config,
            runtime_paths=runtime_paths,
            active_model_name=active_model_name,
            room_id=room_id,
        )
        execution_plan = resolve_history_execution_plan(
            config=config,
            compaction_config=compaction_config,
            has_authored_compaction_config=True,
            active_model_name=runtime_model.model_name,
            active_context_window=runtime_model.context_window,
            static_prompt_tokens=None,
        )
        unavailable = describe_compaction_unavailability(execution_plan)
        if unavailable is not None:
            return _ManualCompactionRequestResult(
                f"Error: Compaction is unavailable for this scope because {unavailable}.",
                session_state=session_state,
            )

        session = scope_context.session
        current_state = read_scope_state(session, scope_context.scope)
        set_force_compaction_state(session, scope_context.scope, current_state, force=True)
        scope_context.storage.upsert_session(session)

        next_session_state = session_state
        if record_pending_scope_in_session_state:
            next_session_state = add_pending_force_compaction_scope(session_state, scope_context.scope)
        logger.info(
            "Manual compaction scheduled",
            agent=agent_name,
            scope=scope_context.scope.key,
        )
        return _ManualCompactionRequestResult(
            _MANUAL_COMPACTION_SUCCESS_MESSAGE,
            session_state=next_session_state,
        )


def _resolve_active_compaction_settings(
    *,
    agent: Agent,
    agent_name: str,
    config: Config,
    runtime_paths: RuntimePaths,
    active_model_name: str | None,
    room_id: str | None,
) -> tuple[ResolvedRuntimeModel, CompactionConfig]:
    """Resolve the active model and compaction config for the current scope."""
    if agent.team_id is None:
        runtime_model = config.resolve_runtime_model(
            entity_name=agent_name,
            active_model_name=active_model_name,
            room_id=room_id,
            runtime_paths=runtime_paths if room_id is not None else None,
        )
        return runtime_model, config.resolve_entity(agent_name).compaction_config

    if agent.team_id not in config.teams:
        runtime_model = config.resolve_runtime_model(
            entity_name=None,
            active_model_name=active_model_name,
        )
        return runtime_model, config.resolve_entity(None).compaction_config

    runtime_model = config.resolve_runtime_model(
        entity_name=agent.team_id,
        active_model_name=active_model_name,
        room_id=room_id,
        runtime_paths=runtime_paths,
    )
    return runtime_model, config.resolve_entity(agent.team_id).compaction_config
