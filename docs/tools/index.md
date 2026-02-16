---
icon: lucide/wrench
---

# Tools

MindRoom includes 100+ tool integrations that agents can use to interact with external services.

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
      - duckduckgo
```

You can also assign tools to all agents globally:

```yaml
defaults:
  tools:
    - scheduler
```

`defaults.tools` are merged into each agent's own `tools` list with duplicates removed. Set `defaults.tools: []` to disable global default tools.

## Tool Categories

Tools are organized by category:

- **Development** - File operations, shell, Docker, GitHub, Jira, Python, Airflow, code execution sandboxes (E2B, Daytona, or MindRoom's built-in [container sandbox proxy](../deployment/sandbox-proxy.md)), Claude Agent SDK
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

```yaml
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

```yaml
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

```yaml
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

Each tool declares its Python dependencies as an optional extra in `pyproject.toml`.
When an agent tries to use a tool whose dependencies aren't installed, MindRoom automatically installs them at runtime:

1. **Pre-check** — uses `importlib.util.find_spec()` to detect missing packages without importing anything
2. **Locked install** — runs `uv sync --locked --inexact --extra <tool>` to install exact pinned versions from `uv.lock`
3. **Fallback** — if no lockfile is available, falls back to `uv pip install` or `pip install`

This means you don't need to install all 100+ tool dependencies upfront — only the tools your agents actually use get installed.

To disable auto-install, set the environment variable:

```bash
MINDROOM_NO_AUTO_INSTALL_TOOLS=1
```

To pre-install specific tool dependencies:

```bash
uv sync --extra gmail --extra slack --extra github
```

See the full list in:

- [Built-in Tools](builtin.md) - Complete list of available built-in tools with configuration details
- [Plugins](../plugins.md) - Extend MindRoom with custom tools and skills (including MCP via plugin workaround)
