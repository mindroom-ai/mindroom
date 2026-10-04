# Tools

MindRoom includes 100+ built-in tools and presets that agents can use to work with files, services, external APIs, and Matrix-native workflows.

## Enabling Tools

Tools are enabled per-agent in the configuration.
Each tool entry can be a plain string or a single-key dict with inline config overrides:

```yaml
agents:
  assistant:
    display_name: Assistant
    role: A helpful assistant with file and web access
    model: sonnet
    tools:
      - file
      - shell:
          extra_env_passthrough: "DAWARICH_*"
      - github
      - duckduckgo
```

You can also assign tools to all agents globally:

```yaml
defaults:
  tools:
    - scheduler
```

`defaults.tools` are merged into each agent's own `tools` list with duplicates removed.
Set `defaults.tools: []` to disable global default tools, or set `agents.<name>.include_default_tools: false` to opt out a specific agent.
When the same tool appears in both `defaults.tools` and an agent's `tools` with inline overrides, the per-agent overrides take priority, with non-overlapping keys merged from both.
See [Per-Agent Tool Configuration](#per-agent-tool-configuration) for the full override syntax and merge order.
Configured MCP servers also appear here as dynamic tools named `mcp_<server_id>`.
See [MCP](https://docs.mindroom.chat/mcp/) for the `mcp_servers` config and naming rules.

## Per-Agent Tool Configuration

Tools can be plain strings or single-key dicts with inline config overrides.
This lets you customize tool behavior per agent without affecting other agents that use the same tool.

```yaml
agents:
  code:
    tools:
      - file                              # no override, uses defaults
      - shell:                            # per-agent override
          extra_env_passthrough: "DAWARICH_*"
          enable_run_shell_command: true
  research:
    tools:
      - shell                             # uses global defaults (no overrides)
      - duckduckgo
```

### Merge Order

MindRoom resolves tool configuration in layers:

1. Tool constructor defaults (hardcoded in tool code)
2. Credentials (dashboard or credential store)
3. `defaults.tools` overrides (global inline config)
4. `agents.<name>.tools` overrides (per-agent inline config)
5. Runtime overrides (sandbox proxy, init overrides)

Within the authored layers (`defaults.tools` and `agents.<name>.tools`), each field has three possible states:

- Key omitted: keep the value from the next lower layer unchanged.
- Concrete value: override the next lower layer with that value.
- `__MINDROOM_INHERIT__`: clear an inherited authored override and fall back to the next lower layer.

When the same tool appears in both `defaults.tools` and `agents.<name>.tools`, MindRoom merges them field-by-field.
Per-agent values win for overlapping keys, non-overlapping keys are kept from both, and `__MINDROOM_INHERIT__` removes the inherited authored value instead of passing the literal string to the tool.

### Defaults with Overrides

`defaults.tools` also accepts the single-key dict syntax for global overrides that apply to all agents:

```yaml
defaults:
  tools:
    - scheduler
    - shell:
        enable_run_shell_command: true     # global default for all agents
```

### Filtering Toolkit Functions

Registered Agno Toolkit integrations accept `include_tools` and `exclude_tools` inline overrides even when those fields are not declared by the concrete toolkit constructor.
`include_tools` is an allowlist, while `exclude_tools` is a denylist applied after the toolkit registers its functions.
Use the function names exposed by the tool catalog.
Unsupported non-Toolkit integrations reject these fields during config validation.

```yaml
agents:
  research:
    tools:
      - searxng:
          include_tools:
            - search_web
            - news_search
            - image_search
```

### Clearing An Inherited Override

Use `__MINDROOM_INHERIT__` when an agent should keep the tool but stop inheriting one authored field from `defaults.tools`.

Optional-field example:

```yaml
defaults:
  tools:
    - shell:
        extra_env_passthrough: "DAWARICH_*"
        enable_run_shell_command: true

agents:
  research:
    tools:
      - shell:
          extra_env_passthrough: __MINDROOM_INHERIT__
```

`research` still inherits `enable_run_shell_command: true`, but `extra_env_passthrough` falls back to the lower layer (persisted tool config if set, otherwise the tool's normal default).
For sandboxed `shell`, provider API keys and other committed runtime credentials are denied by default in both worker startup env and command env.
Use `extra_env_passthrough` when a specific exported process env value must be visible to shell commands.

Required non-secret field example:

```yaml
defaults:
  tools:
    - clickup:
        master_space_id: "space-default"

agents:
  ops:
    tools:
      - clickup:
          master_space_id: __MINDROOM_INHERIT__
```

`ops` still uses the `clickup` tool, but `master_space_id` no longer inherits `"space-default"`.
MindRoom falls back to the next lower layer, which is usually the stored tool config from the dashboard or credential store.

### `include_default_tools` vs `__MINDROOM_INHERIT__`

- `include_default_tools: false` is coarse-grained: it removes every tool and every override inherited from `defaults.tools` for that agent.
- `__MINDROOM_INHERIT__` is fine-grained: it keeps the tool and the rest of the inherited fields, but clears one specific authored override.

### Security Restrictions

Not all config fields can be overridden inline:

- `type="password"` fields are blocked (credentials must go through the dashboard or credential store)
- `base_dir` is blocked (runtime-only, set by the workspace system)
- Fields with `authored_override: false` in the tool metadata are blocked

MindRoom validates overrides at config load time and rejects unknown field names, wrong value types, and blocked fields with a clear error message.

### Backward Compatibility

Existing configs with plain string tool lists work unchanged:

```yaml
tools: [shell, file, duckduckgo]   # still valid
```

### Config Manager

The `!config` chat command and the `config_manager` tool preserve inline overrides when updating tool lists.
Adding or removing tools via chat does not discard existing per-agent overrides on other tools.

See [MindRoom-Managed OAuth Onboarding In Conversation](https://docs.mindroom.chat/oauth-framework/#mindroom-managed-oauth-onboarding-in-conversation).

## Browse By Topic

- [Execution & Coding](https://docs.mindroom.chat/tools/execution-and-coding/) - Local files, shell, Python, coding helpers, and worker-routed execution tools.
- [Data & Databases](https://docs.mindroom.chat/tools/data-and-databases/) - SQL, databases, Google Docs and Drive files, spreadsheets, tabular analysis, and financial/business datasets.
- [Web Search](https://docs.mindroom.chat/tools/web-search/) - Search engines and search APIs.
- [Web Scraping & Browser](https://docs.mindroom.chat/tools/web-scraping-and-browser/) - Crawlers, extractors, browser automation, and page-reading tools.
- [Research Sources](https://docs.mindroom.chat/tools/research-sources/) - ArXiv, Wikipedia, PubMed, and Hacker News.
- [AI & Generation](https://docs.mindroom.chat/tools/ai-and-generation/) - Image, video, speech, and transcription APIs.
- [Media & Content](https://docs.mindroom.chat/tools/media-and-content/) - Media processing, brand/media retrieval, and Spotify.
- [Matrix & Attachments](https://docs.mindroom.chat/tools/matrix-and-attachments/) - Matrix-native messaging and voice messages, thread tags, resolution, summaries, model overrides, Chat UI actions, low-level Matrix API access, and attachment-aware workflows.
- [Agent Chat UI Actions](https://docs.mindroom.chat/tools/chat-ui/) - Bounded requests to reveal an agent computer, open Settings, open Members, or show an interactive canvas in MindRoom Chat.
- [Messaging & Social](https://docs.mindroom.chat/tools/messaging-and-social/) - Email, chat, and social/community integrations.
- [Project Management](https://docs.mindroom.chat/tools/project-management/) - Git hosting, issue trackers, docs platforms, per-thread work plans, and task managers.
- [Atlassian Cloud](https://docs.mindroom.chat/tools/atlassian/) - Per-user OAuth Jira and Confluence Cloud access, additional connected sites, and attachment downloads.
- [Calendar & Scheduling](https://docs.mindroom.chat/tools/calendar-and-scheduling/) - Calendar and task APIs and MindRoom scheduling tools.
- [Memory & Storage](https://docs.mindroom.chat/tools/memory-and-storage/) - Explicit memory tools and external memory providers.
- [Agent Orchestration](https://docs.mindroom.chat/tools/agent-orchestration/) - OAuth connection recovery, Matrix threads, delegation, Dynamic Workflows, config tools, OpenClaw compatibility, and Claude Agent sessions.
- [Dynamic Tools](https://docs.mindroom.chat/tools/dynamic-tools/) - Per-tool lazy loading for optional agent capabilities.
- [Automation & Platforms](https://docs.mindroom.chat/tools/automation-and-platforms/) - Infrastructure automation, generic APIs, and platform aggregators.
- [Location, Commerce, & Home](https://docs.mindroom.chat/tools/location-commerce-and-home/) - Maps, weather, commerce, and Home Assistant.

## Tool Presets And Implied Tools

Some entries are config-only presets rather than runtime toolkits.
`openclaw_compat` expands to a native bundle of MindRoom tools.
Some tools also imply companion tools through `Config.IMPLIED_TOOLS`.
Today `matrix_message` implies both `attachments` and `matrix_room`, so the effective tool set includes all three even when only `matrix_message` is configured explicitly.

See [Tool Runtime Context](https://docs.mindroom.chat/tools/matrix-and-attachments/#tool-runtime-context).

## MindRoom Update Awareness

Enable the `update_awareness` tool to add the installed MindRoom version and the latest published PyPI release to the agent's system prompt.
The tool checks PyPI at most once every 24 hours and stores the result under `mindroom_data/cache/update_awareness.json`.
Every prompt assembled during that cache window receives identical version text, so ordinary turns do not invalidate the prompt cache.
When a newer release is available, the agent is instructed to notify the user briefly at a natural opportunity without repeating the notice in the same conversation.

```yaml
defaults:
  tools:
    - update_awareness
```

See [Worker-Routed Execution](https://docs.mindroom.chat/deployment/sandbox-proxy/#worker-routed-execution) and [Shared-Only Integrations](https://docs.mindroom.chat/deployment/sandbox-proxy/#shared-only-integrations).

## Automatic Dependency Installation

Each tool declares its optional Python dependencies in `pyproject.toml`.
When a tool is enabled but its dependencies are missing, MindRoom can auto-install the required extra at runtime.
Set `MINDROOM_NO_AUTO_INSTALL_TOOLS=1` to disable that behavior.

## Related Docs

- [MCP](https://docs.mindroom.chat/mcp/) - Configure native MCP client servers and expose them as MindRoom tools.
- [Plugins](https://docs.mindroom.chat/plugins/) - Extend MindRoom with custom tools and skills.
- [Attachments](https://docs.mindroom.chat/attachments/) - Attachment lifecycle and context scoping.
- [Scheduling](https://docs.mindroom.chat/scheduling/) - Chat command scheduling and task behavior.
- [OpenClaw Workspace Import](https://docs.mindroom.chat/openclaw/) - `openclaw_compat` preset and workspace portability.
