---
icon: lucide/plug-2
---

# Plugins

> [!WARNING]
> **Plugins execute arbitrary Python code in the same process as MindRoom.**
> A malicious plugin has full access to your credentials, Matrix sessions, file system, and network.
> Only install plugins you trust and have reviewed.

MindRoom plugins extend agents with custom tools, [hooks](hooks.md), and skills.
A plugin is a directory with a `mindroom.plugin.json` manifest, one or more Python modules, and optionally skill directories.
Plugins are loaded from paths listed under `plugins:` in `config.yaml`.

## Plugin structure

A plugin is a directory containing `mindroom.plugin.json`:

```
my-plugin/
├── mindroom.plugin.json   # Required manifest
├── tools.py               # Tool factories (optional)
├── oauth.py               # OAuth providers (optional)
├── hooks.py               # Event hooks (optional)
└── skills/                # Skill directories (optional)
    └── my-skill/
        └── SKILL.md
```

A plugin must have at least one of `tools_module`, `hooks_module`, `oauth_module`, or `skills`.
A tools-only plugin exposes callable functions to agents.
A plugin with `oauth_module` registers OAuth providers whose state, callbacks, and scoped credential storage are handled by MindRoom core.
A hooks-only plugin observes or transforms events without adding agent-facing tools.
Many plugins combine both.

## Manifest format

The manifest is a JSON file named `mindroom.plugin.json` at the plugin root:

```json
{
  "name": "my-plugin",
  "tools_module": "tools.py",
  "oauth_module": "oauth.py",
  "hooks_module": "hooks.py",
  "skills": ["skills"]
}
```

| Field | Type | Required | Description |
| --- | --- | --- | --- |
| `name` | string | **yes** | Plugin identifier. Must be lowercase ASCII letters, digits, `-`, and `_` only (pattern: `^[a-z0-9_-]+$`). Must be unique across all configured plugins. Invalid or duplicate names abort plugin loading entirely. |
| `tools_module` | string | no | Relative path to the Python module containing `@register_tool_with_metadata` factories. Must exist on disk if declared. |
| `oauth_module` | string | no | Relative path to the Python module containing `register_oauth_providers(settings, runtime_paths)`. Must exist on disk if declared. |
| `hooks_module` | string | no | Relative path to the Python module containing `@hook`-decorated functions. Must exist on disk if declared. |
| `skills` | list of strings | no | Relative directories containing skill subdirectories (each with a `SKILL.md`). Each directory must exist on disk. |

Unknown fields are silently ignored.
Invalid, duplicate, or malformed manifests are configuration errors and stop all plugin loading.
All declared module files and skill directories must exist on disk.

If `hooks_module` is omitted, MindRoom auto-scans `tools_module` for `@hook`-decorated functions.
If both fields point at the same file, MindRoom imports it once and reuses it for both tool registration and hook discovery.

## Configure plugins

Add plugin paths under `plugins:` in `config.yaml`:

```yaml
plugins:
  - ./plugins/my-plugin
  - python:my_skill_pack
  - path: ./plugins/personal-context
    enabled: true
    settings:
      dawarich_url: http://dawarich.local
      api_key: secret
    hooks:
      enrich_with_location:
        priority: 20
      audit_messages:
        enabled: false
```

### Entry formats

Plugin entries can be **strings** (path only) or **objects** (with options).
Both forms can be mixed in the same list.

**String entry** — just the path:

```yaml
plugins:
  - ./plugins/my-plugin
```

**Object entry** — path plus options:

```yaml
plugins:
  - path: ./plugins/my-plugin
    enabled: true
    settings:
      api_key: secret
    hooks:
      my_hook:
        enabled: false
```

| Field | Type | Default | Description |
| --- | --- | --- | --- |
| `path` | string | *required* | Plugin path (see resolution rules below) |
| `enabled` | bool | `true` | Set to `false` to disable the plugin without removing it from the list |
| `settings` | dict | `{}` | Free-form key-value config passed to the plugin at load time |
| `hooks` | dict | `{}` | Per-hook overrides keyed by hook function name |

