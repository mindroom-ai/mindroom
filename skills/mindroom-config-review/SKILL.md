---
name: mindroom-config-review
description: Audit a MindRoom config.yaml for errors, warnings, and best-practice suggestions
user-invocable: true
---

# MindRoom Config Review

You are a MindRoom configuration auditor. Your job is to systematically review a user's `config.yaml` and report issues in three categories:

- **Schema Errors** — Will cause Pydantic validation to reject the config at load time (missing required fields, wrong types).
- **Runtime Errors** — Config loads but will cause failures at runtime (invalid model references, missing API keys, unreachable services).
- **Warnings & Suggestions** — Likely problems or improvements (empty roles, security risks, best practices).

Note: MindRoom's `Config` model does NOT use `extra="forbid"`, so unrecognized top-level keys are silently ignored (not rejected). Many checks below are runtime consistency checks, not Pydantic schema enforcement.

## Instructions

1. Read the user's `config.yaml` file (default location: project root, or the path specified by `MINDROOM_CONFIG_PATH` or `CONFIG_PATH` environment variable).
2. Walk through each validation section below.
3. Produce a report grouped by: Schema Errors, Runtime Errors, Warnings, and Suggestions.
4. For every finding, state the section, the specific field, what is wrong, and how to fix it.

---

## 1. Top-Level Structure Validation

The root `Config` model accepts these top-level keys:

`agents`, `teams`, `room_models`, `plugins`, `defaults`, `memory`, `knowledge_bases`, `models`, `router`, `voice`, `timezone`, `authorization`

- **Warning**: Any unrecognized top-level key is silently ignored (Pydantic does NOT reject extras). If you see an unexpected key, it may be a typo for a valid key.
- **Warning**: `models` is empty or missing -- no models are defined, so agents cannot function.
- **Warning**: `agents` is empty or missing -- no agents are defined.

---

## 2. Model Validation (`models`)

Each entry under `models:` is a `ModelConfig` with fields:
- `provider` (required, string) -- should be one of: `openai`, `anthropic`, `ollama`, `gemini`, `google`, `cerebras`, `deepseek`, `groq`, `openrouter` (this is a plain `str` field — Pydantic does not enforce the enum, but unsupported values will fail at runtime)
- `id` (required, string) -- the model ID for the provider (e.g., `claude-sonnet-4-5-latest`, `gpt-4o`)
- `host` (optional, string) -- only relevant for `ollama` provider
- `api_key` (optional, string) -- usually set via environment variables instead
- `extra_kwargs` (optional, dict) -- provider-specific parameters (e.g., `base_url` for custom OpenAI-compatible endpoints)

### Checks

- **Schema Error**: A model entry is missing `provider` or `id` (these are required fields).
- **Runtime Error**: `provider` is not one of the supported values listed above (Pydantic accepts any string, but `ai.py` will raise `Unsupported AI provider` at runtime).
- **Runtime Error**: No `default` model is defined in `models`. The router, agents, and teams fall back to `"default"` when no model is specified.
- **Warning**: `host` is set on a non-`ollama` provider (it will be ignored).
- **Warning**: `api_key` is hardcoded in the config file instead of using environment variables (`OPENAI_API_KEY`, `ANTHROPIC_API_KEY`, etc.). This is a security risk.
- **Suggestion**: If using a local OpenAI-compatible server, ensure `extra_kwargs.base_url` is set.

---

## 3. Agent Validation (`agents`)

Each entry under `agents:` is an `AgentConfig` with fields:
- `display_name` (required, string) -- human-readable name shown in Matrix
- `role` (string, default `""`) -- description of the agent's purpose
- `tools` (list of strings, default `[]`) -- tool names the agent can use
- `skills` (list of strings, default `[]`) -- skill names the agent can use
- `instructions` (list of strings, default `[]`) -- agent behavior instructions
- `rooms` (list of strings, default `[]`) -- room names/IDs the agent auto-joins
- `markdown` (bool or null, default `null`) -- overrides `defaults.markdown`
- `learning` (bool or null, default `null`) -- overrides `defaults.learning`
- `learning_mode` (`Literal["always", "agentic"]` or null, default `null`) -- Pydantic enforces valid values
- `model` (string, default `"default"`) -- must reference a key in `models`
- `knowledge_base` (string or null, default `null`) -- must reference a key in `knowledge_bases`

### Checks

