# Plugins

> [!WARNING]
> **Plugins execute arbitrary Python code in the same process as MindRoom.**
> A malicious plugin has full access to your credentials, Matrix sessions, file system, and network.
> Only install plugins you trust and have reviewed.

Plugins extend MindRoom with custom tools, [hooks](https://docs.mindroom.chat/hooks/), OAuth providers, and skills without changing MindRoom itself.
Use one to add an integration MindRoom does not ship, react to or transform events, or connect an additional Google or Atlassian account.
A plugin is a directory with a `mindroom.plugin.json` manifest, loaded from the paths listed under `plugins:` in `config.yaml`.

## Installing plugins

Vendor a plugin from GitHub into `<config dir>/plugins/<repo name>`:

```bash
mindroom plugins install ping-hook-plugin
mindroom plugins install mindroom-ai/ping-hook-plugin@v1.2.0
```

The spec is `NAME`, `OWNER/REPO`, or `OWNER/REPO@REF`.
Bare names install from the `mindroom-ai` organization, and `@REF` pins a branch, tag, or commit; without it the repository's default branch is used.
A plugin is installed only after it passes the [compatibility check](#compatibility-checks).
The installed directory records the repository, requested reference, and exact commit in `.mindroom-plugin.lock.json`.
Installing into an existing directory fails with `Plugin directory already exists: ... Use 'mindroom plugins update' instead.`
Plugin Python dependencies are not installed; the command prints a note when the plugin has a `pyproject.toml`.

Then add the plugin to `config.yaml`:

```yaml
plugins:
  - path: plugins/ping-hook-plugin
```

Update vendored plugins:

```bash
mindroom plugins update ping-hook-plugin
mindroom plugins update --all
mindroom plugins update ping-hook-plugin --ref v1.3.0
```

An update fetches the latest commit of the pinned reference, or repins with `--ref`, and skips plugins already at that commit.
Installing a new commit replaces the whole plugin directory and discards local edits to it, so copy any changes you want to keep first.
A failed update leaves the installed version untouched.
Both commands accept `--path` to select the config file whose directory is used and `--plugins-dir` to override the vendor directory.
Set `GITHUB_TOKEN` to authenticate GitHub requests, which raises API rate limits and allows private repositories.

### Community plugins

The [mindroom-ai](https://github.com/mindroom-ai) organization maintains these open-source plugins.

| Plugin | Provides | Description |
| --- | --- | --- |
| [ping-hook-plugin](https://github.com/mindroom-ai/ping-hook-plugin) | Hooks | Minimal example that answers `!ping-hook` with a pong; a good starting point for learning hooks. |
| [shell-guard-plugin](https://github.com/mindroom-ai/shell-guard-plugin) | Hooks | Blocks dangerous shell commands, such as `systemctl restart mindroom-chat`, through `tool:before_call` gating. |
| [voice-enrich-plugin](https://github.com/mindroom-ai/voice-enrich-plugin) | Hooks | Warns the model about possible transcription errors in voice-transcribed messages. |
| [location-enrich-plugin](https://github.com/mindroom-ai/location-enrich-plugin) | Hooks | Adds real-time GPS location from [Dawarich](https://dawarich.app/) to prompts, with place matching and movement classification. |
| [restart-resume-plugin](https://github.com/mindroom-ai/restart-resume-plugin) | Hooks | Re-activates threads tagged `pending-restart` after a bot restart. |
| [thread-snooze-plugin](https://github.com/mindroom-ai/thread-snooze-plugin) | Hooks and tools | Temporarily resolves a thread and wakes it at a specified time. |
| [thread-goal-plugin](https://github.com/mindroom-ai/thread-goal-plugin) | Hooks and tools | Per-thread goals stored in Matrix room state that survive context compaction and restarts. |
| [workloop-plugin](https://github.com/mindroom-ai/workloop-plugin) | Hooks and tools | External workloop; not needed for per-thread todo plans and auto-poke, which MindRoom provides natively. |
| [openviking-plugin](https://github.com/mindroom-ai/openviking-plugin) | Hooks and tools | Long-term memory through [OpenViking](https://github.com/volcengine/OpenViking) with automatic extraction, recall, and compaction archiving. |

## Configure plugins

List plugins under `plugins:` in `config.yaml`.
An entry is either a path string or an object with options, and both forms can be mixed:

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

| Field | Type | Default | Description |
| --- | --- | --- | --- |
| `path` | string | *required* | Plugin directory or Python package spec (see [Path resolution](#path-resolution)) |
| `enabled` | bool | `true` | Set to `false` to disable the plugin without removing the entry |
| `settings` | dict | `{}` | Free-form values passed to the plugin's hooks and OAuth module; masked as secret in the dashboard |
| `hooks` | dict | `{}` | Per-hook overrides keyed by hook function name |

Each hook override supports:

| Field | Type | Default | Description |
| --- | --- | --- | --- |
| `enabled` | bool | `true` | Set to `false` to disable that hook |
| `priority` | int | `null` | Override the hook's execution priority |
| `timeout_ms` | int | `null` | Override the hook's timeout in milliseconds |

Adding, removing, or changing a `plugins:` entry takes effect through config hot reload.

### Path resolution

- Absolute paths and paths starting with `~` are used as-is.
- Relative paths resolve against the directory containing `config.yaml`.
- A bare name without `/` that does not start with `.` uses a matching config-relative directory if one exists, otherwise an importable Python package.

## Python package plugins

Plugins can ship inside installed Python packages:

```yaml
plugins:
  - my_skill_pack
  - python:my_skill_pack
  - pkg:my_skill_pack:plugins/demo
  - module:my_skill_pack:plugins/demo
```

The `python:`, `pkg:`, and `module:` prefixes always resolve a package, and `:sub/path` after the package name points to a subdirectory inside it.
The resolved directory must contain `mindroom.plugin.json`.

## Live development (hot reload)

Plugins hot-reload automatically.
Saving any file inside a configured plugin directory makes the new hooks and tools live for the next event, usually 1-2 seconds after the save.
No service restart or agent session reset is needed.
Edits to `__pycache__/`, `*.pyc`, `*.pyo`, editor swap and backup files (`*.swp`, `*.swo`, `*~`, `.#*`, `*.tmp`), `.git/`, and tool caches (`.ruff_cache/`, `.mypy_cache/`, `.pytest_cache/`) are ignored.

> [!IMPORTANT]
> Every plugin reload, automatic or manual, interrupts all unfinished [background scripts](https://docs.mindroom.chat/tools/background-scripts/), including scripts that do not use the changed plugin.

Watch reloads in the logs:

```bash
journalctl -u mindroom.service -f | grep -E 'Reloading plugins|Plugin reload complete'
```

Keep in mind:

- Only plugins already listed under `plugins:` are watched; a new plugin directory on disk is not loaded until you add it to `config.yaml`.
- Callbacks already running finish on the old code; the new code applies to new events.
- An editor that saves a file in two writes can briefly trigger an import error, followed by a successful reload on the second write.
- Reload cancels `asyncio` tasks the plugin keeps in module-level variables; close connections and other resources yourself.

### Broken plugins

`mindroom run` startup does not crash on a broken plugin.
It logs `Failed to load plugin, skipping` (or `Plugin path does not exist, skipping`) and disables that plugin's tools.
If a broken tools module hides which tools it would have registered, unknown tool names in agent configs are also disabled with a warning.
Two plugins whose manifests share a `name` stop the whole configured plugin set from loading.
`mindroom config validate` and `mindroom plugins check` report the same problems as errors instead.

When a save breaks a plugin during hot reload, MindRoom logs `Plugin reload failed; active plugin set degraded` and keeps the other plugins active, or logs `Plugin reload failed; all plugins deactivated` when no valid set remains.
The next valid save reloads normally.
For hooks that raise while handling an event, see [Errors and timeouts](https://docs.mindroom.chat/hooks/#errors-and-timeouts).

MindRoom logs `Loading non-bundled plugin` once for each plugin outside the MindRoom source tree; this is informational.

### `!reload-plugins`

Force-reload every configured plugin from disk, for example when the watcher missed a change:

```
!reload-plugins
```

The reply lists the active plugins and the number of cancelled background tasks:

```
✅ Reloaded N plugins; cancelled K tasks; active: <plugin names>
```

Only users listed in `administrators` in `config.yaml` may run it; others get `❌ Admin only.`
A failed reload replies `❌ Plugin reload failed: <error>`.
`!reload_plugins` is an alias.

## Compatibility checks

Validate a plugin against the installed MindRoom version before deployment:

```bash
mindroom plugins check ./my-plugin
```

The check strictly validates the manifest, imports declared modules, validates tool, hook, and OAuth registrations, and verifies that declared skill directories exist.
It prints the discovered tools, hooks, and skill directories, and exits nonzero on failure.
It does not parse `SKILL.md` contents or evaluate skill eligibility, and it does not touch your running configuration.

## Plugin structure

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

Only the manifest `name` is required; a plugin may provide any combination of tools, hooks, OAuth providers, and skills.

## Manifest format

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
| `name` | string | **yes** | Plugin identifier using only lowercase ASCII letters, digits, `-`, and `_`; must be unique among configured plugins |
| `tools_module` | string | no | Relative path to the module with `@register_tool_with_metadata` factories |
| `oauth_module` | string | no | Relative path to the module defining `register_oauth_providers(settings, runtime_paths)` |
| `hooks_module` | string | no | Relative path to the module with `@hook` functions; when omitted, `tools_module` is scanned for hooks |
| `skills` | list of strings | no | Relative directories containing skill subdirectories |

Every declared module file and skill directory must exist.
Unknown fields are ignored.
Pointing `tools_module` and `hooks_module` at the same file is allowed.

## Tools module

A tools module registers tool factories with `@register_tool_with_metadata`.
Each factory returns a **Toolkit class**, not an instance, and MindRoom constructs it when building agents.

### Minimal example

```python
from __future__ import annotations

from typing import TYPE_CHECKING

from mindroom.tool_system.declarations import (
    SetupType,
    ToolCategory,
    ToolFileAccess,
    ToolStatus,
)
from mindroom.tool_system.registration import register_tool_with_metadata

if TYPE_CHECKING:
    from agno.tools import Toolkit


@register_tool_with_metadata(
    name="greeter",
    file_access=ToolFileAccess.NONE,
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

Assign the tool to agents like any built-in tool:

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
| `name` | string | Tool identifier used in agent `tools:` lists |
| `display_name` | string | Name shown in the dashboard |
| `description` | string | Brief description of the tool |
| `category` | `ToolCategory` | Dashboard grouping: `COMMUNICATION`, `DEVELOPMENT`, `EMAIL`, `ENTERTAINMENT`, `INFORMATION`, `INTEGRATIONS`, `PRODUCTIVITY`, `RESEARCH`, `SMART_HOME`, or `SOCIAL` |
| `file_access` | `ToolFileAccess` | How the tool reaches local files (see below) |

`ToolFileAccess` values:

- `NONE`: the tool takes no local file paths.
- `AGENT`: the tool resolves every model-supplied path through `mindroom.file_access.resolve_agent_file`, so it follows the agent's `file_access` setting.
- `UNCONFINED`: the tool reaches local files in a way `file_access` does not confine, such as running programs, queries, or model-chosen paths.

See [Security Posture](https://docs.mindroom.chat/architecture/security-posture/#file-access) for how each class is treated.

**Optional fields:**

| Field | Type | Default | Description |
| --- | --- | --- | --- |
| `status` | `ToolStatus` | `AVAILABLE` | `AVAILABLE` or `REQUIRES_CONFIG`; whether the dashboard shows the tool as ready or needing setup |
| `setup_type` | `SetupType` | `NONE` | `NONE`, `API_KEY`, `OAUTH`, or `SPECIAL`; which setup flow the dashboard shows |
| `config_fields` | list of `ConfigField` | `None` | Constructor parameters configurable through the dashboard (see [ConfigField](#configfield)) |
| `agent_override_fields` | list of `ConfigField` | `None` | Constructor parameters an agent can set in its own `tools:` entry but `defaults.tools` cannot |
| `dependencies` | list of strings | `None` | Python packages the tool requires (see [Dependencies](#dependencies)) |
| `docs_url` | string | `None` | Link to external documentation |
| `icon` | string | `None` | Dashboard icon name, such as `"FaGoogle"` or `"Home"` |
| `icon_color` | string | `None` | Tailwind color class for the icon, such as `"text-blue-500"` |
| `helper_text` | string | `None` | Markdown help shown in the dashboard setup panel |
| `auth_provider` | string | `None` | OAuth provider ID for OAuth-backed tools (see [OAuth providers](#oauth-providers)) |
| `oauth_fallback_fields` | tuple of strings | `()` | `config_fields` names that let the dashboard offer manual credentials as an alternative to OAuth and count the tool as configured once they are all set; requires `setup_type=OAUTH`; list token-named fields such as `access_token` in the provider's `tool_config_oauth_fallback_fields` too |
| `managed_init_args` | tuple of `ToolManagedInitArg` | `()` | MindRoom-managed values passed to the constructor (see [Managed init args](#managed-init-args)) |
| `default_execution_target` | `ToolExecutionTarget` | `PRIMARY` | Default location, `PRIMARY` or `WORKER`; worker routing configuration can override it |
| `requires_primary_runtime` | bool | `False` | Never route the tool to a worker, even when `worker_tools` lists it |
| `consumes_workspace_paths` | bool | `False` | The tool opens workspace files by path; when it runs in a worker, attachments are saved to the worker workspace so the tool can open them |
| `requires_room_context` | bool | `False` | The tool needs a live Matrix room, so it is hidden where none exists, such as the MCP gateway, the OpenAI-compatible API, and room-less runs, and it always runs in the primary runtime |
| `executes_code` | bool | `False` | The tool runs arbitrary programs; MindRoom warns at startup when such a tool runs in a worker while the same agent keeps unconfined tools in the primary process |
| `supports_toolkit_filters` | bool | `True` | Accept the [`include_tools` and `exclude_tools`](https://docs.mindroom.chat/tools/#filtering-toolkit-functions) inline overrides |
| `worker_inert_agent_functions` | tuple of strings | `()` | Functions whose injected `agent` parameter is unused and can receive `None` in a worker |

Set `requires_primary_runtime=True` when the toolkit depends on primary-process services, keeps state or an open resource between calls, or reads or changes an injected `Agent`, `Team`, or `RunContext`.
A worker builds a fresh toolkit for every call and accepts only JSON values and paths as arguments.
If an SDK function accepts an `agent` parameter but never uses it, list it in `worker_inert_agent_functions` instead; never do this for functions that depend on agent identity, configuration, or session state.
`requires_room_context` and `requires_primary_runtime` describe where a tool can run; they do not grant or replace tool authorization.

### Dependencies

Plugin tool dependencies are never installed automatically.
When a listed package is missing, the tool fails to load with `Missing dependencies for tool '<name>': <packages>` instead of failing mid-conversation.
Document the dependencies in the plugin README so users can install them, for example `pip install openviking-client aiohttp`.
Built-in tools install their own dependencies as described in [Automatic Dependency Installation](https://docs.mindroom.chat/tools/#automatic-dependency-installation).

### ConfigField

Each `ConfigField` describes one constructor parameter that users can set through the dashboard or credentials store.

| Field | Type | Default | Description |
| --- | --- | --- | --- |
| `name` | string | *required* | Constructor keyword argument, such as `"api_key"` |
| `label` | string | *required* | Label shown in the dashboard |
| `type` | string | `"text"` | `text`, `password`, `url`, `number`, `boolean`, `select`, or `string[]` |
| `required` | bool | `True` | Whether the field must be set before the tool can be used |
| `default` | any | `None` | Initial value in the dashboard form |
| `placeholder` | string | `None` | Placeholder text in the input |
| `description` | string | `None` | Help text for the field |
| `options` | list | `None` | For `select`: a list of `{"label": "...", "value": "..."}` dicts |
| `validation` | dict | `None` | Validation rules such as min, max, or pattern |
| `authored_override` | bool | `True` | Set to `False` to forbid setting the field inline in `config.yaml`; `password` fields are never allowed inline (see [Per-Agent Tool Configuration](https://docs.mindroom.chat/tools/#per-agent-tool-configuration)) |

A `ConfigField` `default` only pre-fills the dashboard form and is not passed to the constructor.
When no value is stored or configured, the constructor's Python default applies, so give optional parameters matching defaults, such as `units: str = "metric"` below.

```python
from mindroom.tool_system.declarations import (
    ConfigField,
    SetupType,
    ToolCategory,
    ToolFileAccess,
    ToolStatus,
)
from mindroom.tool_system.registration import register_tool_with_metadata

@register_tool_with_metadata(
    name="weather",
    file_access=ToolFileAccess.NONE,
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

Declare MindRoom-managed constructor values with `managed_init_args`.
Only declared values are passed; MindRoom does not detect them from parameter names.

| Value | Constructor kwarg | Description |
| --- | --- | --- |
| `RUNTIME_PATHS` | `runtime_paths` | Storage paths, environment values, and data directory access |
| `CREDENTIALS_MANAGER` | `credentials_manager` | Read and write the per-tool credentials store |
| `RUNTIME_CONFIG` | `runtime_config` | The active MindRoom config, when available |
| `AGENT_NAME` | `agent_name` | The constructing agent's name, when available |
| `FILE_ACCESS` | `file_access` | The constructing agent's effective `file_access` setting |
| `WORKER_TARGET` | `worker_target` | Resolved worker routing context (scope, execution identity, worker key) |
| `TOOL_OUTPUT_WORKSPACE_ROOT` | `tool_output_workspace_root` | Workspace root used for managed tool-output saves |
| `WORKER_TOOLS_OVERRIDE` | `worker_tools_override` | Effective worker-routed tool override |
| `CURRENT_ROOM_ID` | `current_room_id` | Active Matrix room ID when available |
| `AGENT_STATE_ROOT` | `agent_state_root` | The constructing agent's state root in the primary runtime, requester-scoped for private agents; `None` inside workers |

```python
from agno.tools import Toolkit
from mindroom.tool_system.declarations import ToolCategory, ToolFileAccess, ToolManagedInitArg
from mindroom.tool_system.registration import register_tool_with_metadata


@register_tool_with_metadata(
    name="needs_runtime",
    file_access=ToolFileAccess.NONE,
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

### Tool runtime context

Inside a Matrix-connected agent run, `mindroom.tool_system.runtime_context.get_tool_runtime_context()` returns the current `ToolRuntimeContext`, or `None` outside one.
It carries `room_id`, `thread_id`, `resolved_thread_id`, `requester_id`, `agent_name`, the Matrix client, the active config, and runtime paths.
`thread_id` is the thread the inbound message named, while `resolved_thread_id` is the conversation thread after plain replies are resolved to their thread.

### Dispatch-time worker targets

A tool that builds other registered tools while handling a call, for example a sub-toolkit from `get_tool_by_name`, should use the same worker target as agent toolkit construction so scoped credentials and OAuth MCP sessions resolve identically:

```python
from mindroom.tool_system.runtime_context import get_tool_runtime_context

context = get_tool_runtime_context()
worker_target = context.resolve_worker_target()
```

`resolve_worker_target()` raises `ValueError` in team and router dispatches, which have no single agent scope; catch it if the tool can run inside a team.

## OAuth providers

An OAuth module defines providers, and MindRoom core handles the connect, callback, credential storage, status, and disconnect flows described in the [OAuth framework](https://docs.mindroom.chat/oauth-framework/#oauth-framework).
Declare the module in the manifest:

```json
{
  "name": "drive-plugin",
  "tools_module": "tools.py",
  "oauth_module": "oauth.py"
}
```

Then define `register_oauth_providers(settings, runtime_paths)`, which receives the plugin entry's `settings`:

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
            credential_service="acme_drive_oauth",
            tool_config_service="acme_drive",
            client_config_services=(
                settings.get("client_config_service", "acme_drive_oauth_client"),
            ),
            allowed_email_domains=tuple(settings.get("allowed_email_domains", [])),
            allowed_hosted_domains=tuple(settings.get("allowed_hosted_domains", [])),
        ),
    ]
```

The [OAuth framework](https://docs.mindroom.chat/oauth-framework/#oauth-framework) owns the service naming rules and the PKCE option.

- `tool_config_service` is optional and holds the tool's editable dashboard settings, such as size limits or capability toggles, separately from the tokens.
- `allowed_email_domains` and `allowed_hosted_domains` restrict which accounts may connect; read them from plugin `settings` so each deployment sets its own.

Never write tokens or client secrets to `config.yaml`, prompt files, logs, or tool responses.

Providers that publish protected-resource metadata can discover their endpoints and optionally register a client automatically:

```python
from mindroom.oauth import OAuthDiscoveryConfig, OAuthProvider, oauth_runtime_bootstrapper

provider = OAuthProvider(
    id="acme_drive",
    display_name="Acme Drive",
    authorization_url="",
    token_url="",
    scopes=("files.read",),
    credential_service="acme_drive_oauth",
    client_config_services=("acme_drive_oauth_client",),
    token_endpoint_auth_method="none",
    pkce_code_challenge_method="S256",
    runtime_bootstrapper=oauth_runtime_bootstrapper(
        OAuthDiscoveryConfig(
            resource="https://api.acme.example",
            token_endpoint_auth_method="none",
            pkce_code_challenge_method="S256",
        ),
    ),
)
```

OAuth-backed tools set `setup_type=SetupType.OAUTH` and `auth_provider="<provider_id>"`.
When credentials are missing, return an instruction with a browser-openable connect link so the user can connect and retry the request.
Build the link with `oauth_connect_url(provider, runtime_paths, worker_target=...)` and the message with `build_oauth_connect_instruction(provider, connect_url)`, both from `mindroom.oauth.service`.

### Additional Google workspaces

Use `GoogleWorkspaceConfig` to add another Google account alongside the built-in Google tools.
Each workspace gets its own OAuth providers, stored connections, Connections page cards, and prefixed tool and function names, while reusing the built-in Google tool implementations.

One module can register both the tools and the OAuth providers:

```json
{
  "name": "google-workspaces",
  "tools_module": "workspaces.py",
  "oauth_module": "workspaces.py"
}
```

```python
# workspaces.py
from mindroom.tool_system.google_workspaces import (
    GoogleWorkspaceConfig,
    google_workspace_oauth_providers,
    register_google_workspace_tools,
)

WORKSPACES = (
    GoogleWorkspaceConfig(
        name="secondary",
        display_name="Secondary",
        client_config_service="secondary_google_oauth_client",
        allowed_hosted_domains=("secondary.example",),
        services=("gmail",),
    ),
)

for workspace in WORKSPACES:
    register_google_workspace_tools(workspace)


def register_oauth_providers(settings, runtime_paths):
    return tuple(
        provider
        for workspace in WORKSPACES
        for provider in google_workspace_oauth_providers(workspace)
    )
```

| Field | Description |
| --- | --- |
| `name` | Prefix for tool, provider, and function names: a lowercase letter followed by up to 15 lowercase letters, digits, or `_`; keep it stable because it identifies stored connections |
| `display_name` | Label prefix in the dashboard, such as **Secondary Gmail** |
| `client_config_service` | Credential service holding the workspace's OAuth client; must end with `_oauth_client` |
| `allowed_hosted_domains` | At least one Google hosted domain; signing in with a personal account or another organization's account fails without saving credentials |
| `services` | Any of `gmail`, `google_calendar`, `google_drive`, `google_docs`, `google_sheets`, and `google_tasks`, without duplicates; defaults to all six |

To set up a workspace:

1. Provision its OAuth client through a [shared credential seed](https://docs.mindroom.chat/oauth-framework/#credential-seeds) with `client_id` and `client_secret` under the `client_config_service`.
2. For each service, enable the matching Google API and register its callback on that client, such as `https://your-host/api/oauth/secondary_google_gmail/callback`.
3. Enable the plugin and add the prefixed tools, such as `secondary_gmail` beside `gmail`, to the agent's tools.

When a workspace tool needs a login, it returns its own chat authorization link.
Workspace tools never fall back to the default Google client or a global service account, and like the built-in Google tools they always run in the primary runtime.
Functions are prefixed, such as `secondary_get_latest_emails` and `secondary_send_email`, so approval rules, script-tool allowlists, and function filters must use those names; a rule for `send_email` does not match `secondary_send_email`.

### Google Cloud tools

A plugin tool that calls Google Cloud client libraries, such as `google-cloud-storage`, can reuse the built-in read-only **Google Cloud** connection instead of defining its own OAuth provider.
Users connect once for every Google Cloud tool, the connection requests the `cloud-platform.read-only` scope, and a configured `GOOGLE_SERVICE_ACCOUNT_FILE` is used instead and counts as connected.
See [Google Services OAuth](https://docs.mindroom.chat/deployment/google-services-oauth/) for the client setup, scope, and account restrictions.

Subclass `GoogleCloudToolkit`, set `_oauth_provider` and `_oauth_tool_name`, and pass the managed constructor values through:

```python
import json

from mindroom.custom_tools.google_service import GoogleCloudToolkit
from mindroom.oauth.google_cloud import google_cloud_oauth_provider


class CloudStorageBrowserTools(GoogleCloudToolkit):
    _oauth_provider = google_cloud_oauth_provider()
    _oauth_tool_name = "cloud_storage_browser"

    def __init__(self, *, project, runtime_paths, credentials_manager, worker_target, runtime_config, **kwargs):
        self.project = project
        super().__init__(
            name="cloud_storage_browser",
            tools=[self.list_buckets],
            runtime_paths=runtime_paths,
            credentials_manager=credentials_manager,
            worker_target=worker_target,
            runtime_config=runtime_config,
            **kwargs,
        )

    def list_buckets(self) -> str:
        """List the Cloud Storage buckets in the configured project."""
        from google.api_core.exceptions import GoogleAPICallError
        from google.cloud import storage

        try:
            client = self._google_cloud_client(
                "storage",
                lambda creds: storage.Client(project=self.project, credentials=creds),
            )
            return json.dumps({"buckets": [bucket.name for bucket in client.list_buckets()]})
        except GoogleAPICallError as exc:
            return self._google_cloud_error_result("Cloud Storage", "list_buckets", exc)
```

Register the tool in the same module with an OAuth setup, the Google Cloud provider, the primary runtime, and the four managed init args that the constructor receives:

```python
from mindroom.tool_system.declarations import (
    ConfigField,
    SetupType,
    ToolCategory,
    ToolFileAccess,
    ToolManagedInitArg,
    ToolStatus,
)
from mindroom.tool_system.registration import register_tool_with_metadata


@register_tool_with_metadata(
    name="cloud_storage_browser",
    display_name="Cloud Storage Browser",
    description="List Google Cloud Storage buckets",
    category=ToolCategory.DEVELOPMENT,
    status=ToolStatus.REQUIRES_CONFIG,
    file_access=ToolFileAccess.NONE,
    setup_type=SetupType.OAUTH,
    auth_provider="google_cloud",
    requires_primary_runtime=True,
    config_fields=[ConfigField(name="project", label="Project", type="text", required=True)],
    managed_init_args=(
        ToolManagedInitArg.RUNTIME_PATHS,
        ToolManagedInitArg.CREDENTIALS_MANAGER,
        ToolManagedInitArg.WORKER_TARGET,
        ToolManagedInitArg.RUNTIME_CONFIG,
    ),
    dependencies=["google-cloud-storage"],
)
def cloud_storage_browser_tools() -> type[CloudStorageBrowserTools]:
    return CloudStorageBrowserTools
```

The base class provides three helpers:

| Method | What it does |
| --- | --- |
| `self._google_cloud_credentials()` | Returns the requester's credentials, or the service account's; when the account is not connected, the tool call returns the standard connect prompt instead |
| `self._google_cloud_client(name, factory)` | Returns one client per worker thread, built by `factory(credentials)` and rebuilt whenever the credentials change |
| `self._google_cloud_error_result(service_name, operation, exc)` | Returns a JSON error that exposes only the HTTP status, and turns an HTTP 401 into a reconnect prompt |

The plugin must list and install its own client-library dependencies, because MindRoom does not ship them; see [Dependencies](#dependencies).
Build clients only from the credentials the helpers return, and do not copy them with `with_quota_project`, because the copy no longer refreshes through the stored connection.

### Additional Atlassian connections

Use `AtlassianConnectionConfig` the same way to add another Atlassian Cloud site alongside the built-in `atlassian` tool.
See [Atlassian Cloud](https://docs.mindroom.chat/tools/atlassian/#add-more-connections) for the plugin example, fields, and callback setup.

## MCP via plugins (advanced)

Configure MCP servers directly in `config.yaml` as described in [MCP](https://docs.mindroom.chat/mcp/).
Use a plugin only when you need a custom wrapper around Agno `MCPTools`:

```python
from agno.tools.mcp import MCPTools
from mindroom.tool_system.declarations import (
    SetupType,
    ToolCategory,
    ToolFileAccess,
    ToolStatus,
)
from mindroom.tool_system.registration import register_tool_with_metadata


class FilesystemMCPTools(MCPTools):
    def __init__(self, **kwargs):
        super().__init__(
            command="npx -y @modelcontextprotocol/server-filesystem /path/to/dir",
            **kwargs,
        )


@register_tool_with_metadata(
    name="mcp_filesystem",
    # The filesystem server reads local paths that file_access cannot confine.
    file_access=ToolFileAccess.UNCONFINED,
    display_name="MCP Filesystem",
    description="Tools from an MCP filesystem server",
    category=ToolCategory.DEVELOPMENT,
    status=ToolStatus.AVAILABLE,
    setup_type=SetupType.NONE,
)
def mcp_filesystem_tools():
    return FilesystemMCPTools
```

Add the plugin and assign `mcp_filesystem` to agents as in the [minimal example](#minimal-example).
MindRoom connects and disconnects the MCP session around each agent run.

## Plugin skills

Each directory listed in the manifest `skills` array becomes a skill search root, and each skill subdirectory needs a `SKILL.md`.
See [Skills](https://docs.mindroom.chat/skills/) for the `SKILL.md` format, precedence, and the per-agent `skills:` allowlist that plugin skills also require.

## Hooks

Plugins can ship typed event hooks for message enrichment, response transformation, lifecycle observation, tool-call gating, reactions, schedules, and custom events.
See [Hooks](https://docs.mindroom.chat/hooks/) for the `@hook` decorator, events, execution modes, timeouts and errors, the hook context and Matrix helpers, and testing.
