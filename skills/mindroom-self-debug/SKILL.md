---
name: mindroom-self-debug
description: Self-diagnose issues by inspecting MindRoom runtime state, source code, config, logs, sessions, and memory stores
metadata:
  openclaw:
    always: true
---

# MindRoom Self-Debug

You are a MindRoom agent with the ability to introspect your own runtime. Use this skill when something goes wrong, when a user reports a problem, or when you need to understand how MindRoom works internally. This skill gives you a mental map of the entire MindRoom architecture and instructions for live diagnostics.

---

## Architecture Overview

MindRoom is an AI agent orchestration system that runs agents inside Matrix chat rooms. Here is how the system works at runtime:

```
User message in Matrix room
  -> Synapse (Matrix homeserver)
    -> nio sync loop (one per agent, in bot.py)
      -> Router agent decides which agent responds (routing.py)
        -> Selected agent processes message (ai.py -> agents.py -> Agno Agent)
          -> Response streamed back via Matrix (streaming.py)
```

### Core Runtime Components

| Module | File | Purpose |
|--------|------|---------|
| **Orchestrator** | `src/mindroom/bot.py` | `MultiAgentOrchestrator` -- boots all agents, manages sync loops, handles hot-reload of config.yaml |
| **Agent creation** | `src/mindroom/agents.py` | `create_agent()` -- builds Agno `Agent` from config, loads tools, skills, storage, learning |
| **AI execution** | `src/mindroom/ai.py` | `ai_response()` / `stream_agent_response()` -- prepares prompts with memory, runs agent, caches results |
| **Routing** | `src/mindroom/routing.py` | `suggest_agent_for_message()` -- AI-based router picks best agent for each message |
| **Config** | `src/mindroom/config.py` | Pydantic models: `Config`, `AgentConfig`, `ModelConfig`, `TeamConfig`, etc. |
| **Constants** | `src/mindroom/constants.py` | Paths, environment variables, Matrix settings |
| **Teams** | `src/mindroom/teams.py` | Multi-agent collaboration (coordinate/collaborate modes) |
| **Memory** | `src/mindroom/memory/functions.py` | Mem0-based dual memory: agent-scoped, room-scoped, team-scoped |
| **Knowledge** | `src/mindroom/knowledge.py` | RAG file indexing with file watcher |
| **Skills** | `src/mindroom/skills.py` | Skill loading from SKILL.md files, eligibility filtering |
| **Streaming** | `src/mindroom/streaming.py` | Real-time message updates via Matrix edits |
| **Error handling** | `src/mindroom/error_handling.py` | User-friendly error messages for auth, rate-limit, timeout errors |
| **Tools registry** | `src/mindroom/tools_metadata.py` | Tool registration, `get_tool_by_name()`, `TOOL_REGISTRY` |
| **Matrix client** | `src/mindroom/matrix/client.py` | Send/edit messages, fetch threads, room operations |
| **Matrix identity** | `src/mindroom/matrix/identity.py` | `MatrixID` -- maps agent names to `@mindroom_<name>:<domain>` |
| **Matrix rooms** | `src/mindroom/matrix/rooms.py` | Room creation, alias resolution, membership |
| **Matrix users** | `src/mindroom/matrix/users.py` | Agent user provisioning, login, avatar |
| **Scheduling** | `src/mindroom/scheduling.py` | Cron and natural-language task scheduling |
| **Plugins** | `src/mindroom/plugins.py` | Plugin loading and tool/skill extension |
| **Credentials** | `src/mindroom/credentials.py` | `CredentialsManager` -- secure key storage |
| **Config commands** | `src/mindroom/config_commands.py` | `!config` chat commands for live config editing |
| **Voice** | `src/mindroom/voice_handler.py` | Voice message STT processing |

---

## Persistent State Locations

All runtime state is stored under `STORAGE_PATH` (default: `mindroom_data/`, overridable via env var).

