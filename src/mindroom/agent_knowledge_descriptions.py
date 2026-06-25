"""Model-facing knowledge-search tool descriptions for agents."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from agno.agent import Agent
from agno.tools import Toolkit
from agno.tools.function import Function

from mindroom.knowledge_source_descriptions import KnowledgeSourceDescription, KnowledgeWithSourceDescriptions
from mindroom.logging_config import get_logger

if TYPE_CHECKING:
    from agno.knowledge.knowledge import Knowledge
    from agno.models.base import Model
    from agno.run import RunContext
    from agno.run.agent import RunOutput
    from agno.session import AgentSession

_KNOWLEDGE_SEARCH_TOOL_NAME = "search_knowledge_base"
_OPENAI_PROVIDER_VISIBLE_TOOL_LIMIT = 128

logger = get_logger(__name__)


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


def _uses_openai_tool_limit(model: Model | None) -> bool:
    if model is None:
        return False
    return "openai" in model.get_provider().lower()


def _dict_tool_name(tool: dict[Any, Any]) -> str:
    function = tool.get("function")
    if isinstance(function, dict):
        name = function.get("name")
        if isinstance(name, str):
            return name
    name = tool.get("name")
    return name if isinstance(name, str) else ""


def _toolkit_new_functions(
    toolkit: Toolkit,
    *,
    seen_function_names: set[str],
    async_mode: bool,
) -> list[tuple[str, Function]]:
    functions = toolkit.get_async_functions() if async_mode else toolkit.get_functions()
    return [(name, function) for name, function in functions.items() if name not in seen_function_names]


def _append_toolkit_with_cap(
    limited_tools: list[Any],
    omitted_function_names: list[str],
    seen_function_names: set[str],
    toolkit: Toolkit,
    *,
    async_mode: bool,
) -> None:
    new_functions = _toolkit_new_functions(
        toolkit,
        seen_function_names=seen_function_names,
        async_mode=async_mode,
    )
    if len(seen_function_names) + len(new_functions) <= _OPENAI_PROVIDER_VISIBLE_TOOL_LIMIT:
        limited_tools.append(toolkit)
        seen_function_names.update(name for name, _function in new_functions)
        return

    remaining = _OPENAI_PROVIDER_VISIBLE_TOOL_LIMIT - len(seen_function_names)
    if remaining > 0:
        kept_functions = new_functions[:remaining]
        limited_tools.extend(function for _name, function in kept_functions)
        seen_function_names.update(name for name, _function in kept_functions)
    omitted_function_names.extend(name for name, _function in new_functions[remaining:])


def _standalone_tool_function_name(tool: object) -> str:
    if isinstance(tool, Function):
        return tool.name
    if isinstance(tool, dict):
        return _dict_tool_name(tool)
    return ""


def _append_standalone_tool_with_cap(
    limited_tools: list[Any],
    omitted_function_names: list[str],
    seen_function_names: set[str],
    tool: object,
) -> None:
    function_name = _standalone_tool_function_name(tool)
    if function_name and function_name in seen_function_names:
        limited_tools.append(tool)
        return
    if len(seen_function_names) < _OPENAI_PROVIDER_VISIBLE_TOOL_LIMIT:
        limited_tools.append(tool)
        seen_function_names.add(function_name or f"<unnamed:{len(seen_function_names)}>")
        return
    omitted_function_names.append(function_name or "<unnamed>")


def _limit_provider_visible_tools_for_model(
    model: Model | None,
    tools: list[Any],
    *,
    async_mode: bool,
) -> list[Any]:
    """Cap provider-visible tools before OpenAI rejects the request payload."""
    if not _uses_openai_tool_limit(model):
        return tools

    limited_tools: list[Any] = []
    seen_function_names: set[str] = set()
    omitted_function_names: list[str] = []

    for tool in tools:
        if isinstance(tool, Toolkit):
            _append_toolkit_with_cap(
                limited_tools,
                omitted_function_names,
                seen_function_names,
                tool,
                async_mode=async_mode,
            )
            continue

        _append_standalone_tool_with_cap(
            limited_tools,
            omitted_function_names,
            seen_function_names,
            tool,
        )

    if omitted_function_names:
        logger.warning(
            "Capped provider-visible tool list for OpenAI model",
            model_id=model.id if model is not None else None,
            kept_tool_count=len(seen_function_names),
            omitted_tool_count=len(omitted_function_names),
            omitted_tool_names=omitted_function_names[:20],
        )
    return limited_tools


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
        return _limit_provider_visible_tools_for_model(self.model, tools, async_mode=False)

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
        return _limit_provider_visible_tools_for_model(self.model, tools, async_mode=True)
