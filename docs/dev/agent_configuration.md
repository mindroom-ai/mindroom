# Agent Configuration Guide

MindRoom uses a YAML-based configuration system that makes it easy to customize agents for your specific needs.
You can create agents by editing `config.yaml`, or generate a starter config with `mindroom config init`.

## Configuration File

The default configuration file is `config.yaml`.
MindRoom searches for it in this order: `MINDROOM_CONFIG_PATH` env var, `./config.yaml`, `~/.mindroom/config.yaml`.
You can generate a starter config with `mindroom config init`.

## Configuration Structure

The configuration file has these top-level sections:

1. **agents** - Configure individual agents and their capabilities
2. **teams** - Multi-agent collaboration groups
3. **cultures** - Shared principles and practices applied to groups of agents
4. **models** - Define available AI models and their providers
5. **defaults** - Default settings inherited by all agents
6. **memory** - Memory system configuration (mem0 or file-backed)
7. **knowledge_bases** - File-backed RAG knowledge bases
8. **router** - Agent routing system configuration
9. **voice** - Voice message processing (STT + command intelligence)
10. **authorization** - Fine-grained user and room permissions
11. **matrix_room_access** - Managed room access mode and discoverability
12. **matrix_space** - Optional root Matrix Space for grouping rooms
13. **mindroom_user** - Internal MindRoom user account settings
14. **timezone** - Timezone for scheduled tasks (default: `UTC`)
15. **bot_accounts** - Non-MindRoom bot Matrix user IDs (e.g., bridge bots)
16. **room_models** - Per-room model overrides
17. **plugins** - Plugin paths for tool/skill extensions

## Model Configuration

Before configuring agents, you need to define which AI models are available.
MindRoom supports multiple model providers:

```yaml
models:
  default:  # Default model used when agent doesn't specify one
    provider: "ollama"
    id: "devstral:24b"

  anthropic:
    provider: "anthropic"
    id: "claude-haiku-4-5"

  ollama:
    provider: "ollama"
    id: "devstral:24b"
    # For ollama, you can add:
    # host: "http://localhost:11434"

  openrouter:
    provider: "openrouter"
    id: "anthropic/claude-sonnet-4-6"
```

Each model entry supports these fields:
- **provider** (required) - Provider name (see list below)
- **id** (required) - Model ID specific to the provider
- **host** - Optional host URL (e.g., for Ollama or OpenAI-compatible servers)
- **api_key** - Optional API key (usually set via env vars instead)
- **extra_kwargs** - Additional provider-specific parameters (e.g., `base_url`)
- **context_window** - Context window size in tokens; when set, history is dynamically reduced toward an 80% target

### Supported Providers

- **anthropic** - Claude models (requires `ANTHROPIC_API_KEY`)
- **openai** - OpenAI and OpenAI-compatible models (requires `OPENAI_API_KEY`)
- **ollama** - Local models via Ollama (requires `OLLAMA_HOST`, defaults to `http://localhost:11434`)
- **openrouter** - Access multiple models through OpenRouter (requires `OPENROUTER_API_KEY`)
- **gemini** / **google** - Google Gemini models (requires `GOOGLE_API_KEY`)
- **vertexai_claude** - Claude models via Vertex AI (requires GCP credentials)
- **groq** - Groq-hosted models (requires `GROQ_API_KEY`)
- **deepseek** - DeepSeek models (requires `DEEPSEEK_API_KEY`)
- **cerebras** - Cerebras-hosted models (requires `CEREBRAS_API_KEY`)

## Memory Configuration

The memory system helps agents remember and retrieve relevant information:

```yaml
memory:
  backend: "mem0"  # Global default backend: "mem0" or "file"
  team_reads_member_memory: false  # Allow team reads to access member agent memories
  embedder:
    provider: "ollama"  # Options: openai, ollama, huggingface, sentence_transformers, etc.
    config:
      model: "nomic-embed-text"  # Embedding model to use
      host: "http://localhost:11434"  # Ollama host URL
  file:
    max_entrypoint_lines: 200  # Max lines preloaded from MEMORY.md
  auto_flush:
    enabled: false  # Background file-memory auto-flush (see memory consolidation plan)
    flush_interval_seconds: 1800
```