| Path | Contents | Format |
|------|----------|--------|
| `mindroom_data/sessions/<agent>.db` | Per-agent conversation history | SQLite (Agno sessions) |
| `mindroom_data/learning/<agent>.db` | Per-agent learning data (user profiles, memories) | SQLite (Agno learning) |
| `mindroom_data/memory/` | Mem0 vector store for agent/room/team memories | Mem0 data files |
| `mindroom_data/tracking/` | Response tracking to avoid duplicate replies | JSON/tracking files |
| `mindroom_data/credentials/` | API keys and secrets (synced from .env) | JSON |
| `mindroom_data/encryption_keys/` | E2E encryption keys | Key files |
| `mindroom_data/matrix_state.yaml` | Agent accounts (username/password) and room metadata (ID, alias, name) | YAML |
| `mindroom_data/.ai_cache/` | Cached AI responses (diskcache) | diskcache DB |
| `config.yaml` | Agent/model/team/router configuration | YAML |
| `.env` | Environment variables and API keys | Dotenv |

---

## Self-Diagnostic Procedures

When diagnosing an issue, follow these procedures in order of relevance.

### 1. Identify Yourself

You can understand your own identity from your agent prompt. Your agent name, display name, model provider, and model ID are injected into your system prompt at startup by `agents.py`. Look for the identity context block at the beginning of your role/instructions.

Key facts about your runtime:
- Your Matrix username is `@mindroom_<your_agent_name>:<domain>`
- Your conversation history is in `mindroom_data/sessions/<your_agent_name>.db`
- Your learning data is in `mindroom_data/learning/<your_agent_name>.db`
- Your memory scope is `agent_<your_agent_name>`
- Your config is under `agents.<your_agent_name>` in `config.yaml`

### 2. Check Configuration

Read `config.yaml` and verify:
- Your agent entry exists under `agents:`
- Your `model` field references a valid model in `models:`
- Your `tools` list contains only registered tool names
- Your `rooms` list matches the rooms you should be in
- Your `knowledge_base` (if set) references an entry in `knowledge_bases:`

### 3. Diagnose Model/API Errors

Common error patterns and their causes:

| Error message | Cause | Fix |
|---------------|-------|-----|
| `Authentication failed (openai)` | Missing or invalid `OPENAI_API_KEY` | Check `.env` for the correct API key |
| `Authentication failed (anthropic)` | Missing or invalid `ANTHROPIC_API_KEY` | Check `.env` for the correct API key |
| `Rate limited` | Too many API requests | Wait and retry; consider a different model |
| `Request timed out` | Model provider slow or unreachable | Check network; try a different model |
| `Unknown model: <name>` | Model name in agent config not in `models:` section | Add the model to `models:` in config.yaml |
| `Unknown tool: <name>` | Tool name in agent config not registered | Check tool name spelling; see `src/mindroom/tools/` |
| `Unsupported AI provider: <name>` | Provider not in the supported list | Use one of: openai, anthropic, ollama, gemini, google, cerebras, deepseek, groq, openrouter |

The error handling logic is in `src/mindroom/error_handling.py`. Errors are categorized as auth (401), rate-limit (429), timeout, or generic.

### 4. Diagnose Routing Issues

If messages are not reaching the right agent:

1. The router uses `routing.py:suggest_agent_for_message()` which sends agent descriptions to the router model
2. Agent descriptions are built by `agents.py:describe_agent()` using: agent name, role, tools list, and first instruction
3. If your `role` is empty or vague, the router may not select you correctly
4. The router model is configured in `router.model` (defaults to `"default"`)
5. Thread context (last 3 messages) is included in routing decisions

### 5. Diagnose Tool Failures

When a tool call fails:

1. Check if the tool is in your `tools` list in config.yaml
2. Tool loading happens in `agents.py:create_agent()` -- tools are loaded by name from `TOOL_REGISTRY`
3. The special `memory` tool is instantiated differently (requires agent context) -- see `agents.py`
4. Some tools require environment variables (API keys). Check the tool's `@register_tool_with_metadata` decorator in `src/mindroom/tools/<tool_name>.py` for `config_fields`
5. Tool execution errors are caught and returned as user-friendly messages

### 6. Diagnose Memory Issues

Memory system uses Mem0 with three scopes:
- **Agent memory** (`agent_<name>`): Personal preferences, learned information
- **Room memory** (`room_<sanitized_room_id>`): Room-specific project context. Note: room IDs are sanitized — `!` is removed and `:` is replaced with `_` (e.g., `!abc123:example.com` becomes `room_abc123_example.com`)
- **Team memory** (`team_<sorted_agent_names>`): Shared team context

Memory is enhanced into prompts by `memory/functions.py:build_memory_enhanced_prompt()` before every AI call.

