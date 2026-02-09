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

## Tool Categories

Tools are organized by category:

- **Development** - File operations, shell, Docker, GitHub, Jira, Python, Airflow, code execution sandboxes (E2B, Daytona)
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

See the full list in:

- [Built-in Tools](https://docs.mindroom.chat/tools/builtin/index.md) - Complete list of available built-in tools with configuration details
- [Plugins](https://docs.mindroom.chat/plugins/index.md) - Extend MindRoom with custom tools and skills (including MCP via plugin workaround)