You can override the memory backend per agent with `memory_backend`.
When an agent uses `memory_backend: file`, its file memory lives in the canonical workspace root.
Use `provider: "sentence_transformers"` to run embeddings locally inside MindRoom with the optional `sentence-transformers` package.

## Router Configuration

The router determines which agent should handle a user's request:

```yaml
router:
  model: "default"  # Which model to use for routing decisions (references models section)
```

## Agent Configuration Structure

Each agent in the YAML file follows this structure:

```yaml
agents:
  agent_name:
    display_name: "Human-readable name"
    role: "What the agent does"
    tools:
      - tool_name_1
      - tool_name_2
    include_default_tools: true  # Optional: merge defaults.tools into this agent's tools
    skills:
      - skill_name_1
    instructions:
      - "Specific behavior instruction 1"
      - "Specific behavior instruction 2"
    rooms:
      - lobby
      - dev
    learning: true  # Optional: enable Agno Learning (defaults to true)
    learning_mode: "always"  # Optional: "always" or "agentic"
    memory_backend: "file"  # Optional: per-agent override ("mem0" or "file")
    knowledge_bases:
      - docs
    context_files:
      - SOUL.md
      - USER.md
    model: "anthropic"  # Optional: specific model for this agent (overrides default)
    thread_mode: "thread"  # Optional: "thread" or "room"
    delegate_to: [other_agent]  # Optional: agents this one can delegate to
```

### Configuration Fields

- **agent_name**: The identifier used for the Matrix account (becomes `@mindroom_<agent_name>:<server>`)
- **display_name**: A friendly name shown in conversations
- **role**: A brief description of the agent's purpose
- **tools**: List of tools the agent can use (see Available Tools below)
- **include_default_tools**: Whether to merge `defaults.tools` into this agent's `tools` (default: true)
- **skills**: Skill names the agent can use
- **instructions**: Specific guidelines for the agent's behavior
- **rooms**: List of room aliases where this agent should be active
- **learning**: Enable Agno Learning for this agent (default: true)
- **learning_mode**: Learning mode (`always` or `agentic`, default: `always`)
- **memory_backend**: Optional per-agent memory backend override (`mem0` or `file`), inherits from `memory.backend` when omitted
- **knowledge_bases**: List of configured knowledge base IDs assigned to this agent
- **context_files**: File paths relative to the agent's canonical workspace root (`<storage_root>/agents/<name>/workspace/`) loaded into each agent instance; edits take effect on the next reply without restarting
- **model**: (Optional) Specific model to use for this agent, overrides the default model
- **allow_self_config**: (Optional) When `true`, gives the agent a scoped tool to read and modify its own configuration at runtime (default: inherits from `defaults.allow_self_config`, which defaults to `false`)
- **thread_mode**: Conversation threading mode: `thread` (default) creates Matrix threads per conversation, `room` uses a single continuous conversation per room (ideal for bridges/mobile)
- **room_thread_modes**: Per-room thread mode overrides keyed by room alias/name or Matrix room ID
- **num_history_runs**: Number of prior Agno runs to include as history context (per-agent override)
- **num_history_messages**: Max messages from history (mutually exclusive with `num_history_runs`)
- **compress_tool_results**: Compress tool results in history to save context (per-agent override)
- **enable_session_summaries**: Enable Agno session summaries for conversation compaction (per-agent override)
- **max_tool_calls_from_history**: Max tool call messages replayed from history (per-agent override)
- **show_tool_calls**: Whether to show tool call details inline in responses (per-agent override)
- **worker_tools**: Tool names to route through scoped workers (overrides defaults; `null` uses the built-in default routing policy)
- **worker_scope**: Worker runtime reuse mode for routed tools: `shared`, `user`, or `user_agent`
- **delegate_to**: List of agent names this agent can delegate tasks to via tool calls
- **private**: Optional requester-private state config for per-requester materialized instances

