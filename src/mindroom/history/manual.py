"""Manual history compaction request interface."""

from __future__ import annotations

from dataclasses import replace
from typing import TYPE_CHECKING

from mindroom.history.policy import describe_compaction_unavailability, resolve_history_execution_plan
from mindroom.history.session_context import open_resolved_scope_session_context
from mindroom.history.storage import add_pending_force_compaction_scope, update_scope_state_on_latest
from mindroom.logging_config import get_logger

if TYPE_CHECKING:
    from mindroom.config.main import Config, ResolvedRuntimeModel
    from mindroom.config.models import CompactionConfig
    from mindroom.constants import RuntimePaths
    from mindroom.history.types import HistoryScope
    from mindroom.tool_system.worker_routing import ToolExecutionIdentity


_MANUAL_COMPACTION_SUCCESS_MESSAGE = "Compaction will run before the next reply in this conversation scope."
logger = get_logger(__name__)


def request_compaction_before_next_reply(
    *,
    scope: HistoryScope,
    agent_name: str,
    session_id: str,
    runtime_paths: RuntimePaths,
    config: Config,
    execution_identity: ToolExecutionIdentity | None,
    active_model_name: str | None = None,
    room_id: str | None = None,
    session_state: dict[str, object] | None = None,
) -> str:
    """Schedule text compaction before the next reply in ``scope``, the history scope the running entity stores to.

    Return the message the caller reports. When ``session_state``, the dict the run's tools share, is given, the
    request is also recorded in it, in place.
    """
    with open_resolved_scope_session_context(
        scope=scope,
        agent_name=agent_name,
        session_id=session_id,
        runtime_paths=runtime_paths,
        config=config,
        execution_identity=execution_identity,
        create_session_if_missing=True,
    ) as scope_context:
        if scope_context is None:
            return "Error: Current agent has no history scope. Cannot compact context."

        runtime_model, compaction_config = _resolve_active_compaction_settings(
            scope=scope,
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
            return f"Error: Compaction is unavailable for this scope because {unavailable}."

        session = scope_context.session
        # create_session_if_missing opens a new session when none is stored yet.
        assert session is not None
        update_scope_state_on_latest(
            scope_context.storage,
            session=session,
            scope=scope_context.scope,
            update=lambda latest: replace(latest, force_compact_before_next_run=True),
        )

        if session_state is not None:
            add_pending_force_compaction_scope(session_state, scope_context.scope)
        logger.info(
            "Manual compaction scheduled",
            agent=agent_name,
            scope=scope_context.scope.key,
        )
        return _MANUAL_COMPACTION_SUCCESS_MESSAGE


def _resolve_active_compaction_settings(
    *,
    scope: HistoryScope,
    agent_name: str,
    config: Config,
    runtime_paths: RuntimePaths,
    active_model_name: str | None,
    room_id: str | None,
) -> tuple[ResolvedRuntimeModel, CompactionConfig]:
    """Resolve the active model and compaction config for the current scope."""
    if scope.kind == "agent":
        runtime_model = config.resolve_runtime_model(
            entity_name=agent_name,
            active_model_name=active_model_name,
            room_id=room_id,
            runtime_paths=runtime_paths if room_id is not None else None,
        )
        return runtime_model, config.resolve_entity(agent_name).compaction_config

    team_name = scope.scope_id
    if team_name not in config.teams:
        # An ad-hoc team has no authored settings of its own.
        runtime_model = config.resolve_runtime_model(
            entity_name=None,
            active_model_name=active_model_name,
        )
        return runtime_model, config.resolve_entity(None).compaction_config

    runtime_model = config.resolve_runtime_model(
        entity_name=team_name,
        active_model_name=active_model_name,
        room_id=room_id,
        runtime_paths=runtime_paths,
    )
    return runtime_model, config.resolve_entity(team_name).compaction_config
