# Tools

MindRoom includes 100+ tool integrations that agents can use to interact with external services.

## Enabling Tools

Tools are enabled per-agent in the configuration:

```
agents:
  assistant:
    display_name: Assistant
    role: A helpful assistant with file and web access
    model: sonnet
    tools:
      - file
      - shell
      - github
      - duckduckgo
```

You can also assign tools to all agents globally:

```
defaults:
  tools:
    - scheduler
```

`defaults.tools` are merged into each agent's own `tools` list with duplicates removed. Set `defaults.tools: []` to disable global default tools, or set `agents.<name>.include_default_tools: false` to opt out a specific agent.

## Tool Categories

Tools are organized by category:

- **Development** - File operations, shell, Docker, GitHub, Jira, Python, Airflow, code execution sandboxes (E2B, Daytona, or MindRoom's built-in [container sandbox proxy](https://docs.mindroom.chat/deployment/sandbox-proxy/index.md)), Claude Agent SDK
- **Research** - Web search (DuckDuckGo, Tavily, Exa, SerpAPI), academic papers (arXiv, PubMed), Wikipedia, Hacker News, web scraping (Firecrawl, Crawl4AI, Jina)
- **Communication** - Slack, Discord, Telegram, Twilio, WhatsApp, Webex
- **Email** - Gmail, AWS SES, Resend, generic SMTP
- **Productivity** - Google Calendar, Todoist, Google Sheets, SQL, Pandas, CSV, DuckDB
- **Social** - Reddit, X/Twitter, Zoom
- **Entertainment** - YouTube, Giphy
- **Smart Home** - Home Assistant
- **Integrations** - Composio

## Quick Examples

### Research Agent

```
agents:
  researcher:
    display_name: Researcher
    role: Find and summarize information from the web and academic sources
    model: sonnet
    tools:
      - duckduckgo
      - arxiv
      - wikipedia
      - pubmed
```

### DevOps Agent

```
agents:
  devops:
    display_name: DevOps
    role: Manage infrastructure, containers, and deployments
    model: sonnet
    tools:
      - shell
      - docker
      - github
      - aws_lambda
```

### Communication Agent

```
agents:
  notifier:
    display_name: Notifier
    role: Send notifications and messages across platforms
    model: sonnet
    tools:
      - slack
      - telegram
      - gmail
```

## Automatic Dependency Installation

Each tool declares its Python dependencies as an optional extra in `pyproject.toml`. When an agent tries to use a tool whose dependencies aren't installed, MindRoom automatically installs them at runtime:

1. **Pre-check** — uses `importlib.util.find_spec()` to detect missing packages without importing anything
1. **Locked install** — runs `uv sync --locked --inexact --extra <tool>` to install exact pinned versions from `uv.lock`
1. **Fallback** — if no lockfile is available, falls back to `uv pip install` or `pip install`

This means you don't need to install all 100+ tool dependencies upfront — only the tools your agents actually use get installed.

To disable auto-install, set the environment variable:

```
MINDROOM_NO_AUTO_INSTALL_TOOLS=1
```

To pre-install specific tool dependencies:

```
uv sync --extra gmail --extra slack --extra github
```

See the full list in:

- [Built-in Tools](https://docs.mindroom.chat/tools/builtin/index.md) - Complete list of available built-in tools with configuration details
- [MCP (Planned)](https://docs.mindroom.chat/tools/mcp/index.md) - Native MCP status and plugin-based workaround
- [Plugins](https://docs.mindroom.chat/plugins/index.md) - Extend MindRoom with custom tools and skills (including MCP via plugin workaround)
