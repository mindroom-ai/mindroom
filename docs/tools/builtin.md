---
icon: lucide/box
---

# Built-in Tools

MindRoom includes 100+ built-in tool integrations organized by category.

## File & System

| Icon | Tool | Description | Config Required |
|------|------|-------------|-----------------|
| :lucide-folder-cog: | `file` | Read, write, list, search, and manage local files | - |
| :lucide-folder-cog: | `shell` | Execute shell commands | - |
| :lucide-folder-cog: | `docker` | Manage Docker containers and images | - |
| :lucide-folder-cog: | `python` | Execute Python code | - |
| :lucide-folder-cog: | `sql` | Database query and management for SQL databases | `db_url` or `db_engine`, `user`, `password`, `host`, `port`, `schema`, `dialect` |
| :lucide-folder-cog: | `postgres` | Query PostgreSQL databases - list tables, describe schemas, run SQL | `host`, `port`, `db_name`, `user`, `password` |
| :lucide-folder-cog: | `redshift` | Query Amazon Redshift data warehouse | Connection params |
| :lucide-folder-cog: | `neo4j` | Query Neo4j graph databases with Cypher | `uri`, `user`, `password` |
| :lucide-folder-cog: | `duckdb` | Query data with DuckDB | - |
| :lucide-folder-cog: | `pandas` | Data manipulation with Pandas | - |
| :lucide-folder-cog: | `csv` | CSV file analysis and querying with SQL support | - |
| :lucide-folder-cog: | `calculator` | Mathematical calculations | - |
| :lucide-folder-cog: | `reasoning` | Step-by-step reasoning scratchpad for structured problem solving | - |
| :lucide-folder-cog: | `file_generation` | Generate JSON, CSV, PDF, and text files from data | - |
| :lucide-folder-cog: | `visualization` | Create bar, line, pie charts, scatter plots, and histograms | - |
| :lucide-folder-cog: | `coding` | Advanced code-oriented file operations (precise edits, grep, and discovery) | `base_dir` (optional) |
| :lucide-folder-cog: | `self_config` | Allow an agent to read and modify its own configuration | - |
| :lucide-folder-cog: | `sleep` | Pause execution | - |

## Web Search & Research

| Icon | Tool | Description | Config Required |
|------|------|-------------|-----------------|
| :lucide-search: | `duckduckgo` | DuckDuckGo web search | - |
| :lucide-search: | `googlesearch` | Search Google for web results using the WebSearch backend | - |
| :lucide-search: | `baidusearch` | Baidu search | - |
| :lucide-search: | `tavily` | Real-time web search API | `api_key` |
| :lucide-search: | `exa` | AI-powered web search and research | `api_key` |
| :lucide-search: | `serpapi` | Search API aggregator | `api_key` |
| :lucide-search: | `serper` | Google search API | `api_key` |
| :lucide-search: | `searxng` | Open source metasearch engine for web, images, news, and more | `host` |
| :lucide-search: | `linkup` | Web search via Linkup API for real-time information | `api_key` |

## Web Scraping & Crawling

| Icon | Tool | Description | Config Required |
|------|------|-------------|-----------------|
| :lucide-globe: | `firecrawl` | Web scraping and crawling | `api_key` |
| :lucide-globe: | `crawl4ai` | AI-powered web crawling | - |
| :lucide-globe: | `browserbase` | Cloud browser automation | `api_key` |
| :lucide-globe: | `agentql` | Structured web scraping | `api_key` |
| :lucide-globe: | `spider` | Web spider/crawler | `api_key` |
| :lucide-globe: | `scrapegraph` | Extract structured data from webpages using AI and natural language prompts | `api_key` |
| :lucide-globe: | `apify` | Web scraping platform | `api_key` |
| :lucide-globe: | `brightdata` | Proxy and scraping | `api_key` |
| :lucide-globe: | `oxylabs` | Web scraping including SERP, product data, and universal scraping | `api_key` |
| :lucide-globe: | `jina` | Web content reading and search | `api_key` (optional) |
| :lucide-globe: | `website` | Web scraping and content extraction from websites | - |
| :lucide-globe: | `trafilatura` | Web content and metadata extraction | - |
| :lucide-globe: | `newspaper4k` | Article extraction | - |
| :lucide-globe: | `browser` | OpenClaw-style browser control (status/start/stop/profiles/tabs/open/focus/close/snapshot/screenshot/navigate/console/pdf/upload/dialog/act) | `output_dir` (optional) |
| :lucide-globe: | `web_browser_tools` | Open URLs in web browser tabs or windows | - |

