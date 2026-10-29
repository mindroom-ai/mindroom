"""Manual history compaction request interface."""

from __future__ import annotations

from functools import partial
from typing import TYPE_CHECKING

from mindroom.history.policy import describe_compaction_unavailability, resolve_history_execution_plan
from mindroom.history.session_context import create_scope_session_storage
from mindroom.history.storage import new_scope_session, set_force_compaction
from mindroom.logging_config import get_logger
from mindroom.session_storage.lanes import run_session_storage_operation
from mindroom.turn_model import ModelScope, resolve_turn_model

if TYPE_CHECKING:
    from mindroom.config.main import Config
    from mindroom.constants import RuntimePaths
    from mindroom.history.types import HistoryScope
    from mindroom.tool_system.worker_routing import ToolExecutionIdentity


logger = get_logger(__name__)


async def request_compaction_before_next_reply(
    *,
    scope: HistoryScope,
    agent_name: str,
    session_id: str,
    runtime_paths: RuntimePaths,
    config: Config,
    execution_identity: ToolExecutionIdentity | None,
    active_model_name: str | None,
    room_id: str | None,
) -> str:
    """Schedule text compaction before the next reply in ``scope``, the history scope the running entity stores to.

    ``agent_name`` names the agent that asked, a team member when ``scope`` is its team's; only the log reads it.
    Return the message the caller reports.
    """
    # An agent scope's id is its agent; an ad-hoc team has no authored settings of its own.
    entity_name = scope.scope_id if scope.kind == "agent" else config.configured_team_name(scope.scope_id)
    runtime_model = resolve_turn_model(
        config,
        runtime_paths,
        ModelScope(entity=entity_name, room_id=room_id, explicit=active_model_name),
        None,
    )
    execution_plan = resolve_history_execution_plan(
        config=config,
        history_settings=config.resolve_scope(entity_name).history_settings,
        active_model_name=runtime_model.model_name,
        active_context_window=runtime_model.context_window,
        static_prompt_tokens=None,
    )
    unavailable = describe_compaction_unavailability(execution_plan)
    if unavailable is not None:
        return f"Error: Compaction is unavailable for this scope because {unavailable}."

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
        partial(
            set_force_compaction,
            session=new_scope_session(session_id=session_id, scope=scope),
            scope=scope,
            forced=True,
        ),
    )
    logger.info(
        "Manual compaction scheduled",
        agent=agent_name,
        scope=scope.key,
    )
    return "Compaction will run before the next reply in this conversation scope."
