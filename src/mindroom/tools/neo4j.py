"""Neo4j tool configuration."""

from __future__ import annotations

from typing import TYPE_CHECKING

from mindroom.tools_metadata import ConfigField, SetupType, ToolCategory, ToolStatus, register_tool_with_metadata

if TYPE_CHECKING:
    from agno.tools.neo4j import Neo4jTools


@register_tool_with_metadata(
    name="neo4j",
    display_name="Neo4j",
    description="Query Neo4j graph databases - list labels, relationships, get schema, and run Cypher queries",
    category=ToolCategory.DEVELOPMENT,
    status=ToolStatus.REQUIRES_CONFIG,
    setup_type=SetupType.SPECIAL,
    icon="SiNeo4J",
    icon_color="text-blue-500",
    config_fields=[
        ConfigField(
            name="uri",
            label="URI",
            type="url",
            required=False,
            default=None,
            placeholder="bolt://localhost:7687",
            description="Neo4j connection URI (falls back to NEO4J_URI env var)",
        ),
        ConfigField(
            name="user",
            label="Username",
            type="text",
            required=True,
            placeholder="neo4j",
            description="Neo4j username (falls back to NEO4J_USERNAME env var)",
        ),
        ConfigField(
            name="password",
            label="Password",
            type="password",
            required=True,
            description="Neo4j password (falls back to NEO4J_PASSWORD env var)",
        ),
        ConfigField(
            name="database",
            label="Database",
            type="text",
            required=False,
            default=None,
            placeholder="neo4j",
        ),
        ConfigField(
            name="enable_list_labels",
            label="Enable List Labels",
            type="boolean",
            required=False,
            default=True,
        ),
        ConfigField(
            name="enable_list_relationships",
            label="Enable List Relationships",
            type="boolean",
            required=False,
            default=True,
        ),
        ConfigField(
            name="enable_get_schema",
            label="Enable Get Schema",
            type="boolean",
            required=False,
            default=True,
        ),
        ConfigField(
            name="enable_run_cypher",
            label="Enable Run Cypher",
            type="boolean",
            required=False,
            default=True,
        ),
        ConfigField(
            name="all",
            label="All",
            type="boolean",
            required=False,
            default=False,
        ),
    ],
    dependencies=["neo4j"],
    docs_url="https://docs.agno.com/tools/toolkits/others/neo4j",
)
def neo4j_tools() -> type[Neo4jTools]:
    """Return Neo4j tools for graph database operations."""
    from agno.tools.neo4j import Neo4jTools

    return Neo4jTools