## AI & ML APIs

| Icon | Tool | Description | Config Required |
|------|------|-------------|-----------------|
| :lucide-sparkles: | `openai` | Transcription, image generation, and speech synthesis | `api_key` |
| :lucide-sparkles: | `gemini` | Google AI for image and video generation | `api_key` |
| :lucide-sparkles: | `groq` | Fast AI inference for audio transcription, translation, and text-to-speech | `api_key` |
| :lucide-sparkles: | `replicate` | Generate images and videos using AI models | `api_key` |
| :lucide-sparkles: | `fal` | AI media generation (images and videos) | `api_key` |
| :lucide-sparkles: | `dalle` | DALL-E image generation | `api_key` |
| :lucide-sparkles: | `cartesia` | Text-to-speech and voice localization | `api_key` |
| :lucide-sparkles: | `eleven_labs` | Text-to-speech and sound effects | `api_key` |
| :lucide-sparkles: | `desi_vocal` | Hindi and Indian language text-to-speech | `api_key` |
| :lucide-sparkles: | `lumalabs` | 3D content creation and video generation | `api_key` |
| :lucide-sparkles: | `modelslabs` | Generate videos, audio, and GIFs from text | `api_key` |

## Knowledge & Research

| Icon | Tool | Description | Config Required |
|------|------|-------------|-----------------|
| :lucide-book-open: | `arxiv` | Search and read academic papers from ArXiv | - |
| :lucide-book-open: | `wikipedia` | Search and retrieve information from Wikipedia | - |
| :lucide-book-open: | `pubmed` | Search and retrieve medical and life science literature | - |
| :lucide-book-open: | `hackernews` | Get top stories and user details from Hacker News | - |

## Communication & Social

| Icon | Tool | Description | Config Required |
|------|------|-------------|-----------------|
| :lucide-message-square: | `matrix_message` | Native Matrix messaging actions (`send`, `reply`, `thread-reply`, `react`, `read`, `thread-list`, `edit`, `context`) | - |
| :lucide-message-square: | `gmail` | Read, search, and manage Gmail emails | Google OAuth |
| :lucide-message-square: | `slack` | Send messages and manage channels | `token` |
| :lucide-message-square: | `discord` | Interact with Discord channels and servers | `bot_token` |
| :lucide-message-square: | `telegram` | Send messages via Telegram bot | `token`, `chat_id` |
| :lucide-message-square: | `whatsapp` | WhatsApp Business API messaging | `access_token`, `phone_number_id`, `version` (optional), `recipient_waid` (optional), `async_mode` (optional) |
| :lucide-message-square: | `twilio` | SMS and voice | `account_sid`, `auth_token` |
| :lucide-message-square: | `webex` | Webex Teams messaging | `access_token`, `enable_send_message` (optional), `enable_list_rooms` (optional) |
| :lucide-message-square: | `resend` | Transactional email | `api_key` |
| :lucide-message-square: | `email` | Generic SMTP email | SMTP config |
| :lucide-message-square: | `x` | Post tweets, send DMs, and search X/Twitter | `bearer_token` or OAuth (`consumer_key`, `consumer_secret`, `access_token`, `access_token_secret`); optional: `include_post_metrics`, `wait_on_rate_limit` |
| :lucide-message-square: | `reddit` | Reddit browsing and interaction | `client_id`, `client_secret` |
| :lucide-message-square: | `zoom` | Video conferencing and meetings | `account_id`, `client_id`, `client_secret` |

## Project Management

| Icon | Tool | Description | Config Required |
|------|------|-------------|-----------------|
| :lucide-kanban: | `github` | Repository and issue management | `access_token` |
| :lucide-kanban: | `bitbucket` | Bitbucket repository, PR, and issue management | `username`, `password` or `token`, `workspace`, `repo_slug` |
| :lucide-kanban: | `jira` | Issue tracking and project management | `server_url`, `username`, `password` or `token` |
| :lucide-kanban: | `linear` | Issue tracking and project management | `api_key` |
| :lucide-kanban: | `clickup` | ClickUp task, space, and list management | `api_key`, `master_space_id` |
| :lucide-kanban: | `confluence` | Retrieve, create, and update wiki pages | `url`, `username`, `password` or `api_key` |
| :lucide-kanban: | `notion` | Create, update, and search pages in Notion databases | `api_key`, `database_id` |
| :lucide-kanban: | `trello` | Trello boards | `api_key`, `api_secret`, `token` |
| :lucide-kanban: | `todoist` | Todoist task management | `api_token` |
| :lucide-kanban: | `zendesk` | Search help center articles | `username`, `password`, `company_name`, `enable_search_zendesk` (optional) |