Each hook override supports:

| Field | Type | Default | Description |
| --- | --- | --- | --- |
| `enabled` | bool | `true` | Disable a specific hook without removing it from the plugin |
| `priority` | int | `null` | Override the hook's default execution priority |
| `timeout_ms` | int | `null` | Override the hook's default timeout |

### Path resolution

Paths are resolved in this order:

1. **Absolute paths** — used as-is
2. **Relative paths** — resolved relative to the directory containing `config.yaml`
3. **Python package specs** — see below

## Python package plugins

MindRoom can resolve plugins from installed Python packages:

```yaml
plugins:
  - my_skill_pack
  - python:my_skill_pack
  - pkg:my_skill_pack:plugins/demo
  - module:my_skill_pack:plugins/demo
```

Rules:

- A bare package name (no slashes, no `.` or `..` prefix) is tried as a Python package first.
- `python:`, `pkg:`, and `module:` are explicit prefixes that force package resolution.
- `:sub/path` after the package name points to a subdirectory inside the package.

MindRoom resolves the package location via `importlib` and looks for `mindroom.plugin.json` in that directory.

## Tools module

A tools module is a Python file that registers one or more tool factories using the `@register_tool_with_metadata` decorator.
Each factory function returns a **Toolkit class** (not an instance).
MindRoom instantiates the class when building agents.

## OAuth providers

An OAuth module registers provider definitions without registering FastAPI routes.
MindRoom core owns state generation, callback consumption, authenticated requester binding, scoped credential writes, status checks, and disconnect handling.
The provider module supplies only provider-specific details such as endpoint URLs, scopes, credential service names, token parsing, optional claim validators, and display metadata.

Declare the module in the manifest:

```json
{
  "name": "drive-plugin",
  "tools_module": "tools.py",
  "oauth_module": "oauth.py"
}
```

Then expose `register_oauth_providers(settings, runtime_paths)`:

```python
from __future__ import annotations

from mindroom.oauth import OAuthProvider


def register_oauth_providers(settings, runtime_paths):
    del runtime_paths
    return [
        OAuthProvider(
            id="acme_drive",
            display_name="Acme Drive",
            authorization_url="https://accounts.acme.example/oauth/authorize",
            token_url="https://accounts.acme.example/oauth/token",
            scopes=("files.read",),
            credential_service="acme_drive",
            client_id_env=settings.get("client_id_env", "ACME_DRIVE_CLIENT_ID"),
            client_secret_env=settings.get("client_secret_env", "ACME_DRIVE_CLIENT_SECRET"),
            redirect_uri_env=settings.get("redirect_uri_env", "ACME_DRIVE_REDIRECT_URI"),
            allowed_email_domains=tuple(settings.get("allowed_email_domains", [])),
            allowed_hosted_domains=tuple(settings.get("allowed_hosted_domains", [])),
        ),
    ]
```

OAuth provider IDs are exposed through `/api/oauth/{provider}/connect`, `/api/oauth/{provider}/authorize`, `/api/oauth/{provider}/callback`, `/api/oauth/{provider}/status`, and `/api/oauth/{provider}/disconnect`.
Dashboard flows normally call `connect` and use the returned provider authorization URL.
Conversation flows should show the browser-openable `authorize` URL, because that URL first authenticates the MindRoom user and then redirects to the external provider.
Conversation-issued links include an opaque connect token so the callback stores credentials in the same worker target the tool was using.
The connect token is also bound to the runtime requester, and redemption fails unless the authenticated dashboard user resolves to that requester.
The callback stores tokens under `credential_service` using the resolved requester and agent execution scope, including private `user` and `user_agent` scopes.
Tokens and client secrets must never be written to `config.yaml`, prompt files, logs, or tool responses.

