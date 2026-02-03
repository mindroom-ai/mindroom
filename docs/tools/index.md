---
icon: lucide/wrench
---

# Tools

MindRoom includes 80+ tool integrations that agents can use to interact with external services.

## Enabling Tools

Tools are enabled per-agent in the configuration:

```yaml
agents:
  assistant:
    display_name: Assistant
    role: A helpful assistant with file and web access
    model: sonnet
    tools:
      - file
      - shell
      - github
      - web_search
```

## Tool Categories

### File & System
- `file` - Read, write, and manage files
- `shell` - Execute shell commands
- `docker` - Manage Docker containers

### AI & Search
- `web_search` - Search the web
- `tavily` - AI-powered web search
- `exa` - Neural search engine
- `arxiv` - Search academic papers

### Communication
- `slack` - Send messages to Slack
- `gmail` - Send and read emails
- `telegram` - Telegram bot integration
- `twilio` - SMS and voice calls

### Development
- `github` - Manage GitHub repos, issues, PRs
- `jira` - Issue tracking
- `confluence` - Wiki documentation

### Productivity
- `google_calendar` - Calendar management
- `google_sheets` - Spreadsheet access
- `todoist` - Task management
- `trello` - Board management

See the full list in:

- [Built-in Tools](builtin.md) - All available tool integrations
- [MCP Tools](mcp.md) - Model Context Protocol tools