### File-Based Context Loading

`context_files` is useful for OpenClaw-style workspace context:

- Paths are relative to the agent's canonical workspace root (`<storage_root>/agents/<name>/workspace/`)
- `context_files` are injected in listed order
- Content is refreshed for each freshly built agent instance, so normal replies pick up edits on the next request

## Teams Configuration

Teams let multiple agents collaborate on requests:

```yaml
teams:
  research_team:
    display_name: "Research Team"
    role: "Collaborative research assistant"
    agents: [research, code]
    mode: coordinate  # "coordinate" or "collaborate"
    model: "default"  # Optional model override
    rooms:
      - lobby
```

- **coordinate**: A lead agent orchestrates the others
- **collaborate**: All members respond in parallel with a consensus summary

## Cultures Configuration

Cultures define shared principles applied to groups of agents:

```yaml
cultures:
  engineering:
    description: "Follow clean code principles and write tests"
    agents: [code, data_analyst]
    mode: automatic  # "automatic", "agentic", or "manual"
```

## Knowledge Bases Configuration

Knowledge bases provide file-backed RAG context to agents:

```yaml
knowledge_bases:
  engineering_docs:
    path: ./knowledge_docs  # Path to documents folder
    watch: true  # Watch for file changes
    chunk_size: 5000  # Characters per indexed chunk
    chunk_overlap: 0  # Overlap between adjacent chunks
    git:  # Optional: sync from a Git repository
      repo_url: "https://github.com/org/docs.git"
      branch: main
      poll_interval_seconds: 300
```

Assign knowledge bases to agents via `knowledge_bases: [engineering_docs]` in the agent config.

## Voice Configuration

Enable voice message processing with speech-to-text:

```yaml
voice:
  enabled: false
  visible_router_echo: false  # Post transcript as visible router message
  stt:
    provider: openai
    model: whisper-1
  intelligence:
    model: default  # Model for command recognition
```

## Authorization Configuration

Fine-grained access control for rooms and agents:

```yaml
authorization:
  default_room_access: false
  global_users:
    - "@owner:example.com"
  room_permissions:
    dev: ["@developer:example.com"]
  aliases:
    "@alice:example.com": ["@telegram_123:example.com"]
  agent_reply_permissions:
    "*":
      - "@owner:example.com"
```

- **global_users**: Users with access to all rooms
- **room_permissions**: Per-room user allowlists
- **aliases**: Map canonical Matrix user IDs to bridge aliases
- **agent_reply_permissions**: Per-agent/team reply allowlists (`*` key applies to all entities)

## Matrix Room Access Configuration

Control how managed rooms are created and accessed:

```yaml
matrix_room_access:
  mode: single_user_private  # "single_user_private" or "multi_user"
  multi_user_join_rule: public  # "public" or "knock" (for multi_user mode)
  publish_to_room_directory: false
  invite_only_rooms: []  # Room keys that stay invite-only even in multi_user mode
  reconcile_existing_rooms: false  # Reconcile existing rooms on startup
```

## Defaults Configuration

Default settings inherited by all agents unless overridden:

```yaml
defaults:
  tools: [scheduler]  # Tools added to every agent
  markdown: true
  enable_streaming: true
  show_stop_button: true
  learning: true
  learning_mode: "always"  # "always" or "agentic"
  compress_tool_results: true
  enable_session_summaries: false
  show_tool_calls: true
  allow_self_config: false
  max_preload_chars: 50000  # Hard cap for context_files preload
  # num_history_runs: null  # Default: all
  # num_history_messages: null  # Mutually exclusive with num_history_runs
  # max_tool_calls_from_history: null  # Default: no limit
  # worker_tools: null  # Default: use built-in routing policy
  # worker_scope: null  # Default: no worker scoping
```

## Room Configuration

Agents can be assigned to specific rooms in your Matrix server.
This allows you to create topic-specific rooms, control agent access, and organize assistants by domain.

### How Rooms Work

