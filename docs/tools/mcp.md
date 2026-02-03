---
icon: lucide/plug
---

# MCP Tools

MindRoom supports the Model Context Protocol (MCP) for connecting to external tool servers.

## What is MCP?

MCP (Model Context Protocol) is an open protocol that enables AI models to connect to external data sources and tools through a standardized interface.

## Configuring MCP Tools

Add MCP servers to your agent configuration:

```yaml
agents:
  assistant:
    display_name: Assistant
    role: A helpful assistant with MCP tools
    model: sonnet
    tools:
      - file
      - mcp  # Enable MCP tools
    mcp_servers:
      - name: filesystem
        command: npx
        args: [-y, "@modelcontextprotocol/server-filesystem", "/path/to/allowed/dir"]
      - name: github
        command: npx
        args: [-y, "@modelcontextprotocol/server-github"]
        env:
          GITHUB_PERSONAL_ACCESS_TOKEN: ${GITHUB_TOKEN}
```

## Available MCP Servers

The MCP ecosystem includes servers for:

- **Filesystem** - File operations
- **GitHub** - Repository management
- **Postgres** - Database queries
- **Brave Search** - Web search
- **Google Drive** - Document access
- **Slack** - Messaging

See the [MCP servers directory](https://github.com/modelcontextprotocol/servers) for more options.

## Creating Custom MCP Servers

You can create your own MCP servers to expose custom tools:

```python
from mcp.server import Server
from mcp.types import Tool

server = Server("my-server")

@server.tool()
def my_tool(arg: str) -> str:
    """My custom tool."""
    return f"Result: {arg}"

server.run()
```
