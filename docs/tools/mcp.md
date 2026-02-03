---
icon: lucide/plug
---

# MCP Tools

> [!NOTE]
> MCP (Model Context Protocol) integration is planned for a future release of MindRoom. The functionality described below is not yet implemented.

## What is MCP?

MCP (Model Context Protocol) is an open protocol that enables AI models to connect to external data sources and tools through a standardized interface. It allows agents to dynamically discover and use tools exposed by MCP servers.

## Current Status

MindRoom includes the `mcp` library as a dependency, but direct MCP server configuration in agent YAML is not yet supported. The underlying Agno framework provides `MCPTools` and `MultiMCPTools` classes that MindRoom plans to integrate in a future release.

## Planned Features

When implemented, MCP support will allow:

- Connecting to external MCP servers (filesystem, GitHub, databases, etc.)
- Automatic tool discovery from MCP server capabilities
- Support for stdio, SSE, and Streamable HTTP transports

## Available MCP Servers

The MCP ecosystem includes servers for:

- **Filesystem** - File operations
- **GitHub** - Repository management
- **Postgres** - Database queries
- **Brave Search** - Web search
- **Google Drive** - Document access
- **Slack** - Messaging

See the [MCP servers directory](https://github.com/modelcontextprotocol/servers) for more options.

## Workaround: Using Agno MCPTools Directly

Until native MindRoom configuration is available, you can use MCP tools through a [custom plugin](../plugins.md):

```python
# plugins/mcp_plugin.py
from agno.tools.mcp import MCPTools
from mindroom.tools_metadata import register_tool_with_metadata, ToolCategory, ToolStatus, SetupType

@register_tool_with_metadata(
    name="my_mcp_server",
    display_name="My MCP Server",
    description="Tools from my custom MCP server",
    category=ToolCategory.DEVELOPMENT,
    status=ToolStatus.AVAILABLE,
    setup_type=SetupType.NONE,
)
def my_mcp_tools():
    """Return MCP tools from a custom server."""
    return MCPTools(command="npx -y @modelcontextprotocol/server-filesystem /path/to/dir")
```

Then reference it in your agent configuration:

```yaml
plugins:
  - plugins/mcp_plugin.py

agents:
  assistant:
    display_name: Assistant
    tools:
      - my_mcp_server
```

> [!WARNING]
> MCP tools are async and require special handling. The above workaround may have limitations compared to native support.