1. **Room Aliases**: In `config.yaml`, you specify simple room aliases like `lobby`, `dev`, `research`
2. **Automatic Creation**: When you run `mindroom run`, it automatically creates any missing rooms
3. **Agent Assignment**: Agents are automatically invited to their configured rooms
4. **Room Persistence**: Room information is stored in `matrix_state.yaml` (auto-generated)

### Example Room Setup

```yaml
agents:
  code:
    display_name: "CodeAgent"
    role: "Programming assistant"
    tools: [file, shell]
    rooms:
      - lobby      # Available in main lobby
      - dev        # Available in development room

  research:
    display_name: "ResearchAgent"
    role: "Information gathering"
    tools: [duckduckgo, wikipedia, arxiv]
    rooms:
      - lobby      # Available in main lobby
      - research   # Available in research room
```

## Available Tools

Tools give agents the ability to perform specific actions.
MindRoom includes 100+ tools; the full list is available in the dashboard.
Below is a representative selection:

### Basic Tools
- **calculator** - Perform mathematical calculations
- **file** - Read, write, and manage files
- **shell** - Execute command line operations
- **python** - Run Python code snippets
- **coding** - Code generation and editing

### Data & Analysis Tools
- **csv** - Process and analyze CSV files
- **pandas** - Advanced data manipulation and analysis
- **yfinance** - Fetch financial market data
- **duckdb** - SQL queries on local data files

### Research & Information Tools
- **arxiv** - Search academic papers
- **duckduckgo** - Web search
- **googlesearch** - Google search (requires API key)
- **tavily** - AI-powered search (requires API key)
- **wikipedia** - Encyclopedia lookup
- **newspaper4k** - Parse and extract news articles
- **website** - Extract content from websites
- **jina** - Web reading and search via Jina Reader API
- **crawl4ai** - Advanced web crawling
- **pubmed** - Search biomedical literature

### Development Tools
- **docker** - Manage Docker containers (requires Docker installed)
- **github** - Interact with GitHub repositories (requires token)
- **jira** - Jira issue tracking (requires API token)
- **linear** - Linear issue tracking (requires API key)

### Communication Tools
- **email** - Send emails (requires SMTP configuration)
- **telegram** - Send Telegram messages (requires bot token)
- **slack** - Slack messaging (requires bot token)
- **discord** - Discord messaging (requires bot token)
- **matrix_message** - Send messages to other Matrix rooms

### AI & Generation Tools
- **dalle** - Generate images with DALL-E
- **gemini** - Google Gemini multimodal capabilities
- **claude_agent** - Spawn Claude sub-agents
- **subagents** - Delegate tasks to other MindRoom agents

### Productivity Tools
- **scheduler** - Schedule recurring tasks (included by default)
- **gmail** - Gmail integration (requires Google OAuth)
- **google_calendar** - Calendar management (requires Google OAuth)
- **google_sheets** - Spreadsheet operations (requires Google OAuth)
- **todoist** - Task management (requires API key)
- **notion** - Notion workspace integration (requires API key)

### Special Tool Bundles
- **openclaw_compat** - Convenience bundle that implies: shell, coding, duckduckgo, website, browser, scheduler, subagents, matrix_message, attachments

## Example Agent Configurations

### Example 1: Simple Helper Agent

```yaml
agents:
  helper:
    display_name: "HelpfulAssistant"
    role: "Provide friendly help and encouragement"
    tools: []
    instructions:
      - "Always be positive and encouraging"
      - "Offer specific, actionable advice"
      - "Ask clarifying questions when needed"
```

### Example 2: Project Manager Agent

```yaml
agents:
  project_manager:
    display_name: "ProjectManager"
    role: "Help manage software projects"
    tools:
      - file
      - shell
      - github
    instructions:
      - "Track project tasks and milestones"
      - "Generate status reports"
      - "Help with version control"
      - "Create and update documentation"
```

### Example 3: Data Science Agent