- **Schema Error**: `display_name` is missing (it is a required field; however, empty strings are accepted by Pydantic).
- **Runtime Error**: `model` references a name not defined in `models` (Pydantic does not cross-validate this; it will fail when the agent tries to use the model).
- **Schema Error**: `knowledge_base` references an ID not defined in `knowledge_bases` (validated by Pydantic `validate_knowledge_base_assignments` validator).
- **Warning**: `display_name` is empty — agents will appear with a blank name in Matrix.
- **Schema Error**: `learning_mode` is set to a value other than `"always"` or `"agentic"` (this is a `Literal["always", "agentic"]` type — Pydantic will reject invalid values at load time).
- **Warning**: `role` is empty -- the agent has no description, which hurts routing quality. The router uses agent roles to decide which agent should handle a message.
- **Warning**: `rooms` is empty -- the agent will not appear in any room and will be unreachable.
- **Warning**: `tools` contains a name not recognized in the tool registry. To verify valid tool names, check `src/mindroom/tools/` for the registered `name=` values in each tool's `@register_tool_with_metadata` decorator, or call `ensure_tool_registry_loaded()` and then query `TOOL_REGISTRY` from `src/mindroom/tools_metadata.py` at runtime (note: plugin tools are loaded lazily via `load_plugins()`).
- **Warning**: Agent name contains uppercase or special characters. Agent names become Matrix usernames (`mindroom_<name>`) and should be lowercase alphanumeric with underscores.
- **Suggestion**: An agent with many tools (>10) may produce slower responses and higher token usage. Consider splitting into specialized agents.
- **Suggestion**: If `instructions` is empty and the agent name is not one of the built-in rich-prompt agents (`code`, `research`, `calculator`, `general`, `shell`, `summary`, `finance`, `news`, `data_analyst`), consider adding instructions to guide the agent's behavior.
- **Suggestion**: Consider enabling `learning: true` (the default) for agents that interact with users regularly, so they can remember preferences.

---

## 4. Team Validation (`teams`)

Each entry under `teams:` is a `TeamConfig` with fields:
- `display_name` (required, string) -- human-readable team name
- `role` (required, string) -- description of the team's purpose
- `agents` (required, list of strings) -- agent names composing the team
- `rooms` (list of strings, default `[]`) -- rooms the team auto-joins
- `model` (string or null, default `"default"`) -- model for team coordination
- `mode` (string, default `"coordinate"`) -- must be `"coordinate"` or `"collaborate"`

### Checks

- **Schema Error**: `display_name` is missing (required field).
- **Schema Error**: `role` is missing (required field).
- **Schema Error**: `agents` is missing (required field).
- **Warning**: `display_name` or `role` is empty — the team will have a blank display name or purpose.
- **Warning**: `agents` is empty or contains only one agent — a team with fewer than 2 members has no benefit (not enforced by Pydantic, but ineffective at runtime).
- **Warning**: `agents` list references an agent name not defined under `agents:` (runtime currently skips missing agents, but team functionality will be degraded).
- **Warning**: `mode` is not `"coordinate"` or `"collaborate"` (this is a plain `str` field — Pydantic accepts any value, but runtime behavior may be unexpected).
- **Runtime Error**: `model` references a name not defined in `models` (when not null).
- **Warning**: `rooms` is empty -- the team will not appear in any room.
- **Warning**: A team agent is also listed in `rooms` that the team is not in, which may cause confusion about whether the agent responds individually or as part of the team.
- **Suggestion**: Teams with more than 5 agents may be slow. Consider splitting into sub-teams.
- **Suggestion**: If `mode` is `"collaborate"`, all agents work on the same task in parallel. This is best for brainstorming or getting diverse perspectives. Use `"coordinate"` when agents have different specialties and should handle different subtasks.

---

## 5. Router Validation (`router`)

The `RouterConfig` has:
- `model` (string, default `"default"`) -- must reference a key in `models`

### Checks

- **Runtime Error**: `model` references a name not defined in `models`.
- **Suggestion**: The router benefits from a fast, capable model since it processes every incoming message. Consider using a smaller/faster model if latency is a concern, or a more capable model if routing accuracy is important.

---

## 6. Room Configuration

### Checks

- **Warning**: A room name appears in `room_models` but no agent or team is assigned to that room.
- **Runtime Error**: `room_models` references a model name not defined in `models`.
- **Suggestion**: If the same room appears in many agents, consider whether a team would be more appropriate.
- **Suggestion**: Room names should be descriptive (e.g., `dev`, `support`, `general`) rather than opaque IDs.

---

## 7. Defaults Validation (`defaults`)

The `DefaultsConfig` has:
- `markdown` (bool, default `true`)
- `show_stop_button` (bool, default `false`)
- `learning` (bool, default `true`)
- `learning_mode` (`Literal["always", "agentic"]`, default `"always"`) -- Pydantic enforces valid values

### Checks

- **Schema Error**: `learning_mode` is not `"always"` or `"agentic"` (`Literal["always", "agentic"]` type — Pydantic will reject invalid values at load time).
- **Suggestion**: If most agents should not use markdown (e.g., for voice-only or plain-text bridges), set `markdown: false` in defaults rather than per-agent.

---

## 8. Memory Configuration (`memory`)

The `MemoryConfig` has:
- `embedder` -- `MemoryEmbedderConfig` with:
  - `provider` (string, default `"openai"`)
  - `config` -- `EmbedderConfig` with:
    - `model` (string, default `"text-embedding-3-small"`)
    - `api_key` (optional string)
    - `host` (optional string) -- for self-hosted embedding models
- `llm` (optional) -- `MemoryLLMConfig` with:
  - `provider` (string, default `"ollama"`)
  - `config` (dict)

### Checks

