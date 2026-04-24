"""Agent delegation tools for MindRoom agents.

Allows an agent to delegate tasks to other configured agents via tool calls.
The delegated agent runs independently as a one-shot agent and returns its
response as the tool result.
"""

from __future__ import annotations

from dataclasses import replace
from typing import TYPE_CHECKING
from uuid import uuid4

from agno.tools import Toolkit

from mindroom.agent_descriptions import describe_agent
from mindroom.ai import ai_response
from mindroom.hooks import EnrichmentItem
from mindroom.knowledge import (
    KnowledgeAvailability,
    KnowledgeManager,
    ensure_request_knowledge_managers,
    format_knowledge_availability_notice,
    get_agent_knowledge,
)
from mindroom.logging_config import get_logger
from mindroom.tool_system.runtime_context import (
    ToolRuntimeContext,
    get_tool_runtime_context,
    tool_runtime_context,
)

if TYPE_CHECKING:
    from mindroom.config.main import Config
    from mindroom.constants import RuntimePaths
    from mindroom.knowledge.refresh_owner import KnowledgeRefreshOwner
    from mindroom.tool_system.worker_routing import ToolExecutionIdentity

logger = get_logger(__name__)

MAX_DELEGATION_DEPTH = 3


class DelegateTools(Toolkit):
    """Tools that let an agent delegate tasks to other configured agents."""

    def __init__(
        self,
        agent_name: str,
        delegate_to: list[str],
        runtime_paths: RuntimePaths,
        config: Config,
        execution_identity: ToolExecutionIdentity | None = None,
        delegation_depth: int = 0,
        refresh_owner: KnowledgeRefreshOwner | None = None,
    ) -> None:
        self._agent_name = agent_name
        self._delegate_to = delegate_to
        self._runtime_paths = runtime_paths
        self._config = config
        self._execution_identity = execution_identity
        self._delegation_depth = delegation_depth
        self._refresh_owner = refresh_owner

        super().__init__(
            name="delegate",
            instructions=self._build_instructions(),
            tools=[self.delegate_task],
        )

    def _build_instructions(self) -> str:
        """Build toolkit instructions listing available delegation targets."""
        lines = ["You can delegate tasks to the following agents:"]
        for target_name in self._delegate_to:
            description = describe_agent(target_name, self._config)
            lines.append(f"\n{description}")
        lines.append(
            "\nUse delegate_task to send a task to one of these agents. "
            "The agent will execute the task independently and return its response.",
        )
        return "\n".join(lines)

    async def delegate_task(self, agent_name: str, task: str) -> str:
        """Delegate a task to another agent and return its response.

        Use this when you need a specialist agent to handle a specific subtask.
        The delegated agent runs independently with no shared history.

        Args:
            agent_name: Name of the agent to delegate to (must be one of your configured targets).
            task: The task description or prompt to send to the agent.

        Returns:
            The delegated agent's response, or an error message if delegation failed.

        """
        if agent_name not in self._delegate_to:
            available = ", ".join(self._delegate_to)
            return f"Cannot delegate to '{agent_name}'. Available agents: {available}"

        if not task or not task.strip():
            return "Cannot delegate an empty task. Please provide a task description."

        try:
            request_knowledge_managers: dict[str, KnowledgeManager] = await ensure_request_knowledge_managers(
                [agent_name],
                config=self._config,
                runtime_paths=self._runtime_paths,
                execution_identity=self._execution_identity,
            )

            unavailable_bases: dict[str, KnowledgeAvailability] = {}
            knowledge = get_agent_knowledge(
                agent_name,
                self._config,
                self._runtime_paths,
                request_knowledge_managers=request_knowledge_managers,
                on_unavailable_bases=unavailable_bases.update,
                refresh_owner=self._refresh_owner,
            )
            system_enrichment_items: tuple[EnrichmentItem, ...] = ()
            notice = format_knowledge_availability_notice(unavailable_bases)
            if notice is not None:
                system_enrichment_items = (
                    EnrichmentItem(key="knowledge_availability", text=notice, cache_policy="volatile"),
                )
            logger.info(
                "Delegating task",
                from_agent=self._agent_name,
                to_agent=agent_name,
                depth=self._delegation_depth + 1,
                task_preview=task[:100],
            )
            session_id = f"delegate:{self._agent_name}:{agent_name}:{uuid4()}"
            execution_identity = (
                replace(self._execution_identity, agent_name=agent_name, session_id=session_id)
                if self._execution_identity is not None
                else None
            )
            runtime_context = get_tool_runtime_context()
            room_id = _resolve_delegated_room_id(
                runtime_context=runtime_context,
                execution_identity=execution_identity,
            )
            delegated_runtime_context = self._build_delegated_runtime_context(
                agent_name=agent_name,
                session_id=session_id,
                room_id=room_id,
                runtime_context=runtime_context,
            )
            with tool_runtime_context(delegated_runtime_context):
                response = await ai_response(
                    agent_name=agent_name,
                    prompt=task,
                    session_id=session_id,
                    runtime_paths=self._runtime_paths,
                    config=self._config,
                    knowledge=knowledge,
                    user_id=execution_identity.requester_id if execution_identity is not None else None,
                    room_id=room_id,
                    include_interactive_questions=False,
                    execution_identity=execution_identity,
                    delegation_depth=self._delegation_depth + 1,
                    system_enrichment_items=system_enrichment_items,
                    refresh_owner=self._refresh_owner,
                )
        except Exception as e:
            logger.exception(
                "Delegation failed",
                from_agent=self._agent_name,
                to_agent=agent_name,
                error=str(e),
            )
            return f"Delegation to '{agent_name}' failed: {e}"
        else:
            return response or "Agent completed the task but returned no content."

    def _build_delegated_runtime_context(
        self,
        *,
        agent_name: str,
        session_id: str,
        room_id: str | None,
        runtime_context: ToolRuntimeContext | None,
    ) -> ToolRuntimeContext | None:
        """Return the child tool runtime context for one delegated run."""
        if runtime_context is None:
            return None
        runtime_model = self._config.resolve_runtime_model(
            entity_name=agent_name,
            room_id=room_id,
            runtime_paths=self._runtime_paths,
        )
        return replace(
            runtime_context,
            agent_name=agent_name,
            active_model_name=runtime_model.model_name,
            session_id=session_id,
        )


def _resolve_delegated_room_id(
    *,
    runtime_context: ToolRuntimeContext | None,
    execution_identity: ToolExecutionIdentity | None,
) -> str | None:
    """Resolve the room context that should apply to a delegated child run."""
    if runtime_context is not None:
        return runtime_context.room_id
    if execution_identity is not None:
        return execution_identity.room_id
    return None