```yaml
agents:
  data_scientist:
    display_name: "DataScientist"
    role: "Analyze data and create insights"
    tools:
      - python
      - pandas
      - csv
      - calculator
    instructions:
      - "Perform statistical analysis"
      - "Create data visualizations"
      - "Clean and preprocess data"
      - "Explain findings clearly"
```

### Example 4: Research Assistant

```yaml
agents:
  researcher:
    display_name: "ResearchAssistant"
    role: "Comprehensive research and fact-checking"
    tools:
      - arxiv
      - wikipedia
      - duckduckgo
      - website
      - file
    instructions:
      - "Find credible sources"
      - "Cross-reference information"
      - "Create research summaries"
      - "Track sources and citations"
```

## Using Agents in the Multi-Agent System

Each agent has its own Matrix account.
To interact with an agent:

1. **Mention the agent by its Matrix display name or ID**: `@mindroom_agentname:<server>`
   - Example: `@mindroom_code what is 25 * 4?`

2. **In threads**: Agents automatically respond to all messages without needing mentions
   - Start a thread by replying to any message
   - The agent will see and respond to all subsequent messages in that thread

3. **Multiple agents**: You can mention multiple agents in one message
   - Example: `@mindroom_research @mindroom_code Compare renewable energy trends`

## Tool Requirements

Some tools need additional setup:

### Tools requiring API keys:
- **googlesearch** - Set up Google API credentials
- **tavily** - Get API key from Tavily
- **github** - Create a GitHub personal access token
- **telegram** - Create a Telegram bot and get token
- **email** - Configure SMTP server details

### Tools requiring software:
- **docker** - Install Docker on your system

### Tools that work immediately:
- **calculator**, **file**, **shell**, **python**, **csv**, **pandas**, **arxiv**, **duckduckgo**, **wikipedia**, **newspaper4k**, **website**, **jina**, **yfinance**

## Complete Configuration Example

```yaml
# Memory configuration
memory:
  backend: "mem0"  # "mem0" or "file"
  embedder:
    provider: "ollama"
    config:
      model: "nomic-embed-text"
      host: "http://localhost:11434"

# Model definitions
models:
  default:
    provider: "ollama"
    id: "devstral:24b"

  smart:
    provider: "anthropic"
    id: "claude-sonnet-4-6"

# Agent configurations
agents:
  assistant:
    display_name: "SmartAssistant"
    role: "Advanced reasoning and analysis"
    model: "smart"
    tools: []
    instructions:
      - "Provide thoughtful, detailed responses"
      - "Use advanced reasoning capabilities"
    rooms:
      - lobby

# Teams
teams:
  research_team:
    display_name: "Research Team"
    role: "Collaborative research"
    agents: [assistant]
    mode: coordinate

# Defaults
defaults:
  tools: [scheduler]
  markdown: true
  enable_streaming: true

# Router
router:
  model: "default"

# Timezone
timezone: "America/Los_Angeles"

# Authorization
authorization:
  default_room_access: false
  global_users:
    - "__MINDROOM_OWNER_USER_ID_FROM_PAIRING__"
  agent_reply_permissions:
    "*":
      - "__MINDROOM_OWNER_USER_ID_FROM_PAIRING__"
```

## Troubleshooting

If an agent isn't working as expected:

1. Check that all required tools are properly configured
2. Verify the YAML syntax is correct (proper indentation)
3. Ensure tool names are spelled correctly
4. Test with simpler instructions first
5. Check logs for any error messages (`mindroom run --log-level DEBUG`)

## Best Practices

1. **Clear Agent Roles**: Give each agent a specific, well-defined purpose
2. **Appropriate Tools**: Only include tools the agent actually needs
3. **Detailed Instructions**: Provide clear behavioral guidelines
4. **Test Your Agents**: Try different scenarios to ensure they behave as expected

## Tips for Writing Instructions

Good instructions are specific and actionable:

- Good: "Always cite your sources with author and publication date"
- Vague: "Be accurate"

- Good: "Explain technical concepts in simple terms"
- Vague: "Be helpful"

- Good: "Ask for clarification if the request is ambiguous"
- Vague: "Understand the user"
