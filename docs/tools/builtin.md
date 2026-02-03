---
icon: lucide/box
---

# Built-in Tools

MindRoom includes 80+ built-in tool integrations organized by category.

## File & System

| Tool | Description | Required Env Vars |
|------|-------------|-------------------|
| `file` | Read, write, list, and manage files | - |
| `shell` | Execute shell commands | - |
| `docker` | Manage Docker containers and images | - |
| `python` | Execute Python code | - |
| `sql` | Execute SQL queries | Database connection |
| `duckdb` | Query data with DuckDB | - |
| `pandas` | Data manipulation with Pandas | - |
| `csv` | Read and write CSV files | - |
| `calculator` | Mathematical calculations | - |
| `sleep` | Pause execution | - |

## Web Search & Research

| Tool | Description | Required Env Vars |
|------|-------------|-------------------|
| `duckduckgo` | DuckDuckGo web search | - |
| `googlesearch` | Google search | `GOOGLE_API_KEY`, `GOOGLE_CSE_ID` |
| `baidusearch` | Baidu search | - |
| `tavily` | AI-powered web research | `TAVILY_API_KEY` |
| `exa` | Neural search engine | `EXA_API_KEY` |
| `serpapi` | Search API aggregator | `SERPAPI_API_KEY` |
| `serper` | Google search API | `SERPER_API_KEY` |
| `searxng` | Self-hosted metasearch | `SEARXNG_URL` |
| `linkup` | Link discovery | `LINKUP_API_KEY` |

## Web Scraping & Crawling

| Tool | Description | Required Env Vars |
|------|-------------|-------------------|
| `firecrawl` | Web scraping and crawling | `FIRECRAWL_API_KEY` |
| `crawl4ai` | AI-powered web crawling | - |
| `browserbase` | Cloud browser automation | `BROWSERBASE_API_KEY` |
| `agentql` | Structured web scraping | `AGENTQL_API_KEY` |
| `spider` | Web spider/crawler | `SPIDER_API_KEY` |
| `scrapegraph` | Graph-based scraping | `SCRAPEGRAPH_API_KEY` |
| `apify` | Web scraping platform | `APIFY_API_KEY` |
| `brightdata` | Proxy and scraping | `BRIGHTDATA_API_KEY` |
| `oxylabs` | Web scraping proxy | `OXYLABS_API_KEY` |
| `jina` | Document processing | `JINA_API_KEY` |
| `website` | Simple web fetching | - |
| `newspaper4k` | Article extraction | - |
| `web_browser_tools` | Browser automation | - |

## AI & ML APIs

| Tool | Description | Required Env Vars |
|------|-------------|-------------------|
| `openai` | OpenAI API access | `OPENAI_API_KEY` |
| `gemini` | Google Gemini | `GOOGLE_API_KEY` |
| `groq` | Groq inference | `GROQ_API_KEY` |
| `replicate` | Run ML models | `REPLICATE_API_TOKEN` |
| `fal` | Fal.ai models | `FAL_API_KEY` |
| `dalle` | DALL-E image generation | `OPENAI_API_KEY` |
| `cartesia` | Voice synthesis | `CARTESIA_API_KEY` |
| `eleven_labs` | Text-to-speech | `ELEVEN_LABS_API_KEY` |
| `lumalabs` | Luma video generation | `LUMALABS_API_KEY` |
| `modelslabs` | Custom models | `MODELSLABS_API_KEY` |

## Knowledge & Research

| Tool | Description | Required Env Vars |
|------|-------------|-------------------|
| `arxiv` | Academic papers | - |
| `wikipedia` | Wikipedia lookups | - |
| `pubmed` | Medical literature | - |
| `hackernews` | Hacker News | - |
| `youtube` | YouTube transcripts | - |
| `reddit` | Reddit API | `REDDIT_CLIENT_ID`, `REDDIT_CLIENT_SECRET` |

## Communication

| Tool | Description | Required Env Vars |
|------|-------------|-------------------|
| `gmail` | Gmail integration | Google OAuth |
| `slack` | Slack messaging | `SLACK_BOT_TOKEN` |
| `discord` | Discord bot | `DISCORD_BOT_TOKEN` |
| `telegram` | Telegram bot | `TELEGRAM_BOT_TOKEN` |
| `whatsapp` | WhatsApp Business | `WHATSAPP_API_KEY` |
| `twilio` | SMS and voice | `TWILIO_ACCOUNT_SID`, `TWILIO_AUTH_TOKEN` |
| `webex` | Webex Teams | `WEBEX_ACCESS_TOKEN` |
| `resend` | Transactional email | `RESEND_API_KEY` |
| `email` | Generic email | SMTP config |
| `x` | X/Twitter | `X_API_KEY`, `X_API_SECRET` |

