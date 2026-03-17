"""Agent delegation tools for MindRoom agents.

Allows an agent to delegate tasks to other configured agents via tool calls.
The delegated agent runs independently as a one-shot agent and returns its
response as the tool result.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from agno.tools import Toolkit

from mindroom.agents import create_agent, describe_agent
from mindroom.knowledge.manager import ensure_agent_knowledge_managers
from mindroom.knowledge.utils import get_knowledge_for_base, resolve_agent_knowledge
from mindroom.logging_config import get_logger

if TYPE_CHECKING:
    from mindroom.config.main import Config
    from mindroom.constants import RuntimePaths
    from mindroom.knowledge.manager import KnowledgeManager
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
    ) -> None:
        self._agent_name = agent_name
        self._delegate_to = delegate_to
        self._runtime_paths = runtime_paths
        self._config = config
        self._execution_identity = execution_identity
        self._delegation_depth = delegation_depth

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
            request_knowledge_managers: dict[str, KnowledgeManager] = await ensure_agent_knowledge_managers(
                agent_name,
                self._config,
                self._runtime_paths,
                execution_identity=self._execution_identity,
            )

            knowledge = resolve_agent_knowledge(
                agent_name,
                self._config,
                lambda base_id: get_knowledge_for_base(
                    base_id,
                    config=self._config,
                    runtime_paths=self._runtime_paths,
                    request_knowledge_managers=request_knowledge_managers,
                    execution_identity=self._execution_identity,
                ),
            )
            agent = create_agent(
                agent_name,
                self._config,
                runtime_paths=self._runtime_paths,
                execution_identity=self._execution_identity,
                knowledge=knowledge,
                include_interactive_questions=False,
                delegation_depth=self._delegation_depth + 1,
            )
            logger.info(
                "Delegating task",
                from_agent=self._agent_name,
                to_agent=agent_name,
                depth=self._delegation_depth + 1,
                task_preview=task[:100],
            )
            response = await agent.arun(task)
        except Exception as e:
            logger.exception(
                "Delegation failed",
                from_agent=self._agent_name,
                to_agent=agent_name,
                error=str(e),
            )
            return f"Delegation to '{agent_name}' failed: {e}"
        else:
            return response.content or "Agent completed the task but returned no content."
