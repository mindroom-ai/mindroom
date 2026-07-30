"""Model-facing knowledge-search tool descriptions for agents."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from agno.agent import Agent
from agno.tools.function import Function
from agno.tools.toolkit import Toolkit

from mindroom.knowledge_source_descriptions import KnowledgeSourceDescription, KnowledgeWithSourceDescriptions

if TYPE_CHECKING:
    from collections.abc import Callable

    from agno.knowledge.knowledge import Knowledge
    from agno.run import RunContext
    from agno.run.agent import RunOutput
    from agno.session import AgentSession

_KNOWLEDGE_SEARCH_TOOL_NAME = "search_knowledge_base"
_MEMORY_SEARCH_TOOL_NAME = "search_memories"


def _normalize_description(value: str) -> str:
    return " ".join(value.split())


def knowledge_source_descriptions(knowledge: Knowledge) -> tuple[KnowledgeSourceDescription, ...]:
    """Return the resolved queryable knowledge sources one agent can search."""
    if isinstance(knowledge, KnowledgeWithSourceDescriptions):
        return knowledge.source_descriptions

    if knowledge.name is None:
        return ()

    return (
        KnowledgeSourceDescription(
            base_id=knowledge.name,
            description=_normalize_description(knowledge.description or ""),
        ),
    )


def _knowledge_search_tool_description(
    sources: tuple[KnowledgeSourceDescription, ...],
    *,
    memory_search_available: bool,
) -> str:
    """Build the description shown to the model for the knowledge-search tool."""
    if not sources:
        return "Search this agent's configured knowledge bases for information about a query."

    lines = [
        "Search this agent's configured knowledge bases for information about a query.",
        "Available sources:",
    ]
    for source in sources:
        description = source.description or "No description configured."
        lines.append(f"- {source.base_id}: {description}")
    lines.append("This list only describes sources available through search_knowledge_base.")
    if memory_search_available:
        lines.append("For resilient memory search, team-visible memory, and memory IDs, use search_memories.")
    return "\n".join(lines)


def _tool_function_available(tools: list[Any], function_name: str, *, async_mode: bool) -> bool:
    for tool in tools:
        if isinstance(tool, Function) and tool.name == function_name:
            return True
        if isinstance(tool, Toolkit):
            functions = tool.get_async_functions() if async_mode else tool.get_functions()
            if function_name in functions:
                return True
    return False


def _annotate_knowledge_search_tool(
    tools: list[Any],
    sources: tuple[KnowledgeSourceDescription, ...],
    *,
    async_mode: bool,
) -> None:
    """Attach MindRoom source descriptions to Agno's generated knowledge-search tool."""
    description = _knowledge_search_tool_description(
        sources,
        memory_search_available=_tool_function_available(tools, _MEMORY_SEARCH_TOOL_NAME, async_mode=async_mode),
    )
    for tool in tools:
        if isinstance(tool, Function) and tool.name == _KNOWLEDGE_SEARCH_TOOL_NAME:
            tool.description = description


def _filter_generated_functions(
    tools: list[Any],
    predicate: Callable[[Function], bool] | None,
) -> list[Any]:
    """Apply a channel policy to functions Agno adds after agent construction."""
    if predicate is None:
        return tools

    filtered: list[Any] = []
    for tool in tools:
        function = tool if isinstance(tool, Function) else Function.from_callable(tool) if callable(tool) else None
        if function is None or predicate(function):
            filtered.append(tool)
    return filtered


class KnowledgeToolDescribingAgent(Agent):
    """Agent subclass that owns MindRoom's model-facing knowledge-search metadata."""

    knowledge_sources: tuple[KnowledgeSourceDescription, ...] = ()
    tool_function_filter: Callable[[Function], bool] | None = None

    def get_tools(
        self,
        run_response: RunOutput,
        run_context: RunContext,
        session: AgentSession,
        user_id: str | None = None,
    ) -> list[Any]:
        """Return Agno tools with MindRoom knowledge-source metadata attached."""
        tools = _filter_generated_functions(
            super().get_tools(run_response, run_context, session, user_id=user_id),
            self.tool_function_filter,
        )
        _annotate_knowledge_search_tool(tools, self.knowledge_sources, async_mode=False)
        return tools

    async def aget_tools(
        self,
        run_response: RunOutput,
        run_context: RunContext,
        session: AgentSession,
        user_id: str | None = None,
        check_mcp_tools: bool = True,
    ) -> list[Any]:
        """Return async Agno tools with MindRoom knowledge-source metadata attached."""
        tools = _filter_generated_functions(
            await super().aget_tools(
                run_response,
                run_context,
                session,
                user_id=user_id,
                check_mcp_tools=check_mcp_tools,
            ),
            self.tool_function_filter,
        )
        _annotate_knowledge_search_tool(tools, self.knowledge_sources, async_mode=True)
        return tools
