"""Manual history compaction request interface."""

from __future__ import annotations

from functools import partial
from typing import TYPE_CHECKING

from mindroom.history.policy import describe_compaction_unavailability, resolve_history_execution_plan
from mindroom.history.session_context import create_scope_session_storage
from mindroom.history.storage import first_scope_session, set_force_compaction
from mindroom.logging_config import get_logger
from mindroom.session_storage.lanes import run_session_storage_operation

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
    active_model_name: str | None = None,
    room_id: str | None = None,
) -> str:
    """Schedule text compaction before the next reply in ``scope``, the history scope the running entity stores to.

    ``agent_name`` names the agent that asked, a team member when ``scope`` is its team's; only the log reads it.
    Return the message the caller reports.
    """
    # An agent scope's id is its agent; an ad-hoc team has no authored settings of its own.
    entity_name = scope.scope_id if scope.kind == "agent" or scope.scope_id in config.teams else None
    runtime_model = config.resolve_runtime_model(
        entity_name=entity_name,
        active_model_name=active_model_name,
        room_id=room_id,
        runtime_paths=runtime_paths,
    )
    execution_plan = resolve_history_execution_plan(
        config=config,
        compaction_config=config.resolve_entity(entity_name).compaction_config,
        has_authored_compaction_config=True,
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
        partial(set_force_compaction, session=first_scope_session(scope, session_id), scope=scope, forced=True),
    )
    logger.info(
        "Manual compaction scheduled",
        agent=agent_name,
        scope=scope.key,
    )
    return "Compaction will run before the next reply in this conversation scope."
