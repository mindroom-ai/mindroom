# Gmail MCP Server Implementation Comparison

## Available Options

### 1. **jeremyjordan/mcp-gmail** ⭐ Recommended
**GitHub**: https://github.com/jeremyjordan/mcp-gmail

**Pros**:
- Clean, well-structured Python implementation
- Uses official MCP Python SDK
- Includes pre-commit hooks and testing
- Active development (recent commits)
- Proper package management with pyproject.toml
- Tools: compose_email, send_email, search_emails, query_emails

**Cons**:
- Only 5 stars (newer project)
- May need customization for MindRoom

**Installation**:
```bash
git clone https://github.com/jeremyjordan/mcp-gmail
cd mcp-gmail
uv sync
```

### 2. **GongRzhe/Gmail-MCP-Server**
**GitHub**: https://github.com/GongRzhe/Gmail-MCP-Server

**Pros**:
- Most comprehensive feature set
- Batch operations support
- Attachment handling
- Label and filter management
- Docker support
- Multiple authentication methods

**Cons**:
- More complex setup
- May be over-engineered for basic needs
- Uses Node.js/TypeScript (not Python)

### 3. **theposch/gmail-mcp**
**GitHub**: https://github.com/theposch/gmail-mcp

**Pros**:
- Single-file architecture (simpler)
- Auto token refresh
- Natural language interface focus

**Cons**:
- Less documentation
- Fewer features
- Less active development

## Official MCP Python SDK Approach

The official SDK uses `FastMCP` which makes creating servers very simple:

```python
from mcp.server.fastmcp import FastMCP

mcp = FastMCP("Gmail")

@mcp.tool()
def search_emails(query: str, max_results: int = 10) -> list[dict]:
    """Search Gmail emails"""
    # Implementation here
    pass

@mcp.tool()
def send_email(to: str, subject: str, body: str) -> dict:
    """Send an email"""
    # Implementation here
    pass
```

## Recommendation for MindRoom

### Option A: Use jeremyjordan/mcp-gmail (Fastest)
**Why**:
- It's Python-based and well-structured
- Uses the official MCP SDK
- Can be integrated quickly
- Good starting point for customization

**Integration Steps**:
1. Clone the repository
2. Study the implementation
3. Adapt authentication to use your existing OAuth flow
4. Integrate with MindRoom's agent system
5. Extend with additional features as needed

### Option B: Build Custom Using FastMCP (Most Control)
**Why**:
- Full control over implementation
- Can leverage your existing Gmail code
- Simpler architecture
- Better integration with MindRoom

**Implementation Plan**:
```python
# src/mindroom/mcp/gmail_server.py
from mcp.server.fastmcp import FastMCP
from typing import Optional, List, Dict
import json
from pathlib import Path

# Import your existing Gmail functions
from mindroom.gmail_tool import (
    search_gmail,
    read_latest_emails,
    read_unread_emails,
    get_gmail_credentials
)

# Create MCP server
gmail_mcp = FastMCP("Gmail")

@gmail_mcp.tool()
def search_emails(
    query: str,
    max_results: int = 5,
    include_body: bool = True
) -> str:
    """Search Gmail for emails matching a query.

    Args:
        query: Gmail search query (e.g., 'is:unread', 'from:example@gmail.com')
        max_results: Maximum number of results
        include_body: Whether to include email body

    Returns:
        Email summaries as formatted string
    """
    return search_gmail(query, max_results, include_body)

@gmail_mcp.tool()
def get_latest_emails(max_results: int = 5) -> str:
    """Get the latest emails from inbox."""
    return read_latest_emails(max_results)

@gmail_mcp.tool()
def get_unread_emails(max_results: int = 5) -> str:
    """Get unread emails."""
    return read_unread_emails(max_results)

@gmail_mcp.resource("gmail://status")
def get_status() -> dict:
    """Get Gmail connection status."""
    creds = get_gmail_credentials()
    return {
        "connected": creds is not None,
        "scopes": ["gmail.readonly"] if creds else []
    }

# Add more tools as needed...
```

### Option C: Hybrid Approach (Best of Both)
1. Start with jeremyjordan/mcp-gmail for reference
2. Build custom server using FastMCP
3. Copy the best patterns from existing implementations
4. Integrate with your existing OAuth and widget

## Integration with MindRoom

### Current Architecture
```
Widget (Frontend)
    ↓ OAuth
Backend (FastAPI)
    ↓ Token
Gmail Tool (Agno)
    ↓
Agents
```

### Target MCP Architecture
```
Widget (Frontend)
    ↓ OAuth
Backend (FastAPI)
    ↓ Token
Gmail MCP Server (FastMCP)
    ↓ MCP Protocol
MCP Client in Agents
    ↓
Agents use Gmail via MCP
```

### Migration Path
1. **Phase 1**: Install MCP SDK
   ```bash
   uv add "mcp[cli]"
   ```

2. **Phase 2**: Create Gmail MCP server wrapper
   - Wrap existing functions in MCP tools
   - Keep existing OAuth flow
   - Test with MCP dev server

3. **Phase 3**: Create MCP client for agents
   - Build client that agents can use
   - Bridge between Agno and MCP

4. **Phase 4**: Gradual migration
   - Run both systems in parallel
   - Migrate agents one by one
   - Deprecate old system

## Decision Matrix

| Criteria | Use Existing | Build Custom | Hybrid |
|----------|-------------|--------------|--------|
| Time to implement | 1-2 days | 3-5 days | 2-3 days |
| Maintenance burden | Medium | High | Medium |
| Customization | Limited | Full | Full |
| Learning curve | Low | Medium | Medium |
| Integration effort | Medium | Low | Low |
| Feature completeness | High | Start low | Medium |

## Recommended Action Plan

Given your existing Gmail implementation, I recommend **Option B or C** (Build Custom or Hybrid):

1. **Install MCP Python SDK**
2. **Create `src/mindroom/mcp/` directory**
3. **Build Gmail MCP server using FastMCP**
4. **Wrap your existing Gmail functions**
5. **Test with MCP dev tools**
6. **Integrate with agents**
7. **Extend with features from other implementations**

This approach:
- Leverages your existing working code
- Gives you full control
- Creates a template for other integrations
- Maintains backward compatibility
- Allows gradual migration

## Next Steps

1. Install MCP SDK: `uv add "mcp[cli]"`
2. Create base MCP server structure
3. Wrap existing Gmail functions
4. Test standalone MCP server
5. Create MCP client for agents
6. Integrate and test with MindRoom

Would you like me to start implementing the custom Gmail MCP server using FastMCP?
