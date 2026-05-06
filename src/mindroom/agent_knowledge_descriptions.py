"""Model-facing knowledge-search tool descriptions for agents."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from agno.agent import Agent
from agno.tools.function import Function

from mindroom.knowledge_source_descriptions import KnowledgeSourceDescription, KnowledgeWithSourceDescriptions

if TYPE_CHECKING:
    from agno.knowledge.knowledge import Knowledge
    from agno.run import RunContext
    from agno.run.agent import RunOutput
    from agno.session import AgentSession

_KNOWLEDGE_SEARCH_TOOL_NAME = "search_knowledge_base"


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


def _knowledge_search_tool_description(sources: tuple[KnowledgeSourceDescription, ...]) -> str:
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
    lines.append("Use this when the answer may depend on these sources.")
    return "\n".join(lines)


def _annotate_knowledge_search_tool(tools: list[Any], sources: tuple[KnowledgeSourceDescription, ...]) -> None:
    """Attach MindRoom source descriptions to Agno's generated knowledge-search tool."""
    description = _knowledge_search_tool_description(sources)
    for tool in tools:
        if isinstance(tool, Function) and tool.name == _KNOWLEDGE_SEARCH_TOOL_NAME:
            tool.description = description


class KnowledgeToolDescribingAgent(Agent):
    """Agent subclass that owns MindRoom's model-facing knowledge-search metadata."""

    knowledge_sources: tuple[KnowledgeSourceDescription, ...] = ()

    def get_tools(
        self,
        run_response: RunOutput,
        run_context: RunContext,
        session: AgentSession,
        user_id: str | None = None,
    ) -> list[Any]:
        """Return Agno tools with MindRoom knowledge-source metadata attached."""
        tools = super().get_tools(run_response, run_context, session, user_id=user_id)
        _annotate_knowledge_search_tool(tools, self.knowledge_sources)
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
        tools = await super().aget_tools(
            run_response,
            run_context,
            session,
            user_id=user_id,
            check_mcp_tools=check_mcp_tools,
        )
        _annotate_knowledge_search_tool(tools, self.knowledge_sources)
        return tools