## Calendar & Scheduling

| Icon | Tool | Description | Config Required |
|------|------|-------------|-----------------|
| :lucide-calendar: | `google_calendar` | View and schedule meetings | Google OAuth |
| :lucide-calendar: | `cal_com` | Cal.com scheduling | `api_key` |
| :lucide-calendar: | `scheduler` | Schedule, edit, list, and cancel tasks and reminders | - |

## Data & Business

| Icon | Tool | Description | Config Required |
|------|------|-------------|-----------------|
| :lucide-chart-column: | `google_sheets` | Read, create, update spreadsheets | Google OAuth |
| :lucide-chart-column: | `yfinance` | Financial data | - |
| :lucide-chart-column: | `openbb` | Stock prices, company news, price targets via OpenBB | `openbb_pat` (optional) |
| :lucide-chart-column: | `shopify` | Shopify store sales data, products, orders | `shop_name`, `access_token` |
| :lucide-chart-column: | `financial_datasets_api` | Financial datasets | `api_key` |

## Location & Maps

| Icon | Tool | Description | Config Required |
|------|------|-------------|-----------------|
| :lucide-map-pinned: | `google_maps` | Place search, directions, geocoding, and more via Google Maps | `key` |
| :lucide-map-pinned: | `openweather` | Weather data | `api_key` |

## DevOps & Infrastructure

| Icon | Tool | Description | Config Required |
|------|------|-------------|-----------------|
| :lucide-server: | `aws_lambda` | AWS Lambda functions | AWS credentials |
| :lucide-server: | `aws_ses` | AWS email service | AWS credentials |
| :lucide-server: | `airflow` | Apache Airflow DAG file management | - |
| :lucide-server: | `e2b` | Code execution sandbox | `api_key` |
| :lucide-server: | `daytona` | Development environments | `api_key` |
| :lucide-server: | `claude_agent` | Persistent Claude coding sessions with tool use and subagents | `api_key` (recommended) |
| :lucide-server: | `composio` | Access 1000+ integrations including Gmail, Salesforce, GitHub, and more | `api_key` |
| :lucide-server: | `google_bigquery` | Query Google BigQuery - list tables, schemas, run SQL | `dataset`, `project`, `location` |

## Smart Home

| Icon | Tool | Description | Config Required |
|------|------|-------------|-----------------|
| :lucide-house: | `homeassistant` | Control and monitor smart home devices | `HOMEASSISTANT_URL`, `HOMEASSISTANT_TOKEN` |

## Media & Entertainment

| Icon | Tool | Description | Config Required |
|------|------|-------------|-----------------|
| :lucide-clapperboard: | `youtube` | Extract video data, captions, and timestamps | - |
| :lucide-clapperboard: | `spotify` | Search tracks, manage playlists, get recommendations | `access_token` |
| :lucide-clapperboard: | `giphy` | GIF search | `api_key` |
| :lucide-clapperboard: | `moviepy_video_tools` | Video processing | - |
| :lucide-clapperboard: | `unsplash` | Search and retrieve royalty-free images | `access_key` |
| :lucide-clapperboard: | `brandfetch` | Retrieve brand logos, colors, and fonts by domain | `api_key` |

## Memory & Storage

| Icon | Tool | Description | Config Required |
|------|------|-------------|-----------------|
| :lucide-database: | `memory` | Explicitly store and search agent memories on demand | - |
| :lucide-database: | `mem0` | Persistent memory system | `api_key` (optional for cloud) |
| :lucide-database: | `zep` | Conversation memory | `api_key` |
| :lucide-paperclip: | `attachments` | List and register context-scoped file attachments (send via `matrix_message`) (see [Attachments](../attachments.md)) | - |

## Custom & Config

| Icon | Tool | Description | Config Required |
|------|------|-------------|-----------------|
| :lucide-sliders-horizontal: | `custom_api` | Custom API calls | Varies |
| :lucide-sliders-horizontal: | `config_manager` | Build and manage MindRoom agents with expert knowledge of the system | - |
| :lucide-workflow: | `subagents` | Spawn and communicate with sub-agent sessions | - |
| :lucide-workflow: | `delegate` | Delegate tasks to other configured agents | - |