If memory seems missing:
1. Check if `defaults.learning` is `true` (default) or if the agent overrides it with `learning: false`
2. Check `mindroom_data/memory/` for the Mem0 data files
3. The memory embedder config is in `memory.embedder` in config.yaml
4. Memory search may fail silently if the embedder API key is missing

### 7. Diagnose Team Issues

Teams are configured under `teams:` in config.yaml with modes:
- `coordinate`: Leader delegates different subtasks to different agents
- `collaborate`: All agents work on the same task in parallel

Team formation logic (`teams.py:decide_team_formation()`) triggers when:
1. Multiple agents are explicitly @-mentioned
2. Multiple agents participated in the thread history
3. DM room has multiple agents (main timeline only)

If team responses fail:
1. Check that all team member names exist in `agents:`
2. Check that the team's `model` is valid
3. Team streaming happens via `team_response_stream()` in teams.py

### 8. Diagnose Streaming Issues

Streaming is enabled by default (`MINDROOM_ENABLE_STREAMING=true` env var).

The streaming flow:
1. `ai.py:stream_agent_response()` yields events from Agno
2. `streaming.py` accumulates chunks and edits the Matrix message in-place
3. The ` ⋯` marker (Unicode midline horizontal ellipsis) indicates the response is still in progress
4. Streaming can be disabled per-provider if the model doesn't support it

If messages appear incomplete or stuck with ` ⋯`:
1. Check if the model provider supports streaming
2. Look for errors in the agent's session (timeout, rate limit)
3. The stop manager (`stop.py`) can cancel in-flight responses

### 9. Diagnose Room/Matrix Issues

Room management:
1. Rooms are created by `matrix/rooms.py:ensure_all_rooms_exist()`
2. Room aliases follow the pattern `#<room_name>:<domain>`
3. Agent membership is managed by `ensure_user_in_rooms()`
4. Room metadata (IDs, aliases, names) and agent accounts are stored in `matrix_state.yaml`

If an agent is missing from a room:
1. Check that the room is listed in the agent's `rooms:` config
2. Check `mindroom_data/matrix_state.yaml` for the room's ID
3. The orchestrator re-joins rooms on startup and config reload

### 10. Check Hot-Reload

Config changes are detected by `file_watcher.py:watch_file()`. When `config.yaml` changes:
1. The orchestrator diffs the old and new configs
2. Changed agents are restarted
3. Room memberships are updated

Note: The skill cache is cleared by a **separate** file watcher that monitors skill files, not by the config watcher.

If hot-reload seems broken:
1. Check file watcher is running (look for `file_watcher` in logs)
2. YAML syntax errors will prevent reload -- validate the YAML
3. Some changes (like Matrix homeserver) require a full restart

---

## Common Debugging Recipes

### "Why am I not responding to messages?"
1. Check your `rooms` list in config.yaml -- are you in the right room?
2. Check the router's `role` descriptions -- does the router know about you?
3. Check `matrix_state.yaml` -- is your Matrix user joined to the room?
4. Check if another agent is being selected instead (routing issue)

### "Why is my response slow?"
1. Check your model -- slower models (e.g., large Claude/GPT models) take longer
2. Check your tool count -- many tools increase prompt size and latency
3. Check if AI caching is enabled (`ENABLE_AI_CACHE=true`)
4. Check memory search -- large memory stores slow down prompt building

### "Why do I keep giving the same answer?"
1. Check if AI caching is enabled -- cached responses repeat exactly
2. The cache key includes: agent name, model, prompt, and session ID
3. Clear the cache by removing `mindroom_data/.ai_cache/`

### "Why did my tool call fail?"
1. Read the error message -- it usually indicates the cause
2. Check the tool's required environment variables
3. Check if the tool is actually in your `tools` list
4. Some tools need special config (e.g., `homeassistant` needs URL + token)

### "Why are my memories not working?"
1. Check `defaults.learning` and your agent's `learning` override
2. Check `memory.embedder` config -- is the API key set?
3. Check `mindroom_data/memory/` exists and has data
4. Memory is scope-limited: you can only access `agent_<your_name>` and team memories you belong to

---

## Environment Variables Reference