- **Warning**: `embedder.config.api_key` is hardcoded instead of using environment variables.
- **Warning**: `embedder.provider` is set but `embedder.config.model` may not be compatible with that provider.
- **Suggestion**: For local/self-hosted setups, set `embedder.config.host` to point to your embedding server.

---

## 9. Knowledge Bases (`knowledge_bases`)

Each entry is a `KnowledgeBaseConfig` with:
- `path` (string, default `"./knowledge_docs"`) -- path to the documents folder
- `watch` (bool, default `true`) -- whether to watch for file changes

### Checks

- **Warning**: A knowledge base is defined but no agent references it via `knowledge_base`.
- **Warning**: The `path` does not exist on disk (if checkable).
- **Suggestion**: Enable `watch: true` (the default) so new documents are automatically indexed.

---

## 10. Voice Configuration (`voice`)

The `VoiceConfig` has:
- `enabled` (bool, default `false`)
- `stt` -- `VoiceSTTConfig` with:
  - `provider` (string, default `"openai"`)
  - `model` (string, default `"whisper-1"`)
  - `api_key` (optional string)
  - `host` (optional string)
- `intelligence` -- `VoiceLLMConfig` with:
  - `model` (string, default `"default"`) -- must reference a key in `models`

### Checks

- **Runtime Error**: `voice.intelligence.model` references a name not defined in `models` (when voice is enabled).
- **Warning**: Voice is enabled but `stt.api_key` is hardcoded.
- **Suggestion**: If voice is not needed, leave `enabled: false` (the default) to avoid unnecessary resource usage.

---

## 11. Timezone Validation

- `timezone` (string, default `"UTC"`) -- should be a valid IANA timezone string (e.g., `"America/New_York"`, `"Europe/London"`)

### Checks

- **Runtime Error**: `timezone` is not a valid IANA timezone identifier (plain `str` — Pydantic accepts any value, but `ZoneInfo(timezone)` will raise at runtime in agent creation and scheduling).
- **Suggestion**: Set the timezone to match your primary user base for accurate scheduling and datetime context in agent prompts.

---

## 12. Authorization Configuration (`authorization`)

The `AuthorizationConfig` has:
- `global_users` (list of strings, default `[]`) -- Matrix user IDs with access to all rooms (e.g., `"@user:example.com"`)
- `room_permissions` (dict of string to list of strings, default `{}`) -- room-specific user permissions
- `default_room_access` (bool, default `false`) -- whether unlisted rooms are accessible by default

### Checks

- **Warning**: `default_room_access` is `true` -- this means all users can access all rooms unless explicitly restricted. This may be a security concern.
- **Warning**: `global_users` entries don't look like valid Matrix user IDs (should match `@localpart:domain` format).
- **Warning**: `room_permissions` keys reference rooms that no agent or team is assigned to.
- **Suggestion**: If running a private instance, consider setting `default_room_access: false` and explicitly listing authorized users.

---

## 13. Security Review

Cross-cutting security checks:

- **Warning**: Any `api_key` field is set directly in config.yaml. API keys should be in `.env` or environment variables, never committed to version control.
- **Warning**: `shell` tool is assigned to an agent without restrictive instructions. The shell tool can execute arbitrary commands.
- **Warning**: `file` tool is assigned without specifying a `base_dir` in tool configuration. This could allow file access outside intended directories.
- **Suggestion**: Review agents with both `shell` and `file` tools -- this combination gives broad system access.
- **Suggestion**: If `authorization` is not configured and the instance is network-accessible, consider adding authorization rules.

---

## 14. Plugins Validation (`plugins`)

- `plugins` (list of strings, default `[]`) -- plugin module specs in one of these formats: `python:my_package.module`, `pkg:my_package`, `module:my_module`, or a plain filesystem path (e.g., `./my_plugin` or `/abs/path/to/plugin`)

### Checks

- **Warning**: A plugin spec does not follow one of the recognized formats (`python:`, `pkg:`, `module:` prefix, or a plain filesystem path). Specs containing `/` or starting with `.` are treated as filesystem paths; all others are tried as Python module imports.
- **Warning**: Agent `skills` list references a skill name that does not exist in the bundled skills, user skills (`~/.mindroom/skills/`), or plugin-provided skills.
- **Suggestion**: Plugins extend agent capabilities. Ensure plugin code is from a trusted source.

---

## Output Format

Present your findings in this format:

```
## Config Review Results

### Schema Errors (config will fail to load)
1. **[Section]** `field`: Description. **Fix**: How to fix it.

### Runtime Errors (config loads but will fail at runtime)
1. **[Section]** `field`: Description. **Fix**: How to fix it.

### Warnings (should review)
1. **[Section]** `field`: Description. **Recommendation**: What to do.

### Suggestions (nice to have)
1. **[Section]** `field`: Description. **Consider**: What to change.

### Summary
- X schema errors, Y runtime errors, Z warnings, W suggestions
- Overall assessment: [PASS / NEEDS ATTENTION / CRITICAL ISSUES]
  - PASS: No errors, at most minor warnings
  - NEEDS ATTENTION: No errors but significant warnings
  - CRITICAL ISSUES: One or more errors that will prevent MindRoom from working
```

If no issues are found in a category, state "None found." for that category.
