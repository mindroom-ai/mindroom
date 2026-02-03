---
icon: lucide/box
---

# Built-in Tools

MindRoom includes 85+ built-in tool integrations organized by category.

## File & System

| Tool | Description | Config Required |
|------|-------------|-----------------|
| `file` | Read, write, list, search, and manage local files | - |
| `shell` | Execute shell commands | - |
| `docker` | Manage Docker containers and images | - |
| `python` | Execute Python code | - |
| `sql` | Database query and management for SQL databases | `db_url` or connection params |
| `duckdb` | Query data with DuckDB | - |
| `pandas` | Data manipulation with Pandas | - |
| `csv` | Read and write CSV files | - |
| `calculator` | Mathematical calculations | - |
| `sleep` | Pause execution | - |

## Web Search & Research

| Tool | Description | Config Required |
|------|-------------|-----------------|
| `duckduckgo` | DuckDuckGo web search | - |
| `googlesearch` | Google search via WebSearch backend | - |
| `baidusearch` | Baidu search | - |
| `tavily` | Real-time web search API | `api_key` |
| `exa` | AI-powered web search and research | `api_key` |
| `serpapi` | Search API aggregator | `api_key` |
| `serper` | Google search API | `api_key` |
| `searxng` | Self-hosted metasearch | `host` |
| `linkup` | Link discovery | `api_key` |

## Web Scraping & Crawling

| Tool | Description | Config Required |
|------|-------------|-----------------|
| `firecrawl` | Web scraping and crawling | `api_key` |
| `crawl4ai` | AI-powered web crawling | - |
| `browserbase` | Cloud browser automation | `api_key` |
| `agentql` | Structured web scraping | `api_key` |
| `spider` | Web spider/crawler | `api_key` |
| `scrapegraph` | Graph-based scraping | `api_key` |
| `apify` | Web scraping platform | `api_key` |
| `brightdata` | Proxy and scraping | `api_key` |
| `oxylabs` | Web scraping proxy | `api_key` |
| `jina` | Web content reading and search | `api_key` (optional) |
| `website` | Simple web fetching | - |
| `newspaper4k` | Article extraction | - |
| `web_browser_tools` | Browser automation | - |

## AI & ML APIs

| Tool | Description | Config Required |
|------|-------------|-----------------|
| `openai` | Transcription, image generation, and speech synthesis | `api_key` |
| `gemini` | Google AI for image and video generation | `api_key` |
| `groq` | Audio transcription, translation, and text-to-speech | `api_key` |
| `replicate` | Generate images and videos using AI models | `api_key` |
| `fal` | AI media generation (images and videos) | `api_key` |
| `dalle` | DALL-E image generation | `api_key` |
| `cartesia` | Text-to-speech and voice localization | `api_key` |
| `eleven_labs` | Text-to-speech and sound effects | `api_key` |
| `lumalabs` | 3D content creation and video generation | `api_key` |
| `modelslabs` | Generate videos, audio, and GIFs from text | `api_key` |

## Knowledge & Research

| Tool | Description | Config Required |
|------|-------------|-----------------|
| `arxiv` | Search and read academic papers from ArXiv | - |
| `wikipedia` | Search and retrieve information from Wikipedia | - |
| `pubmed` | Search and retrieve medical and life science literature | - |
| `hackernews` | Get top stories and user details from Hacker News | - |

## Communication & Social

| Tool | Description | Config Required |
|------|-------------|-----------------|
| `gmail` | Read, search, and manage Gmail emails | Google OAuth |
| `slack` | Send messages and manage channels | `token` |
| `discord` | Interact with Discord channels and servers | `bot_token` |
| `telegram` | Send messages via Telegram bot | `token`, `chat_id` |
| `whatsapp` | WhatsApp Business API messaging | `access_token`, `phone_number_id` |
| `twilio` | SMS and voice | `account_sid`, `auth_token` |
| `webex` | Webex Teams messaging | `access_token` |
| `resend` | Transactional email | `api_key` |
| `email` | Generic SMTP email | SMTP config |
| `x` | X/Twitter posting and DMs | `bearer_token` or OAuth credentials |
| `reddit` | Reddit browsing and interaction | `client_id`, `client_secret` |
| `zoom` | Video conferencing and meetings | `account_id`, `client_id`, `client_secret` |

## Project Management

| Tool | Description | Config Required |
|------|-------------|-----------------|
| `github` | Repository and issue management | `access_token` |
| `jira` | Issue tracking and project management | `server_url`, `username`, `password` or `token` |
| `linear` | Issue tracking and project management | `api_key` |
| `confluence` | Atlassian wiki pages | `url`, `username`, `password` or `api_key` |
| `trello` | Trello boards | `api_key`, `token` |
| `todoist` | Todoist task management | `api_token` |
| `zendesk` | Search help center articles | `username`, `password`, `company_name` |

## Calendar & Scheduling

| Tool | Description | Config Required |
|------|-------------|-----------------|
| `google_calendar` | View and schedule meetings | Google OAuth |
| `cal_com` | Cal.com scheduling | `api_key` |

## Data & Business

| Tool | Description | Config Required |
|------|-------------|-----------------|
| `google_sheets` | Read, create, update spreadsheets | Google OAuth |
| `yfinance` | Financial data | - |
| `financial_datasets_api` | Financial datasets | `api_key` |

## Location & Maps

| Tool | Description | Config Required |
|------|-------------|-----------------|
| `google_maps` | Maps and places | `api_key` |
| `openweather` | Weather data | `api_key` |

## DevOps & Infrastructure

| Tool | Description | Config Required |
|------|-------------|-----------------|
| `aws_lambda` | AWS Lambda functions | AWS credentials |
| `aws_ses` | AWS email service | AWS credentials |
| `airflow` | Apache Airflow DAG file management | - |
| `e2b` | Code execution sandbox | `api_key` |
| `daytona` | Development environments | `api_key` |
| `composio` | API composition | `api_key` |

## Smart Home

| Tool | Description | Config Required |
|------|-------------|-----------------|
| `homeassistant` | Control and monitor smart home devices | `HOMEASSISTANT_URL`, `HOMEASSISTANT_TOKEN` |

## Media & Entertainment

| Tool | Description | Config Required |
|------|-------------|-----------------|
| `youtube` | Extract video data, captions, and timestamps | - |
| `giphy` | GIF search | `api_key` |
| `moviepy_video_tools` | Video processing | - |

## Memory & Storage

| Tool | Description | Config Required |
|------|-------------|-----------------|
| `mem0` | Persistent memory system | `api_key` (optional for cloud) |
| `zep` | Conversation memory | `api_key` |

## Custom & Config

| Tool | Description | Config Required |
|------|-------------|-----------------|
| `custom_api` | Custom API calls | Varies |
| `config_manager` | MindRoom configuration management | - |

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