Tool presets are config-only macros, not runtime tools.
For OpenClaw workspace portability, `openclaw_compat` expands to `shell`, `coding`, `duckduckgo`, `website`, `browser`, `scheduler`, `subagents`, `matrix_message`, and `attachments`.
`attachments` is not directly in the preset — it is implied by `matrix_message` via the `IMPLIED_TOOLS` mapping.

## Claude Agent Sessions

The `claude_agent` tool manages long-lived Claude coding sessions on the backend.
This allows iterative coding workflows in the same session (including Claude-side tool usage and subagents).

When using the OpenAI-compatible API, set `X-Session-Id` to keep tool sessions stable across requests.
See [OpenAI API Compatibility](../openai-api.md#session-continuity).

Add `claude_agent` to an agent's tools in `config.yaml`:

```yaml
agents:
  code:
    display_name: Code Agent
    role: Coding assistant with persistent Claude sessions
    model: general
    tools:
      - claude_agent
```

Configure credentials via the dashboard or by writing `mindroom_data/credentials/claude_agent_credentials.json`:

```json
{
  "api_key": "sk-ant-or-proxy-key",
  "model": "claude-sonnet-4-6",
  "permission_mode": "default",
  "continue_conversation": true,
  "session_ttl_minutes": 60,
  "max_sessions": 200
}
```

To run through an Anthropic-compatible gateway (for example LiteLLM `/v1/messages`):

```json
{
  "api_key": "sk-dummy",
  "anthropic_base_url": "http://litellm.local",
  "anthropic_auth_token": "sk-dummy",
  "disable_experimental_betas": true
}
```

Use the gateway host root for `anthropic_base_url` (no `/v1` suffix), because Claude clients append `/v1/messages`.
Some Anthropic-compatible backends may reject Claude's `anthropic-beta` headers.
Set `disable_experimental_betas` to `true` in that case.

## Worker-Routed Tools

Some tools default to running in a sandboxed worker container instead of the primary agent process.
This is controlled by the `default_execution_target` metadata field.

The following tools default to worker execution:

| Tool | Purpose |
|------|---------|
| `file` | File read/write operations |
| `shell` | Shell command execution |
| `python` | Python code execution |
| `coding` | Advanced code editing and discovery |

When a [sandbox proxy](../deployment/sandbox-proxy.md) backend is configured, calls to these tools are forwarded to an isolated container.
The worker container receives credentials via short-lived leases and inherits the agent's execution scope.

### Execution Scopes

Worker routing uses three scope levels configured via `worker_scope` on an agent:

| Scope | Isolation | Worker Key |
|-------|-----------|------------|
| `shared` | One worker per agent (all requesters share state) | `v1:<tenant>:shared:<agent>` |
| `user` | One worker per requester (state shared across agents) | `v1:<tenant>:user:<requester>` |
| `user_agent` | One worker per requester-agent pair (full isolation) | `v1:<tenant>:user_agent:<requester>:<agent>` |

### Shared-Only Integrations

Some dashboard integrations are restricted to shared or unscoped execution and cannot be used by agents with `user` or `user_agent` worker scope:

`google`, `spotify`, `homeassistant`, `gmail`, `google_calendar`, `google_sheets`

If an agent with an isolating scope tries to use one of these integrations, the tool call is rejected with an error explaining the scope restriction.

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

MindRoom still supports a config-adjacent `.env` file for provider and bootstrap settings such as model API keys:

```bash
# AI providers
OPENAI_API_KEY=sk-...
ANTHROPIC_API_KEY=sk-ant-...
GOOGLE_API_KEY=...
REPLICATE_API_TOKEN=r8_...
```

That `.env` file is resolved next to the active `config.yaml`, not from an arbitrary working directory.

Tool integrations should not rely on tool-specific env vars such as `CLICKUP_API_KEY` or `DAYTONA_API_KEY`.

Configure tools through the dashboard credentials store, persisted tool configuration, or explicit runtime overrides instead.

Execution tools do not use env vars as constructor-time configuration.
`shell` still receives the committed runtime env as explicit execution context.
`python` should not rely on in-process runtime env emulation.
If Python code needs runtime-scoped env, run it through sandbox/worker subprocess execution instead.
