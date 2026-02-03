---
icon: lucide/wrench
---

# Tools

MindRoom includes 85+ tool integrations that agents can use to interact with external services.

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

## Tool Categories

Tools are organized by category:

- **File & System** - File operations, shell, Docker, Python, SQL, DuckDB, Pandas, CSV
- **Web Search & Research** - DuckDuckGo, Tavily, Exa, SerpAPI, Serper, SearXNG
- **Web Scraping** - Firecrawl, Crawl4AI, Browserbase, Apify, Spider, Jina
- **AI & ML APIs** - OpenAI, Gemini, Replicate, DALL-E, Eleven Labs, Cartesia
- **Knowledge & Research** - arXiv, Wikipedia, PubMed, Hacker News, YouTube, Reddit
- **Communication** - Gmail, Slack, Discord, Telegram, Twilio, WhatsApp, Webex, X/Twitter
- **Project Management** - GitHub, Jira, Linear, Confluence, Trello, Todoist, Zendesk
- **Calendar & Scheduling** - Google Calendar, Cal.com, Zoom
- **Data & Business** - Google Sheets, YFinance, Financial Datasets API
- **Location & Maps** - Google Maps, OpenWeather
- **DevOps & Infrastructure** - AWS Lambda, AWS SES, Airflow, E2B, Daytona
- **Smart Home** - Home Assistant
- **Media** - Giphy, MoviePy video tools
- **Memory & Storage** - Mem0, Zep
- **Custom & Config** - Custom API calls, MindRoom configuration management

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

See the full list in:

- [Built-in Tools](builtin.md) - Complete list of 85+ available tools with configuration details
- [MCP Tools](mcp.md) - Model Context Protocol tools (planned)
