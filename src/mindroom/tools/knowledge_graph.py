"""Knowledge graph tool metadata registration.

The toolkit (``mindroom.custom_tools.knowledge_graph.KnowledgeGraphTools``) reads the caller's own graph, which it
resolves from agent context in ``create_agent()``, so it has no generic factory in ``TOOL_REGISTRY``.
"""

from mindroom.tool_system.declarations import (
    SetupType,
    ToolCategory,
    ToolFileAccess,
    ToolMetadata,
    ToolStatus,
)
from mindroom.tool_system.registration import register_builtin_tool_metadata

register_builtin_tool_metadata(
    ToolMetadata(
        name="knowledge_graph",
        file_access=ToolFileAccess.NONE,
        display_name="Knowledge Graph",
        description="Look up facts, entities, and their sources in the agent's knowledge graph",
        category=ToolCategory.PRODUCTIVITY,
        status=ToolStatus.AVAILABLE,
        setup_type=SetupType.NONE,
        icon="Network",
        icon_color="text-teal-500",
        config_fields=[],
        dependencies=["graphiti-core"],
        function_names=("graph_entity", "search_graph"),
    ),
)