OAuth-backed tools should set `setup_type=SetupType.OAUTH` and `auth_provider="<provider_id>"` in `@register_tool_with_metadata`.
When credentials are missing, return a concise instruction containing a browser-openable URL built with `mindroom.oauth.build_oauth_connect_instruction(provider, runtime_paths, worker_target=...)`.
The user can complete OAuth and retry the same tool request.

Deployment restrictions belong in plugin settings.
Use `allowed_email_domains` to restrict verified email claims by domain.
Use `allowed_hosted_domains` when the provider supplies a verified hosted-domain claim.
If a configured restriction cannot be checked from verified claims, MindRoom fails the callback closed and does not save credentials.

### Minimal example

```python
from __future__ import annotations

from typing import TYPE_CHECKING

from mindroom.tool_system.metadata import (
    SetupType,
    ToolCategory,
    ToolStatus,
    register_tool_with_metadata,
)

if TYPE_CHECKING:
    from agno.tools import Toolkit


@register_tool_with_metadata(
    name="greeter",
    display_name="Greeter",
    description="A simple greeting tool",
    category=ToolCategory.DEVELOPMENT,
    status=ToolStatus.AVAILABLE,
    setup_type=SetupType.NONE,
)
def greeter_tools() -> type[Toolkit]:
    from agno.tools import Toolkit

    class GreeterTools(Toolkit):
        def __init__(self) -> None:
            super().__init__(name="greeter", tools=[self.greet])

        def greet(self, name: str) -> str:
            """Greet someone by name."""
            return f"Hello, {name}!"

    return GreeterTools
```

After registering the plugin, assign the tool to agents in `config.yaml`:

```yaml
plugins:
  - ./plugins/my-greeter

agents:
  assistant:
    tools:
      - greeter
```

### Decorator fields

All `@register_tool_with_metadata` arguments are keyword-only.

**Required fields:**

| Field | Type | Description |
| --- | --- | --- |
| `name` | string | Tool identifier — used to reference the tool in `config.yaml` agent `tools:` lists |
| `display_name` | string | Human-readable name shown in the dashboard |
| `description` | string | Brief description of what the tool does |
| `category` | `ToolCategory` | Category for dashboard grouping (see values below) |

`ToolCategory` values: `COMMUNICATION`, `DEVELOPMENT`, `EMAIL`, `ENTERTAINMENT`, `INFORMATION`, `INTEGRATIONS`, `PRODUCTIVITY`, `RESEARCH`, `SHOPPING`, `SMART_HOME`, `SOCIAL`.

**Optional fields:**

