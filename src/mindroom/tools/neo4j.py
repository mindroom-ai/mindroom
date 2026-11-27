"""Neo4j tool configuration and native implementation."""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any, Literal, LiteralString, cast

from mindroom.logging_config import get_logger
from mindroom.tool_system.authoring import Toolkit
from mindroom.tool_system.declarations import (
    ConfigField,
    SetupType,
    ToolCategory,
    ToolFileAccess,
    ToolStatus,
    bool_config_field,
)
from mindroom.tool_system.registration import register_tool_with_metadata
from mindroom.tools.native_helpers import (
    UNSET,
    narrow_action_enum,
    require_enabled_action,
    require_non_empty,
    resolve_bool_toggle,
)

if TYPE_CHECKING:
    from mindroom.tool_system.tool_function import ToolFunction

logger = get_logger(__name__)

_Neo4jInspectAction = Literal["list_labels", "list_relationship_types", "get_schema"]


class _Neo4jTools(Toolkit):
    """Inspect graph schemas and run Cypher queries against Neo4j."""

    def __init__(
        self,
        uri: str | None = None,
        user: str | None = None,
        password: str | None = None,
        database: str | None = None,
        enable_list_labels: bool | None = UNSET,
        enable_list_relationships: bool | None = UNSET,
        enable_get_schema: bool | None = UNSET,
        enable_run_cypher: bool | None = UNSET,
        all: bool | None = UNSET,  # noqa: A002 - matches ConfigField name
    ) -> None:
        if not isinstance(user, str) or not user.strip() or not isinstance(password, str) or not password.strip():
            msg = "Username and password for Neo4j are required"
            raise ValueError(msg)

        self.uri = uri.strip() if isinstance(uri, str) and uri.strip() else "bolt://localhost:7687"
        self.user = user.strip()
        self.password = password
        self.database = database.strip() if isinstance(database, str) and database.strip() else "neo4j"

        enable_all = resolve_bool_toggle(all, default=False)
        inspect_actions: list[str] = []
        if enable_all or resolve_bool_toggle(enable_list_labels, default=True):
            inspect_actions.append("list_labels")
        if enable_all or resolve_bool_toggle(enable_list_relationships, default=True):
            inspect_actions.append("list_relationship_types")
        if enable_all or resolve_bool_toggle(enable_get_schema, default=True):
            inspect_actions.append("get_schema")
        self._inspect_actions = tuple(inspect_actions)

        tools: list[Any] = []
        if self._inspect_actions:
            tools.append(self.neo4j_inspect)
        if enable_all or resolve_bool_toggle(enable_run_cypher, default=True):
            tools.append(self.run_cypher_query)

        super().__init__(name="neo4j_tools", tools=tools)

    def offered_function(self, function: ToolFunction) -> ToolFunction:
        """Restrict ``neo4j_inspect`` actions to the ones enabled on this instance."""
        if function.name == "neo4j_inspect":
            return narrow_action_enum(function, self._inspect_actions)
        return function

    def neo4j_inspect(self, action: _Neo4jInspectAction) -> str:
        """Inspect Neo4j graph metadata and schema.

        Actions:
        - `list_labels`: return all node labels.
        - `list_relationship_types`: return all relationship types.
        - `get_schema`: return graph schema visualization data.
        """
        if (disabled := require_enabled_action(action, self._inspect_actions)) is not None:
            return disabled

        try:
            from neo4j import GraphDatabase

            with (
                GraphDatabase.driver(self.uri, auth=(self.user, self.password)) as driver,
                driver.session(database=self.database) as session,
            ):
                if action == "list_labels":
                    result = session.run("CALL db.labels()")
                    return json.dumps([record["label"] for record in result], default=str)
                if action == "list_relationship_types":
                    result = session.run("CALL db.relationshipTypes()")
                    return json.dumps([record["relationshipType"] for record in result], default=str)
                if action == "get_schema":
                    result = session.run("CALL db.schema.visualization()")
                    return json.dumps(result.data(), default=str)
        except Exception as exc:
            logger.exception("Error inspecting Neo4j database", action=action)
            return f"Error inspecting Neo4j ({action}): {exc}"
        return f"Unsupported action: {action}"

    def run_cypher_query(self, query: str) -> str:
        """Execute a Cypher query against the Neo4j database and return JSON records."""
        try:
            from neo4j import GraphDatabase

            cypher = require_non_empty(query, "query")
            with (
                GraphDatabase.driver(self.uri, auth=(self.user, self.password)) as driver,
                driver.session(database=self.database) as session,
            ):
                result = session.run(cast("LiteralString", cypher))
                return json.dumps(result.data(), default=str)
        except Exception as exc:
            logger.exception("Error running Neo4j Cypher query")
            return f"Error running Cypher query: {exc}"


@register_tool_with_metadata(
    name="neo4j",
    file_access=ToolFileAccess.NONE,
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
            env=("NEO4J_URI",),
            label="URI",
            type="url",
            required=False,
            default=None,
            placeholder="bolt://localhost:7687",
            description="Neo4j connection URI",
        ),
        ConfigField(
            name="user",
            env=("NEO4J_USERNAME",),
            label="Username",
            type="text",
            required=True,
            placeholder="neo4j",
            description="Neo4j username",
        ),
        ConfigField(
            name="password",
            env=("NEO4J_PASSWORD",),
            label="Password",
            type="password",
            required=True,
            description="Neo4j password",
        ),
        ConfigField(
            name="database",
            label="Database",
            type="text",
            required=False,
            default=None,
            placeholder="neo4j",
        ),
        bool_config_field("enable_list_labels", "Enable List Labels"),
        bool_config_field("enable_list_relationships", "Enable List Relationships"),
        bool_config_field("enable_get_schema", "Enable Get Schema"),
        bool_config_field("enable_run_cypher", "Enable Run Cypher"),
        bool_config_field("all", "All", default=False),
    ],
    dependencies=["neo4j"],
    docs_url="https://docs.mindroom.chat/tools/data-and-databases/#neo4j",
    function_names=("neo4j_inspect", "run_cypher_query"),
    action_scoped_grants=("neo4j_inspect",),
)
def neo4j_tools() -> type[Toolkit]:
    """Return Neo4j tools for graph database operations."""
    return _Neo4jTools
