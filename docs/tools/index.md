---
icon: lucide/wrench
---

# Tools

MindRoom includes 100+ built-in tools and presets that agents can use to work with files, services, external APIs, and Matrix-native workflows.
This page covers enabling tools, per-agent tool settings, function filters, presets, update awareness, and dependency installation.
To find a specific tool, see [Browse By Topic](#browse-by-topic).

## Enabling Tools

List tools under an agent's `tools`, or under `defaults.tools` to give them to every agent:

```yaml
defaults:
  tools:
    - scheduler

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

Each agent gets its own `tools` plus `defaults.tools`, without duplicates.
The `defaults.tools` default and the per-agent `include_default_tools` opt-out are described in [Agent Configuration](../configuration/agents.md).
Configured MCP servers appear as tools named `mcp_<server_id>`; see [MCP](../mcp.md).
When [`config_manager` or `self_config`](agent-orchestration.md#access-redaction-and-saving) replaces an agent's tool list, tools that stay keep their inline settings.

## Per-Agent Tool Configuration

A tool entry can be a plain name or a single-key mapping of the tool name to inline settings.
Inline settings change the tool for that agent only.
Settings under `defaults.tools` apply to every agent that includes default tools.

```yaml
defaults:
  tools:
    - scheduler
    - shell:
        enable_run_shell_command: true     # every agent

agents:
  code:
    tools:
      - file                               # no inline settings
      - shell:                             # this agent only
          extra_env_passthrough: "DAWARICH_*"
  research:
    tools:
      - shell                              # uses the defaults.tools settings
      - duckduckgo
```

Each tool's page lists the settings it accepts.

### Security Restrictions

Some fields cannot be set inline:

- `password` fields, such as API keys and tokens, must be set through the dashboard or credential store.
- Fields MindRoom manages itself, such as `base_dir`, cannot be overridden.
- Per-agent-only fields, such as the [`usage_stats`](../usage.md#usage_stats) tool's `admin_scope`, are rejected in `defaults.tools`.

MindRoom rejects these fields, unknown field names, and values of the wrong type when the config loads, and the unknown-field error lists the allowed fields.

### How Settings Combine

From lowest to highest priority, a tool's settings come from:

1. The tool's built-in defaults.
2. Saved tool settings from the dashboard or credential store.
3. `defaults.tools` inline settings.
4. `agents.<name>.tools` inline settings.
5. Values MindRoom sets for the agent's runtime, such as its workspace directory.

When a tool appears in both `defaults.tools` and an agent's `tools`, the settings merge field by field.
Per-agent values win for the same field, and fields set in only one place are kept.

### Clearing An Inherited Override

Set a field to `__MINDROOM_INHERIT__` to keep a tool but drop one inherited `defaults.tools` setting:

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

`research` still inherits `enable_run_shell_command: true`, while `extra_env_passthrough` falls back to the saved tool setting, or to the tool's default when none is saved.
To drop every default tool and all their settings for an agent instead, set `include_default_tools: false`.

### Filtering Toolkit Functions

Most built-in toolkits accept `include_tools` and `exclude_tools` inline settings to limit which of their functions the agent sees.
`include_tools` is an allowlist, and `exclude_tools` removes functions from what remains.
Use the function names the toolkit exposes.
Tools that do not support filtering reject these fields when the config loads.

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

A name the toolkit does not provide fails when the tool loads with `Included tool(s) not present in the toolkit: <names>` or `Excluded tool(s) not present in the toolkit: <names>`.

## Browse By Topic

- [Execution & Coding](execution-and-coding.md) - Local files, shell, Python, coding helpers, and worker-routed execution tools.
- [Background Python Scripts](background-scripts.md) - Long-running Python scripts that call the agent's tools and wake it when something changes.
- [Worker Computer](worker-computer.md) - A visible browser in the agent's worker that the user can watch and control.
- [Desktop Bridge](desktop.md) - Operate allowlisted apps, read selected folders, and run approved commands on a local computer.
- [Minimal Agent Mode](agent-cli.md) - One Bash tool instead of full tool schemas, to cut tokens per request.
- [Data & Databases](data-and-databases.md) - SQL, databases, Google Docs and Drive files, spreadsheets, tabular analysis, and financial/business datasets.
- [Web Search](web-search.md) - Search engines and search APIs.
- [Web Scraping & Browser](web-scraping-and-browser.md) - Crawlers, extractors, browser automation, and page-reading tools.
- [Research Sources](research-sources.md) - ArXiv, Google Scholar, Wikipedia, PubMed, and Hacker News.
- [AI & Generation](ai-and-generation.md) - Image, video, speech, and transcription APIs.
- [Media & Content](media-and-content.md) - Media processing, brand/media retrieval, and Spotify.
- [Matrix & Attachments](matrix-and-attachments.md) - Matrix-native messaging and voice messages, thread tags, resolution, summaries, model overrides, low-level Matrix API access, and attachment-aware workflows.
- [Matrix Message Tool](matrix-message.md) - Send, read, edit, and react to messages with `matrix_message`.
- [Agent Chat UI Actions](chat-ui.md) - Bounded requests to reveal an agent computer, open Settings, open Members, or show an [interactive canvas](../canvases.md) in MindRoom Chat.
- [Messaging & Social](messaging-and-social.md) - Email, chat, and social/community integrations.
- [Project Management](project-management.md) - Git hosting, issue trackers, docs platforms, per-thread work plans, and task managers.
- [Atlassian Cloud](atlassian.md) - Per-user OAuth Jira and Confluence Cloud access, additional connected sites, and attachment downloads.
- [Calendar & Scheduling](calendar-and-scheduling.md) - Calendar and task APIs and MindRoom scheduling tools.
- [Memory & Storage](memory-and-storage.md) - Mem0 and Zep external memory services; the built-in [`memory`](../memory.md#memory) tool is documented with the memory system.
- [Agent Orchestration](agent-orchestration.md) - Delegation, Dynamic Workflows, report publishing, config tools, and Claude Agent sessions.
- [Dynamic Tools](dynamic-tools.md) - Per-tool lazy loading for optional agent capabilities.
- [Automation & Platforms](automation-and-platforms.md) - Infrastructure automation, generic APIs, and platform aggregators.
- [Location, Commerce, & Home](location-commerce-and-home.md) - Maps, weather, commerce, and Home Assistant.

## Tool Presets And Implied Tools

`openclaw_compat` is a preset that expands to a bundle of MindRoom tools; see [OpenClaw Workspace Import](../openclaw.md).
Enabling `matrix_message` also enables its [companion tools](matrix-message.md#setup).

## MindRoom Update Awareness

Enable the `update_awareness` tool to tell the agent which MindRoom version is installed and which release is the latest on PyPI.

```yaml
defaults:
  tools:
    - update_awareness
```

MindRoom checks PyPI at most once every 24 hours and caches the result in `mindroom_data/cache/update_awareness.json`.
When a newer release exists, the agent mentions it briefly at a natural moment, once per conversation, and does not install it unless the user asks.
The agent can also call `get_mindroom_update_status()` to report both versions.

## Automatic Dependency Installation

When an enabled tool's optional Python dependencies are missing, MindRoom installs them the first time the tool loads.
Set `MINDROOM_NO_AUTO_INSTALL_TOOLS=1` to disable this.
When auto-install is disabled or fails, the tool fails to load with `Missing dependencies for tool '<tool>': <packages>`.
Install the tool's extra manually, for example `pip install 'mindroom[<tool>]'` or `uv sync --extra <tool>` in a source checkout.

## Related Docs

- [MCP](../mcp.md) - Configure native MCP client servers and expose them as MindRoom tools.
- [Plugins](../plugins.md) - Extend MindRoom with custom tools and skills.
- [Sandbox Proxy](../deployment/sandbox-proxy.md#worker-routing) - Which tools run in isolated workers and which stay in the primary runtime.
- [OAuth Onboarding In Conversation](../oauth-framework.md#mindroom-managed-oauth-onboarding-in-conversation) - How agents send users links to connect OAuth tools.
- [Attachments](../attachments.md) - Attachment lifecycle and context scoping.
- [Scheduling](../scheduling.md) - Chat command scheduling and task behavior.