## Project Management

| Tool | Description | Required Env Vars |
|------|-------------|-------------------|
| `github` | GitHub repos, issues, PRs | `GITHUB_TOKEN` |
| `jira` | Jira issue tracking | `JIRA_URL`, `JIRA_EMAIL`, `JIRA_API_TOKEN` |
| `linear` | Linear issues | `LINEAR_API_KEY` |
| `confluence` | Confluence wikis | `CONFLUENCE_URL`, `CONFLUENCE_API_TOKEN` |
| `trello` | Trello boards | `TRELLO_API_KEY`, `TRELLO_TOKEN` |
| `todoist` | Todoist tasks | `TODOIST_API_KEY` |
| `zendesk` | Zendesk support | `ZENDESK_SUBDOMAIN`, `ZENDESK_EMAIL`, `ZENDESK_API_TOKEN` |

## Calendar & Scheduling

| Tool | Description | Required Env Vars |
|------|-------------|-------------------|
| `google_calendar` | Google Calendar | Google OAuth |
| `cal_com` | Cal.com scheduling | `CAL_COM_API_KEY` |
| `zoom` | Zoom meetings | `ZOOM_API_KEY`, `ZOOM_API_SECRET` |

## Data & Business

| Tool | Description | Required Env Vars |
|------|-------------|-------------------|
| `google_sheets` | Google Sheets | Google OAuth |
| `yfinance` | Financial data | - |
| `financial_datasets_api` | Financial datasets | `FINANCIAL_DATASETS_API_KEY` |

## Location & Maps

| Tool | Description | Required Env Vars |
|------|-------------|-------------------|
| `google_maps` | Maps and places | `GOOGLE_MAPS_API_KEY` |
| `openweather` | Weather data | `OPENWEATHER_API_KEY` |

## DevOps & Infrastructure

| Tool | Description | Required Env Vars |
|------|-------------|-------------------|
| `aws_lambda` | AWS Lambda functions | AWS credentials |
| `aws_ses` | AWS email service | AWS credentials |
| `airflow` | Apache Airflow | `AIRFLOW_URL`, `AIRFLOW_USERNAME`, `AIRFLOW_PASSWORD` |
| `e2b` | Code execution sandbox | `E2B_API_KEY` |
| `daytona` | Development environments | `DAYTONA_API_KEY` |
| `composio` | API composition | `COMPOSIO_API_KEY` |

## Smart Home

| Tool | Description | Required Env Vars |
|------|-------------|-------------------|
| `homeassistant` | Home Assistant control | `HOMEASSISTANT_URL`, `HOMEASSISTANT_TOKEN` |

## Media

| Tool | Description | Required Env Vars |
|------|-------------|-------------------|
| `giphy` | GIF search | `GIPHY_API_KEY` |
| `moviepy_video_tools` | Video processing | - |

## Memory & Storage

| Tool | Description | Required Env Vars |
|------|-------------|-------------------|
| `mem0` | Memory management | `MEM0_API_KEY` |
| `zep` | Conversation memory | `ZEP_API_KEY` |

## Custom

| Tool | Description | Required Env Vars |
|------|-------------|-------------------|
| `custom_api` | Custom API calls | Varies |
| `config_manager` | MindRoom config | - |

## Enabling Tools

Add tools to agents in `config.yaml`:

```yaml
agents:
  assistant:
    display_name: Assistant
    role: A helpful assistant
    model: sonnet
    tools:
      - file
      - shell
      - duckduckgo
      - github
```

Or use the Dashboard's Agents tab to enable tools visually.

## Environment Variables

Most tools require API keys or credentials. Set them in your `.env` file:

```bash
# Search
TAVILY_API_KEY=tvly-...
EXA_API_KEY=...

# Communication
SLACK_BOT_TOKEN=xoxb-...
GITHUB_TOKEN=ghp_...

# AI Services
OPENAI_API_KEY=sk-...
REPLICATE_API_TOKEN=r8_...
```

MindRoom automatically loads `.env` files from the working directory.
