# Tools

MindRoom includes 100+ built-in tools and presets that agents can use to work with files, services, external APIs, and Matrix-native workflows.

## Enabling Tools

Tools are enabled per-agent in the configuration. Each tool entry can be a plain string or a single-key dict with inline config overrides:

```
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

```
defaults:
  tools:
    - scheduler
```

`defaults.tools` are merged into each agent's own `tools` list with duplicates removed. Set `defaults.tools: []` to disable global default tools, or set `agents.<name>.include_default_tools: false` to opt out a specific agent. When the same tool appears in both `defaults.tools` and an agent's `tools` with inline overrides, the per-agent overrides take priority, with non-overlapping keys merged from both. See [Per-Agent Tool Configuration](https://docs.mindroom.chat/configuration/agents/#per-agent-tool-configuration) for the full override syntax and merge order. Configured MCP servers also appear here as dynamic tools named `mcp_<server_id>`. See [MCP](https://docs.mindroom.chat/mcp/index.md) for the `mcp_servers` config and naming rules.

## Browse By Topic

- [Execution & Coding](https://docs.mindroom.chat/tools/execution-and-coding/index.md) - Local files, shell, Python, coding helpers, and worker-routed execution tools.
- [Data & Databases](https://docs.mindroom.chat/tools/data-and-databases/index.md) - SQL, databases, spreadsheets, tabular analysis, and financial/business datasets.
- [Web Search](https://docs.mindroom.chat/tools/web-search/index.md) - Search engines and search APIs.
- [Web Scraping & Browser](https://docs.mindroom.chat/tools/web-scraping-and-browser/index.md) - Crawlers, extractors, browser automation, and page-reading tools.
- [Research Sources](https://docs.mindroom.chat/tools/research-sources/index.md) - ArXiv, Wikipedia, PubMed, and Hacker News.
- [AI & Generation](https://docs.mindroom.chat/tools/ai-and-generation/index.md) - Image, video, speech, and transcription APIs.
- [Media & Content](https://docs.mindroom.chat/tools/media-and-content/index.md) - Media processing, brand/media retrieval, and Spotify.
- [Matrix & Attachments](https://docs.mindroom.chat/tools/matrix-and-attachments/index.md) - Matrix-native messaging, thread summaries and resolution, and attachment-aware workflows.
- [Messaging & Social](https://docs.mindroom.chat/tools/messaging-and-social/index.md) - Email, chat, and social/community integrations.
- [Project Management](https://docs.mindroom.chat/tools/project-management/index.md) - Git hosting, issue trackers, docs platforms, and task managers.
- [Calendar & Scheduling](https://docs.mindroom.chat/tools/calendar-and-scheduling/index.md) - Calendar APIs and MindRoom scheduling tools.
- [Memory & Storage](https://docs.mindroom.chat/tools/memory-and-storage/index.md) - Explicit memory tools and external memory providers.
- [Agent Orchestration](https://docs.mindroom.chat/tools/agent-orchestration/index.md) - Subagents, delegation, config tools, OpenClaw compatibility, and Claude Agent sessions.
- [Automation & Platforms](https://docs.mindroom.chat/tools/automation-and-platforms/index.md) - Infrastructure automation, generic APIs, and platform aggregators.
- [Location, Commerce, & Home](https://docs.mindroom.chat/tools/location-commerce-and-home/index.md) - Maps, weather, commerce, and Home Assistant.

## Tool Presets And Implied Tools

Some entries are config-only presets rather than runtime toolkits. `openclaw_compat` expands to a native bundle of MindRoom tools. Some tools also imply companion tools through `Config.IMPLIED_TOOLS`. Today `matrix_message` implies `attachments`, so the effective tool set includes both even when only `matrix_message` is configured explicitly.

## Tool Runtime Context

When a tool runs inside a Matrix-connected agent, it receives a `ToolRuntimeContext` via a context variable. This context carries the current `room_id`, `thread_id`, `requester_id`, `agent_name`, the Matrix client, the active config, and runtime paths. Tools like `matrix_message`, `matrix_room`, `thread_tags`, and `matrix_api` use this context to act on the correct room and thread without the caller passing explicit IDs. `thread_tags` can also target another authorized room, but it still checks the target room's canonical thread root and requester membership before writing the shared tag state. `thread_tags.tag_thread()` and `thread_tags.untag_thread()` still use the active thread when the caller explicitly repeats the current `room_id`. `thread_tags.list_thread_tags()` uses the active thread by default, but passing `room_id` without `thread_id` forces room-wide listing even from inside an active thread. `thread_tags.list_thread_tags(tag=...)` narrows both thread-specific and room-wide responses to the requested tag only. `thread_tags` also validates and normalizes predefined payload schemas for `blocked.data.blocked_by`, `waiting.data.waiting_on`, `priority.data.level`, and `due.data.deadline`. `thread_tags` intentionally replaces the removed experimental `thread_resolution` tool and does not auto-read old `com.mindroom.thread.resolution` markers. `matrix_api` defaults `room_id` to the active room, supports authorized cross-room targeting, and never infers event IDs or state keys from thread context.

## Worker-Routed Execution

Some tools default to running in a sandboxed worker container instead of the primary agent process. The current worker-routed defaults are `file`, `shell`, `python`, and `coding`. Use [Sandbox Proxy Isolation](https://docs.mindroom.chat/deployment/sandbox-proxy/index.md) for deployment details and worker-scope behavior.

## Shared-Only Integrations

Some dashboard integrations are restricted to shared or unscoped execution and cannot be used by agents with isolating worker scopes. The current shared-only integrations are `google`, `spotify`, `homeassistant`, `gmail`, `google_calendar`, `google_sheets`, and all configured `mcp_<server_id>` tools.

## Automatic Dependency Installation

Each tool declares its optional Python dependencies in `pyproject.toml`. When a tool is enabled but its dependencies are missing, MindRoom can auto-install the required extra at runtime. Set `MINDROOM_NO_AUTO_INSTALL_TOOLS=1` to disable that behavior.

## Related Docs

- [MCP](https://docs.mindroom.chat/mcp/index.md) - Configure native MCP client servers and expose them as MindRoom tools.
- [Plugins](https://docs.mindroom.chat/plugins/index.md) - Extend MindRoom with custom tools and skills.
- [Attachments](https://docs.mindroom.chat/attachments/index.md) - Attachment lifecycle and context scoping.
- [Scheduling](https://docs.mindroom.chat/scheduling/index.md) - Chat command scheduling and task behavior.
- [OpenClaw Workspace Import](https://docs.mindroom.chat/openclaw/index.md) - `openclaw_compat` preset and workspace portability.
