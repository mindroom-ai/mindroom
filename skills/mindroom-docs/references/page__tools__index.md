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

- **File & System** - File operations, shell, Docker, Python, SQL, databases (Postgres, Redshift, Neo4j, DuckDB), Pandas, CSV, coding, self-config, calculator, reasoning, file generation, visualization, sleep
- **Web Search & Research** - DuckDuckGo, Google Search, Baidu, Tavily, Exa, SerpAPI, Serper, SearXNG, Linkup
- **Web Scraping & Crawling** - Firecrawl, Crawl4AI, BrowserBase, AgentQL, Spider, ScrapeGraph, Apify, BrightData, Oxylabs, Jina, Website, Trafilatura, Newspaper4k, Web Browser Tools, Browser (OpenClaw)
- **AI & ML APIs** - OpenAI, Gemini, Groq, Replicate, Fal, DALL-E, Cartesia, ElevenLabs, Desi Vocal, LumaLabs, ModelsLabs
- **Knowledge & Research** - arXiv, Wikipedia, PubMed, Hacker News
- **Communication & Social** - Matrix, Gmail, Slack, Discord, Telegram, WhatsApp, Twilio, Webex, Resend, Email (SMTP), X/Twitter, Reddit, Zoom
- **Project Management** - GitHub, Bitbucket, Jira, Linear, ClickUp, Confluence, Notion, Trello, Todoist, Zendesk
- **Calendar & Scheduling** - Google Calendar, Cal.com, Scheduler
- **Data & Business** - Google Sheets, Yahoo Finance, OpenBB, Shopify, Financial Datasets API
- **Location & Maps** - Google Maps, OpenWeather
- **DevOps & Infrastructure** - AWS Lambda, AWS SES, Airflow, E2B, Daytona, Claude Agent, Composio, Google BigQuery, [container sandbox proxy](https://docs.mindroom.chat/deployment/sandbox-proxy/index.md)
- **Smart Home** - Home Assistant
- **Media & Entertainment** - YouTube, Spotify, Giphy, MoviePy, Unsplash, Brandfetch
- **Memory & Storage** - Memory, Mem0, Zep, Attachments
- **Custom & Config** - Custom API, Config Manager, Subagents, Delegate

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

## Implied Tools

Some tools automatically include companion tools via the `IMPLIED_TOOLS` mapping. When `matrix_message` is in an agent's tool list, `attachments` is automatically added. This happens during tool name expansion — the effective tool set includes both the explicitly listed tools and any implied tools.

Currently the only implied mapping is:

| Tool             | Implies       |
| ---------------- | ------------- |
| `matrix_message` | `attachments` |

This is why the `openclaw_compat` preset includes `attachments` in its effective tool set even though the preset definition only lists `matrix_message`.

## Tool Runtime Context

When a tool runs inside a Matrix-connected agent, it receives a `ToolRuntimeContext` via a context variable. This context carries the current `room_id`, `thread_id`, `requester_id`, `agent_name`, the Matrix client, the active config, and runtime paths. Tools like `matrix_message` use this context to post messages back to the correct room and thread without the caller passing explicit IDs.

Attachment IDs from the current conversation are also available in the runtime context. Tools that accept `attachment_ids` (such as `matrix_message`) resolve those IDs against the context-scoped attachment registry, preventing one conversation from accessing files uploaded in another.

## Automatic Dependency Installation

Each tool declares its Python dependencies as an optional extra in `pyproject.toml`. When an agent tries to use a tool whose dependencies aren't installed, MindRoom automatically installs them at runtime:

1. **Pre-check** — uses `importlib.util.find_spec()` to detect missing packages without importing anything
1. **Locked install** — runs `uv sync --locked --inexact --no-dev --extra <tool>` to install exact pinned versions from `uv.lock`
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
