"""Manual history compaction request interface."""

from __future__ import annotations

from functools import partial
from typing import TYPE_CHECKING

from mindroom.history.policy import describe_compaction_unavailability, resolve_history_preparation_inputs
from mindroom.history.session_context import create_scope_session_storage, scope_session_database_path
from mindroom.history.storage import new_scope_session, set_force_compaction
from mindroom.logging_config import get_logger
from mindroom.session_storage.lanes import run_session_storage_operation
from mindroom.turn_model import ModelScope, resolve_turn_model

if TYPE_CHECKING:
    from mindroom.config.main import Config
    from mindroom.constants import RuntimePaths
    from mindroom.history.types import HistoryPreparationInputs, HistoryScope
    from mindroom.tool_system.worker_routing import ToolExecutionIdentity
    from mindroom.turn_model import TurnBudget


logger = get_logger(__name__)


async def scope_has_session(
    *,
    scope: HistoryScope,
    session_id: str,
    runtime_paths: RuntimePaths,
    config: Config,
    execution_identity: ToolExecutionIdentity | None,
) -> bool:
    """Return whether ``scope`` has a stored session ``session_id``, without loading its runs.

    A scope whose database file does not exist has none, and its store is never opened, which would create the file
    and, for a private agent, its owner record.
    """
    database = scope_session_database_path(
        scope=scope,
        config=config,
        runtime_paths=runtime_paths,
        execution_identity=execution_identity,
    )
    if not database.is_file():
        return False
    return await run_session_storage_operation(
        partial(
            create_scope_session_storage,
            scope=scope,
            config=config,
            runtime_paths=runtime_paths,
            execution_identity=execution_identity,
        ),
        lambda storage: storage.has_session(session_id),
    )


async def schedule_compaction_before_next_reply(
    *,
    scope: HistoryScope,
    agent_name: str,
    session_id: str,
    runtime_paths: RuntimePaths,
    config: Config,
    execution_identity: ToolExecutionIdentity | None,
    active_model_name: str | None,
    room_id: str | None,
    thread_id: str | None = None,
    budget: TurnBudget | None = None,
) -> str | None:
    """Schedule text compaction before the next reply in ``scope``; return why it is unavailable, else None.

    ``agent_name`` names the agent that asked (for logging); ``thread_id`` lets the thread's saved model choice
    decide the model when no ``active_model_name`` is given.
    """
    plan = resolve_manual_compaction_plan(scope, config, runtime_paths, room_id, thread_id, budget, active_model_name)
    if isinstance(plan, str):
        return plan

    # The flag merges into the metadata the stored row holds, in the database's lane as the session's other writes
    # run; a new session names that row, which the write creates when the scope has none yet. No run save writes
    # session metadata, so the requesting run's own saves keep the flag.
    await run_session_storage_operation(
        partial(
            create_scope_session_storage,
            scope=scope,
            config=config,
            runtime_paths=runtime_paths,
            execution_identity=execution_identity,
        ),
        partial(set_force_compaction, session=new_scope_session(session_id=session_id, scope=scope), forced=True),
    )
    logger.info(
        "Manual compaction scheduled",
        agent=agent_name,
        scope=scope.key,
    )
    return None


def resolve_manual_compaction_plan(
    scope: HistoryScope,
    config: Config,
    runtime_paths: RuntimePaths,
    room_id: str | None,
    thread_id: str | None,
    budget: TurnBudget | None,
    active_model_name: str | None = None,
) -> tuple[str, HistoryPreparationInputs] | str:
    """Resolve the model and history policy without touching session storage."""
    # An agent scope's id is its agent; an ad-hoc team has no authored settings of its own.
    entity_name = scope.scope_id if scope.kind == "agent" else config.configured_team_name(scope.scope_id)
    model = resolve_turn_model(
        config,
        runtime_paths,
        ModelScope(entity=entity_name, room_id=room_id, thread_id=thread_id, explicit=active_model_name),
        budget,
    )
    inputs = resolve_history_preparation_inputs(
        config=config,
        history_settings=config.resolve_scope(entity_name).history_settings,
        active_model_name=model.model_name,
        active_context_window=model.context_window,
    )
    return describe_compaction_unavailability(inputs.execution_plan) or (model.model_name, inputs)
