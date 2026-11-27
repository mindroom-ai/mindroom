"""Knowledge graph tool metadata registration.

The toolkit (``mindroom.custom_tools.knowledge_graph.KnowledgeGraphTools``) reads, and where allowed edits, the caller's
own graph, which it resolves from agent context in ``build_agent_toolkit()``, so it has no generic factory.
"""

from mindroom.tool_system.declarations import ToolCategory, ToolFileAccess, ToolMetadata
from mindroom.tool_system.registration import register_builtin_tool_metadata

register_builtin_tool_metadata(
    ToolMetadata(
        name="knowledge_graph",
        file_access=ToolFileAccess.NONE,
        display_name="Knowledge Graph",
        description=(
            "Look up facts, entities, and their sources in the agent's knowledge graph, and with knowledge_graph_edits "
            "delete wrong facts and entities, merge duplicates, and rewrite summaries"
        ),
        docs_url="https://docs.mindroom.chat/knowledge-graph/#the-knowledge_graph-tool",
        category=ToolCategory.PRODUCTIVITY,
        icon="Network",
        icon_color="text-teal-500",
        config_fields=[],
        dependencies=["graphiti-core"],
        function_names=("knowledge_graph",),
        action_scoped_grants=("knowledge_graph",),
    ),
)
