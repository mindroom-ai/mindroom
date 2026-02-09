# MindRoom Source Map

Table of contents for all Python source files in `src/mindroom/`.

| File | Description |
|------|-------------|
| `src/mindroom/__init__.py` | MindRoom: A universal interface for AI agents with persistent memory. |
| `src/mindroom/agent_prompts.py` | Rich prompts for agents - like prompts.py but for agents instead of tools. |
| `src/mindroom/agents.py` | Agent loader that reads agent configurations from YAML file. |
| `src/mindroom/ai.py` | AI integration module for MindRoom agents and memory management. |
| `src/mindroom/api/__init__.py` | Backend initialization for the widget module. |
| `src/mindroom/api/credentials.py` | Unified credentials management API. |
| `src/mindroom/api/google_integration.py` | Unified Google Integration for MindRoom. |
| `src/mindroom/api/google_tools_helper.py` | Helper utilities for Google tools management. |
| `src/mindroom/api/homeassistant_integration.py` | Home Assistant Integration for MindRoom. |
| `src/mindroom/api/integrations.py` | Third-party service integrations API. |
| `src/mindroom/api/knowledge.py` | Knowledge base management API. |
| `src/mindroom/api/main.py` | - |
| `src/mindroom/api/matrix_operations.py` | API endpoints for Matrix operations. |
| `src/mindroom/api/schedules.py` | API endpoints for scheduled task management. |
| `src/mindroom/api/skills.py` | API endpoints for skill inspection and editing. |
| `src/mindroom/api/tools.py` | API endpoints for tools information. |
| `src/mindroom/background_tasks.py` | Background task management for non-blocking operations. |
| `src/mindroom/bot.py` | Multi-agent bot implementation where each agent has its own Matrix user account. |
| `src/mindroom/cli.py` | Mindroom CLI - Simplified multi-agent Matrix bot system. |
| `src/mindroom/commands.py` | Command parsing and handling for user commands. |
| `src/mindroom/config.py` | Pydantic models for configuration. |
| `src/mindroom/config_commands.py` | Configuration command handling for user-driven config changes. |
| `src/mindroom/config_confirmation.py` | Configuration change confirmation system using Matrix reactions with persistence. |
| `src/mindroom/constants.py` | Shared constants for the mindroom package. |
| `src/mindroom/credentials.py` | Unified credentials management for MindRoom. |
| `src/mindroom/credentials_sync.py` | Sync API keys from environment variables to CredentialsManager. |
| `src/mindroom/custom_tools/__init__.py` | MindRoom custom tools package. |
| `src/mindroom/custom_tools/config_manager.py` | Consolidated ConfigManager tool for building and managing MindRoom agents. |
| `src/mindroom/custom_tools/gmail.py` | Custom Gmail Tools wrapper for MindRoom. |
| `src/mindroom/custom_tools/google_calendar.py` | Custom Google Calendar Tools wrapper for MindRoom. |
| `src/mindroom/custom_tools/google_sheets.py` | Custom Google Sheets Tools wrapper for MindRoom. |
| `src/mindroom/custom_tools/homeassistant.py` | Home Assistant tools for MindRoom agents. |
| `src/mindroom/custom_tools/memory.py` | Explicit memory tools for MindRoom agents. |
| `src/mindroom/custom_tools/scheduler.py` | Scheduler tool that reuses the same backend as `!schedule`. |
| `src/mindroom/error_handling.py` | Simple error handling for MindRoom agents. |
| `src/mindroom/file_watcher.py` | Simple file watcher utility without external dependencies. |
| `src/mindroom/interactive.py` | Interactive Q&A system using Matrix reactions as clickable buttons. |
| `src/mindroom/knowledge.py` | Knowledge base management for file-backed RAG. |
| `src/mindroom/logging_config.py` | Logging configuration for mindroom using structlog. |
| `src/mindroom/matrix/__init__.py` | Matrix operations module for mindroom. |
| `src/mindroom/matrix/client.py` | Matrix client operations and utilities. |
| `src/mindroom/matrix/event_info.py` | Comprehensive event relation analysis for Matrix events. |
| `src/mindroom/matrix/identity.py` | Unified Matrix ID handling system. |
| `src/mindroom/matrix/large_messages.py` | Handle large Matrix messages that exceed the 64KB event limit. |
| `src/mindroom/matrix/mentions.py` | Matrix mention utilities. |
| `src/mindroom/matrix/message_builder.py` | Matrix message content builder with proper threading support. |
| `src/mindroom/matrix/message_content.py` | Centralized message content extraction with large message support. |
| `src/mindroom/matrix/presence.py` | Matrix presence and status message utilities. |
| `src/mindroom/matrix/rooms.py` | Matrix room management functions. |
| `src/mindroom/matrix/state.py` | Pydantic models for Matrix state. |
| `src/mindroom/matrix/typing.py` | Typing indicator management for Matrix agents. |
| `src/mindroom/matrix/users.py` | Matrix user account management for agents. |
| `src/mindroom/memory/__init__.py` | Memory management for mindroom agents and rooms. |
| `src/mindroom/memory/config.py` | Memory configuration and setup. |
| `src/mindroom/memory/functions.py` | Simple memory management functions following Mem0 patterns. |
| `src/mindroom/plugins.py` | Plugin loader for Mindroom tools and skills. |
| `src/mindroom/response_tracker.py` | Track which messages have been responded to by agents. |
| `src/mindroom/room_cleanup.py` | Room cleanup utilities for removing stale bot memberships from Matrix rooms. |
| `src/mindroom/routing.py` | Simple AI routing for multi-agent threads. |
| `src/mindroom/scheduling.py` | Scheduled task management with AI-powered workflow scheduling. |
| `src/mindroom/scheduling_context.py` | Runtime context for the scheduler tool. |
| `src/mindroom/skills.py` | Skill integration built on Agno skills with OpenClaw-compatible metadata. |
| `src/mindroom/stop.py` | Minimal stop button functionality for the bot. |
| `src/mindroom/streaming.py` | Streaming response implementation for real-time message updates. |
| `src/mindroom/teams.py` | Team-based collaboration for multiple agents. |
| `src/mindroom/thread_utils.py` | Utilities for thread analysis and agent detection. |
| `src/mindroom/tool_events.py` | Tool-event formatting and metadata helpers for Matrix messages. |
| `src/mindroom/tools/__init__.py` | Tools registry for all available Agno tools. |
| `src/mindroom/tools/agentql.py` | AgentQL tool configuration. |
| `src/mindroom/tools/airflow.py` | Airflow tool configuration. |
| `src/mindroom/tools/apify.py` | Apify tool configuration. |
| `src/mindroom/tools/arxiv.py` | ArXiv tool configuration. |
| `src/mindroom/tools/aws_lambda.py` | AWS Lambda tool configuration. |
| `src/mindroom/tools/aws_ses.py` | AWS SES tool configuration. |
| `src/mindroom/tools/baidusearch.py` | BaiduSearch tool configuration. |
| `src/mindroom/tools/bitbucket.py` | Bitbucket tool configuration. |
| `src/mindroom/tools/brandfetch.py` | Brandfetch tool configuration. |
| `src/mindroom/tools/bravesearch.py` | Brave Search tool configuration. |
| `src/mindroom/tools/brightdata.py` | BrightData tool configuration. |
| `src/mindroom/tools/browserbase.py` | Browserbase tool configuration. |
| `src/mindroom/tools/cal_com.py` | Cal.com tool configuration. |
| `src/mindroom/tools/calculator.py` | Calculator tool configuration. |
| `src/mindroom/tools/cartesia.py` | Cartesia tool configuration. |
| `src/mindroom/tools/clickup.py` | ClickUp tool configuration. |
| `src/mindroom/tools/composio.py` | Composio tool configuration. |
| `src/mindroom/tools/config_manager.py` | Config Manager tool configuration. |
| `src/mindroom/tools/confluence.py` | Confluence tool configuration. |
| `src/mindroom/tools/crawl4ai.py` | Crawl4AI tool configuration. |
| `src/mindroom/tools/csv.py` | CSV toolkit tool configuration. |
| `src/mindroom/tools/custom_api.py` | Custom API tool configuration. |
| `src/mindroom/tools/dalle.py` | DALL-E tool configuration. |
| `src/mindroom/tools/daytona.py` | Daytona tool configuration. |
| `src/mindroom/tools/desi_vocal.py` | DesiVocal tool configuration. |
| `src/mindroom/tools/discord.py` | Discord tool configuration. |
| `src/mindroom/tools/docker.py` | Docker tool configuration. |
| `src/mindroom/tools/duckdb.py` | DuckDB tool configuration. |
| `src/mindroom/tools/duckduckgo.py` | DuckDuckGo tool configuration. |
| `src/mindroom/tools/e2b.py` | E2B code execution tool configuration. |
| `src/mindroom/tools/eleven_labs.py` | Eleven Labs tool configuration. |
| `src/mindroom/tools/email.py` | Email tool configuration. |
| `src/mindroom/tools/exa.py` | Exa tool configuration. |
| `src/mindroom/tools/fal.py` | Fal tool configuration. |
| `src/mindroom/tools/file.py` | File tool configuration. |
| `src/mindroom/tools/file_generation.py` | File generation tool configuration. |
| `src/mindroom/tools/financial_datasets_api.py` | Financial Datasets API tool configuration. |
| `src/mindroom/tools/firecrawl.py` | Firecrawl tool configuration. |
| `src/mindroom/tools/gemini.py` | Gemini tool configuration. |
| `src/mindroom/tools/giphy.py` | Giphy tool configuration. |
| `src/mindroom/tools/github.py` | Tools registry for all available Agno tools. |
| `src/mindroom/tools/gmail.py` | Gmail tool configuration. |
| `src/mindroom/tools/google_bigquery.py` | Google BigQuery tool configuration. |
| `src/mindroom/tools/google_calendar.py` | Google Calendar tool configuration. |
| `src/mindroom/tools/google_maps.py` | Google Maps tool configuration. |
| `src/mindroom/tools/google_sheets.py` | Google Sheets tool configuration. |
| `src/mindroom/tools/googlesearch.py` | Google Search tool configuration. |
| `src/mindroom/tools/groq.py` | Groq tool configuration. |
| `src/mindroom/tools/hackernews.py` | Hacker News tool configuration. |
| `src/mindroom/tools/jina.py` | Jina Reader tool configuration. |
| `src/mindroom/tools/jira.py` | Jira tool configuration. |
| `src/mindroom/tools/linear.py` | Linear tool configuration. |
| `src/mindroom/tools/linkup.py` | Linkup tool configuration. |
| `src/mindroom/tools/lumalabs.py` | Luma Labs tool configuration. |
| `src/mindroom/tools/mem0.py` | Mem0 Memory tool configuration. |
| `src/mindroom/tools/memory.py` | Memory tool metadata registration. |
| `src/mindroom/tools/modelslabs.py` | ModelsLabs tool configuration. |
| `src/mindroom/tools/moviepy_video_tools.py` | MoviePy Video Tools configuration. |
| `src/mindroom/tools/neo4j.py` | Neo4j tool configuration. |
| `src/mindroom/tools/newspaper4k.py` | Newspaper4k tool configuration. |
| `src/mindroom/tools/notion.py` | Notion tool configuration. |
| `src/mindroom/tools/openai.py` | OpenAI tool configuration. |
| `src/mindroom/tools/openbb.py` | OpenBB tool configuration. |
| `src/mindroom/tools/openweather.py` | OpenWeather tool configuration. |
| `src/mindroom/tools/oxylabs.py` | Oxylabs tool configuration. |
| `src/mindroom/tools/pandas.py` | Pandas tools configuration. |
| `src/mindroom/tools/postgres.py` | PostgreSQL tool configuration. |
| `src/mindroom/tools/pubmed.py` | PubMed tool configuration. |
| `src/mindroom/tools/python.py` | Python tools configuration. |
| `src/mindroom/tools/reasoning.py` | Reasoning tool configuration. |
| `src/mindroom/tools/reddit.py` | Reddit tool configuration. |
| `src/mindroom/tools/redshift.py` | Amazon Redshift tool configuration. |
| `src/mindroom/tools/replicate.py` | Replicate tool configuration. |
| `src/mindroom/tools/resend.py` | Resend email tool configuration. |
| `src/mindroom/tools/scheduler.py` | Scheduler tool configuration. |
| `src/mindroom/tools/scrapegraph.py` | ScrapeGraph tool configuration. |
| `src/mindroom/tools/searxng.py` | Searxng tool configuration. |
| `src/mindroom/tools/serpapi.py` | SerpApi tool configuration. |
| `src/mindroom/tools/serper.py` | Serper tool configuration. |
| `src/mindroom/tools/shell.py` | Shell tool configuration. |
| `src/mindroom/tools/shopify.py` | Shopify tool configuration. |
| `src/mindroom/tools/slack.py` | Slack tool configuration. |
| `src/mindroom/tools/sleep.py` | Sleep tool configuration. |
| `src/mindroom/tools/spider.py` | Spider tool configuration. |
| `src/mindroom/tools/spotify.py` | Spotify tool configuration. |
| `src/mindroom/tools/sql.py` | SQL tool configuration. |
| `src/mindroom/tools/tavily.py` | Tavily tool configuration. |
| `src/mindroom/tools/telegram.py` | Telegram tool configuration. |
| `src/mindroom/tools/todoist.py` | Todoist tool configuration. |
| `src/mindroom/tools/trafilatura.py` | Trafilatura tool configuration. |
| `src/mindroom/tools/trello.py` | Trello tool configuration. |
| `src/mindroom/tools/twilio.py` | Twilio tool configuration. |
| `src/mindroom/tools/unsplash.py` | Unsplash tool configuration. |
| `src/mindroom/tools/visualization.py` | Visualization tool configuration. |
| `src/mindroom/tools/web_browser_tools.py` | Web Browser Tools configuration. |
| `src/mindroom/tools/webex.py` | Webex tool configuration. |
| `src/mindroom/tools/website.py` | Website tools configuration. |
| `src/mindroom/tools/whatsapp.py` | WhatsApp tool configuration. |
| `src/mindroom/tools/wikipedia.py` | Wikipedia tool configuration. |
| `src/mindroom/tools/x.py` | X (Twitter) tool configuration. |
| `src/mindroom/tools/yfinance.py` | Yahoo Finance tool configuration. |
| `src/mindroom/tools/youtube.py` | YouTube tool configuration. |
| `src/mindroom/tools/zendesk.py` | Zendesk tool configuration. |
| `src/mindroom/tools/zep.py` | Zep memory system tool configuration. |
| `src/mindroom/tools/zoom.py` | Zoom tool configuration. |
| `src/mindroom/tools_metadata.py` | Tool metadata and enhanced registration system. |
| `src/mindroom/topic_generator.py` | Generate contextual topics for Matrix rooms using AI. |
| `src/mindroom/voice_handler.py` | Voice message handler with speech-to-text and intelligent command recognition. |

**Total: 176 files**