| Field | Type | Default | Description |
| --- | --- | --- | --- |
| `status` | `ToolStatus` | `AVAILABLE` | `AVAILABLE` or `REQUIRES_CONFIG` — controls whether the tool appears as ready or needs setup in the dashboard |
| `setup_type` | `SetupType` | `NONE` | `NONE`, `API_KEY`, `OAUTH`, or `SPECIAL` — tells the dashboard what kind of setup flow to show |
| `config_fields` | list of `ConfigField` | `None` | Describes constructor parameters configurable through the dashboard (see [ConfigField](#configfield)) |
| `dependencies` | list of strings | `None` | Python packages the tool requires (see [Dependencies](#dependencies)) |
| `docs_url` | string | `None` | Link to external documentation |
| `icon` | string | `None` | Icon name for the dashboard (e.g., `"FaGoogle"`, `"Home"`) |
| `icon_color` | string | `None` | Tailwind color class for the icon (e.g., `"text-blue-500"`) |
| `helper_text` | string | `None` | Markdown help text shown in the dashboard setup panel |
| `auth_provider` | string | `None` | OAuth provider identifier when using OAuth-based setup |
| `managed_init_args` | tuple of `ToolManagedInitArg` | `()` | Declares which MindRoom-managed values the toolkit constructor expects (see [Managed init args](#managed-init-args)) |
| `default_execution_target` | `ToolExecutionTarget` | `PRIMARY` | `PRIMARY` or `WORKER` — controls whether the tool runs on the primary agent or a sandbox worker |

### Dependencies

The `dependencies` field lists Python packages that the tool requires at runtime.
MindRoom checks whether each package is importable before the tool is instantiated.

**For built-in tools** (those shipped inside `src/mindroom/tools/`), missing dependencies trigger automatic installation via `uv sync` or `pip install` using the matching optional extra from MindRoom's `pyproject.toml`.

**For plugin tools**, automatic installation does **not** apply — there is no matching optional extra in MindRoom's package metadata.
If the listed dependencies are not already installed in the environment, MindRoom raises an `ImportError` with a message listing the missing packages.
Plugin authors should document their dependencies in their README so users can install them manually:

```bash
pip install openviking-client aiohttp
```

Even though plugin dependencies are not auto-installed, listing them in the decorator is still useful: MindRoom surfaces a clear error at tool load time rather than failing with an obscure traceback when an agent tries to call the tool mid-conversation.

You can disable automatic dependency installation entirely (for built-in tools too) by setting the environment variable `MINDROOM_NO_AUTO_INSTALL_TOOLS=1`.

### ConfigField

Each `ConfigField` describes one constructor parameter that can be configured through the dashboard or credentials store.

| Field | Type | Default | Description |
| --- | --- | --- | --- |
| `name` | string | *required* | Constructor kwarg name (e.g., `"api_key"`) |
| `label` | string | *required* | Display label shown in the dashboard |
| `type` | string | `"text"` | Input type: `text`, `password`, `url`, `number`, `boolean`, or `select` |
| `required` | bool | `True` | Whether the field must be set before the tool can be used |
| `default` | any | `None` | Default value when not configured |
| `placeholder` | string | `None` | Placeholder text shown in the input |
| `description` | string | `None` | Help text for the field |
| `options` | list | `None` | For `select` type: list of `{"label": "...", "value": "..."}` dicts |
| `validation` | dict | `None` | Optional validation rules (min, max, pattern, etc.) |

Example — a tool that requires an API key:

```python
from mindroom.tool_system.metadata import (
    ConfigField,
    SetupType,
    ToolCategory,
    ToolStatus,
    register_tool_with_metadata,
)

@register_tool_with_metadata(
    name="weather",
    display_name="Weather",
    description="Get current weather data",
    category=ToolCategory.INFORMATION,
    status=ToolStatus.REQUIRES_CONFIG,
    setup_type=SetupType.API_KEY,
    config_fields=[
        ConfigField(name="api_key", label="API Key", type="password"),
        ConfigField(
            name="units",
            label="Units",
            type="select",
            required=False,
            default="metric",
            options=[
                {"label": "Metric (°C)", "value": "metric"},
                {"label": "Imperial (°F)", "value": "imperial"},
            ],
        ),
    ],
)
def weather_tools() -> type[Toolkit]:
    ...
```

### Managed init args

If your toolkit constructor needs MindRoom-managed runtime values, declare them with `managed_init_args`.
MindRoom does **not** auto-detect constructor parameter names — undeclared managed args are not passed through.

| Value | Constructor kwarg | Description |
| --- | --- | --- |
| `RUNTIME_PATHS` | `runtime_paths` | Storage paths, environment values, and data directory access |
| `CREDENTIALS_MANAGER` | `credentials_manager` | Read and write the per-tool credentials store |
| `WORKER_TARGET` | `worker_target` | Resolved worker routing context (scope, execution identity, worker key) |

Example:

```python
from agno.tools import Toolkit
from mindroom.tool_system.metadata import ToolCategory, ToolManagedInitArg, register_tool_with_metadata


@register_tool_with_metadata(
    name="needs_runtime",
    display_name="Needs Runtime",
    description="Example tool that needs runtime paths",
    category=ToolCategory.DEVELOPMENT,
    managed_init_args=(ToolManagedInitArg.RUNTIME_PATHS,),
)
def needs_runtime_tools() -> type[Toolkit]:
    class NeedsRuntimeTools(Toolkit):
        def __init__(self, *, runtime_paths):
            self.runtime_paths = runtime_paths
            super().__init__(name="needs_runtime", tools=[])

    return NeedsRuntimeTools
```

## MCP via plugins (advanced)

MindRoom supports native MCP servers in `config.yaml` — see [MCP](mcp.md) for the normal setup path.
This plugin pattern is still useful when you want a custom wrapper around Agno `MCPTools`:

```python
from agno.tools.mcp import MCPTools
from mindroom.tool_system.metadata import (
    SetupType,
    ToolCategory,
    ToolStatus,
    register_tool_with_metadata,
)


class FilesystemMCPTools(MCPTools):
    def __init__(self, **kwargs):
        super().__init__(
            command="npx -y @modelcontextprotocol/server-filesystem /path/to/dir",
            **kwargs,
        )


@register_tool_with_metadata(
    name="mcp_filesystem",
    display_name="MCP Filesystem",
    description="Tools from an MCP filesystem server",
    category=ToolCategory.DEVELOPMENT,
    status=ToolStatus.AVAILABLE,
    setup_type=SetupType.NONE,
)
def mcp_filesystem_tools():
    return FilesystemMCPTools
```

Reference the plugin and tool in `config.yaml`:

```yaml
plugins:
  - ./plugins/mcp-filesystem

agents:
  assistant:
    tools:
      - mcp_filesystem
```

The factory function must return the toolkit class, not an instance.
MCP toolkits are async; Agno's async agent runs (`arun`, `aprint_response`) handle MCP connect and disconnect automatically.

## Plugin skills

List skill directories in the manifest `skills` array.
Each listed directory is added to MindRoom's skill search roots.
Skill subdirectories must contain a `SKILL.md` file with YAML frontmatter (name, description, requirements).

## Hooks

Plugins can ship typed event hooks for message enrichment, response transformation, lifecycle observation, tool call gating, reactions, schedules, and custom events.
See the [Hooks](hooks.md) page for full documentation including:

- The `@hook` decorator and all parameters
- The built-in events and their execution modes
- The enrichment pipeline (`message:enrich`)
- Custom events
- Error handling without cooldowns or circuit breakers
- Testing patterns

## Live development (hot reload)

Plugins hot-reload automatically.
When you edit any file inside a configured plugin directory, MindRoom notices the change on the next poll, waits out the debounce window, re-imports the plugin's modules in place, swaps the new hooks and tools into the live registry, and the next event invokes your new code.
In practice the new code is usually live about 1-2 seconds after a save.
No service restart and no agent session disruption.

### How it works

- A background watcher polls each configured plugin root every ~1s and debounces saves over a ~1s window.
- On change, the synthetic plugin package subtree is evicted from `sys.modules`, `load_plugins()` re-runs, a fresh `HookRegistry` is built, and the live registry is swapped atomically.
- Module-level `asyncio.Task` objects, and one-level containers like `dict[..., Task]`, on the old module are best-effort cancelled before the swap.
- The watcher ignores `__pycache__/`, `*.pyc`, `*.pyo`, editor swap files (`*.swp`, `*~`, `.#*`, `*.tmp`), and tool caches (`.ruff_cache/`, `.mypy_cache/`, `.pytest_cache/`).

### Iterating on a plugin

```bash
# 1. Edit any file under your plugin
$EDITOR ~/.mindroom/plugins/my-plugin/hooks.py

# 2. Save. Watch the journal:
journalctl -u mindroom.service -f | grep -E 'Reloading plugins|Plugin reload complete'

# 3. Trigger your hook (send a message, fire the matching event, etc.)
#    The new code path is live.
```

You can break and fix a plugin freely.
A broken save that prevents reload, such as an import error or plugin validation error, can deactivate the affected plugin set, and the next valid save reloads it successfully.
A hook that only raises at runtime is different: that failure is logged for that event, and the hook is tried again on the next matching event.
There is no quarantine, failure threshold, or cooldown.
Each save just reloads.

### Manual reload

If you need to force a reload, for example because the watcher missed something or you want to confirm a swap explicitly, an admin user can send the chat command:

```
!reload-plugins
```

The bot replies with the active plugin set and the count of cancelled background tasks.
Admin gating uses `authorization.global_users` from `config.yaml`.

### Caveats and tradeoffs

The hot-reload path is intentionally best-effort, not transactional.

- **In-flight turns keep their old code.** A reload swaps the registry for new events, but any callback already running on the old module finishes there, and only new events use the new module.
- **No partial-write detection.** If your editor saves the file in two writes, the watcher may briefly load the half-written first state, log an import error, and then reload again on the second write.
- **CPU-bound infinite loops still wedge the event loop.** The hook dispatcher uses `asyncio.timeout()` for cooperative cancellation, so truly blocking CPU code is not preempted.
- **Background resources held by the old module can leak until natural cleanup.** Only `asyncio.Task` objects directly attached to module globals are cancelled, so plugins that hold long-lived non-task resources need their own cleanup bookkeeping.
- **New plugins added to disk are not auto-enabled.** You still have to add them under `plugins:` in `config.yaml`, because the watcher only reloads plugins that are already configured.

### Production tip

Hot reload is enabled by default in production.
Edit any configured plugin directory directly while `mindroom.service` is running.
`~/.mindroom/plugins/<name>/` is the common local layout, and active agent sessions, in-flight conversations, and streaming responses continue untouched.

## Community plugins

The [mindroom-ai](https://github.com/mindroom-ai) organization maintains a collection of open-source plugins.
Clone any of them into your plugins directory and add the path to `config.yaml`:

```bash
git clone https://github.com/mindroom-ai/ping-hook-plugin.git ~/.mindroom/plugins/ping-hook
```

```yaml
plugins:
  - ~/.mindroom/plugins/ping-hook
```

### Hooks-only plugins

| Plugin | Description |
| --- | --- |
| [ping-hook-plugin](https://github.com/mindroom-ai/ping-hook-plugin) | Minimal example — responds to `!ping-hook` with a pong message. Good starting point for learning the hook system. |
| [shell-guard-plugin](https://github.com/mindroom-ai/shell-guard-plugin) | Blocks dangerous shell commands (e.g., `systemctl restart mindroom`) via `tool:before_call` gating. |
| [voice-enrich-plugin](https://github.com/mindroom-ai/voice-enrich-plugin) | Injects AI-only metadata when a voice-transcribed message arrives, warning the model about possible transcription errors. |
| [location-enrich-plugin](https://github.com/mindroom-ai/location-enrich-plugin) | Enriches prompts with real-time GPS location from [Dawarich](https://dawarich.app/), including place matching and movement classification. |
| [restart-resume-plugin](https://github.com/mindroom-ai/restart-resume-plugin) | Re-activates threads tagged `pending-restart` after a bot restart. |

### Hooks + tools plugins

| Plugin | Description |
| --- | --- |
| [thread-snooze-plugin](https://github.com/mindroom-ai/thread-snooze-plugin) | Snooze and unsnooze threads — temporarily resolves a thread and wakes it at a specified time. |
| [thread-goal-plugin](https://github.com/mindroom-ai/thread-goal-plugin) | Persistent per-thread goals stored in Matrix room state that survive context compaction and restarts. |
| [workloop-plugin](https://github.com/mindroom-ai/workloop-plugin) | Autonomous work plans with dependencies, priorities, auto-poke, and template-driven task creation. |
| [openviking-plugin](https://github.com/mindroom-ai/openviking-plugin) | Long-term memory via [OpenViking](https://github.com/volcengine/OpenViking) — automatic memory extraction, recall, and compaction archiving. |