| Variable | Purpose | Default |
|----------|---------|---------|
| `MATRIX_HOMESERVER` | Matrix server URL | `http://localhost:8008` |
| `MATRIX_SERVER_NAME` | Server name for federation | None (derived from homeserver) |
| `MATRIX_SSL_VERIFY` | Verify SSL certificates | `true` |
| `STORAGE_PATH` | Runtime data directory | `mindroom_data` |
| `MINDROOM_CONFIG_PATH` / `CONFIG_PATH` | Config file location | `config.yaml` in project root |
| `MINDROOM_ENABLE_STREAMING` | Enable streaming responses | `true` |
| `ENABLE_AI_CACHE` | Enable response caching | `true` |
| `OPENAI_API_KEY` | OpenAI API key | (required for OpenAI models) |
| `ANTHROPIC_API_KEY` | Anthropic API key | (required for Anthropic models) |
| `OPENROUTER_API_KEY` | OpenRouter API key | (required for OpenRouter models) |
| `GOOGLE_API_KEY` | Google/Gemini API key | (required for Gemini models) |
| `CEREBRAS_API_KEY` | Cerebras API key | (required for Cerebras models) |
| `DEEPSEEK_API_KEY` | DeepSeek API key | (required for DeepSeek models) |
| `GROQ_API_KEY` | Groq API key | (required for Groq models) |

---

## Supported Model Providers

The `ai.py:_create_model_for_provider()` function supports these providers:

| Provider | Agno Class | Notes |
|----------|-----------|-------|
| `openai` | `OpenAIChat` | Also works with OpenAI-compatible APIs via `extra_kwargs.base_url` |
| `anthropic` | `Claude` | Claude models |
| `ollama` | `Ollama` | Local models, uses `host` field (default: `http://localhost:11434`) |
| `gemini` / `google` | `Gemini` | Google Gemini models |
| `cerebras` | `Cerebras` | Fast inference |
| `deepseek` | `DeepSeek` | DeepSeek models |
| `groq` | `Groq` | Fast inference |
| `openrouter` | `OpenRouter` | Multi-provider gateway, needs explicit `api_key` |

---

## Config Pydantic Models Quick Reference

These are the Pydantic models from `src/mindroom/config.py` that define valid configuration:

### AgentConfig
- `display_name: str` (required)
- `role: str` (default: `""`)
- `tools: list[str]` (default: `[]`)
- `skills: list[str]` (default: `[]`)
- `instructions: list[str]` (default: `[]`)
- `rooms: list[str]` (default: `[]`)
- `markdown: bool | None` (default: `None`, falls back to `defaults.markdown`)
- `learning: bool | None` (default: `None`, falls back to `defaults.learning`)
- `learning_mode: "always" | "agentic" | None` (default: `None`)
- `model: str` (default: `"default"`)
- `knowledge_base: str | None` (default: `None`)

### ModelConfig
- `provider: str` (required)
- `id: str` (required)
- `host: str | None` (default: `None`)
- `api_key: str | None` (default: `None`)
- `extra_kwargs: dict | None` (default: `None`)

### TeamConfig
- `display_name: str` (required)
- `role: str` (required)
- `agents: list[str]` (required)
- `rooms: list[str]` (default: `[]`)
- `model: str | None` (default: `"default"`)
- `mode: str` (default: `"coordinate"`, valid: `"coordinate"` or `"collaborate"`)

### RouterConfig
- `model: str` (default: `"default"`)

### DefaultsConfig
- `markdown: bool` (default: `true`)
- `show_stop_button: bool` (default: `false`)
- `learning: bool` (default: `true`)
- `learning_mode: "always" | "agentic"` (default: `"always"`)

### MemoryConfig > MemoryEmbedderConfig > EmbedderConfig
- `embedder.provider: str` (default: `"openai"`)
- `embedder.config.model: str` (default: `"text-embedding-3-small"`)
- `embedder.config.api_key: str | None`
- `embedder.config.host: str | None`

### VoiceConfig
- `enabled: bool` (default: `false`)
- `stt.provider: str` (default: `"openai"`)
- `stt.model: str` (default: `"whisper-1"`)
- `intelligence.model: str` (default: `"default"`)

### AuthorizationConfig
- `global_users: list[str]` (default: `[]`)
- `room_permissions: dict[str, list[str]]` (default: `{}`)
- `default_room_access: bool` (default: `false`)

### KnowledgeBaseConfig
- `path: str` (default: `"./knowledge_docs"`)
- `watch: bool` (default: `true`)
