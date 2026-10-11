"""Run one child turn inside its caller's tool call and settle it before returning."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import TYPE_CHECKING

from mindroom.ai import run_delegated_child_response
from mindroom.delegation.lifecycle import child_run_context, finish_child_turn, reserve_child_turn, start_child_turn
from mindroom.delegation.sessions import subagent_liveness
from mindroom.logging_config import get_logger
from mindroom.response_turn import ResponsePausedForApproval

if TYPE_CHECKING:
    from mindroom.config.main import Config
    from mindroom.constants import RuntimePaths
    from mindroom.delegation.state import DelegationChild
    from mindroom.knowledge.refresh_scheduler import KnowledgeRefreshScheduler
    from mindroom.tool_system.worker_routing import ToolExecutionIdentity

logger = get_logger(__name__)


@dataclass(frozen=True)
class _DirectChildResult:
    """What one settled child turn returns to the tool call that ran it."""

    text: str
    receipt: str
    completed: bool


async def run_direct_child_turn(
    child: DelegationChild,
    *,
    owner: ToolExecutionIdentity,
    parent_run_id: str | None,
    config: Config,
    runtime_paths: RuntimePaths,
    refresh_scheduler: KnowledgeRefreshScheduler | None,
    approval_config: Config | None = None,
) -> _DirectChildResult:
    """Reserve, run, and settle one child turn.

    A refused reservation raises ``SubagentSessionError`` and an approval pause
    raises ``ResponsePausedForApproval``; every other outcome is settled here.
    """
    async with subagent_liveness(child, runtime_paths):
        try:
            await reserve_child_turn(child, owner=owner, runtime_paths=runtime_paths)
        except asyncio.CancelledError:
            await _settle_cancelled(child, config=config, runtime_paths=runtime_paths)
            raise
        try:
            await start_child_turn(
                child,
                parent_run_id=parent_run_id,
                config=config,
                runtime_paths=runtime_paths,
                caller_execution_identity=owner,
            )
            async with child_run_context(child, config=config, runtime_paths=runtime_paths):
                response = await run_delegated_child_response(
                    child,
                    prompt=child.task,
                    config=config,
                    runtime_paths=runtime_paths,
                    refresh_scheduler=refresh_scheduler,
                    supports_native_tool_approval=False,
                    approval_config=approval_config,
                )
        except asyncio.CancelledError:
            await _settle_cancelled(child, config=config, runtime_paths=runtime_paths)
            raise
        except ResponsePausedForApproval:
            raise
        except Exception as error:
            logger.exception(
                "Delegation failed",
                from_agent=child.caller_agent_name,
                to_agent=child.child_agent_name,
                error=str(error),
            )
            receipt = await finish_child_turn(
                child,
                config=config,
                runtime_paths=runtime_paths,
                status="failed",
                reason=str(error),
            )
            return _DirectChildResult(f"Delegation to '{child.child_agent_name}' failed: {error}", receipt, False)
        receipt = await finish_child_turn(
            child,
            config=config,
            runtime_paths=runtime_paths,
            status="failed",
            reason="Delegated run ended without a retained terminal outcome.",
        )
        text = response or "Agent completed the task but returned no content."
        return _DirectChildResult(text, receipt, child.status == "completed")


async def _settle_cancelled(child: DelegationChild, *, config: Config, runtime_paths: RuntimePaths) -> None:
    await finish_child_turn(
        child,
        config=config,
        runtime_paths=runtime_paths,
        status="cancelled",
        reason="Delegation cancelled.",
    )
