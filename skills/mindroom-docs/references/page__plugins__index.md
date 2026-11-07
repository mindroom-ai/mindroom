# Plugins

> [!WARNING]
> **Plugins execute arbitrary Python code in the same process as MindRoom.**
> A malicious plugin has full access to your credentials, Matrix sessions, file system, and network.
> Only install plugins you trust and have reviewed.

Plugins extend MindRoom with custom tools, [hooks](https://docs.mindroom.chat/hooks/), [automations](#automations), OAuth providers, and skills without changing MindRoom itself.
Use one to add an integration MindRoom does not ship, react to or transform events, run agent maintenance on a schedule, or connect an additional Google or Atlassian account.
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
| [response-audit-plugin](https://github.com/mindroom-ai/response-audit-plugin) | Hooks | Audits delivered answers with a [judgment backend](https://docs.mindroom.chat/configuration/threads/#judgment-backends) and asks the agent to verify facts that no lookup supported. |
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
| `settings` | dict | `{}` | Free-form values passed to the plugin's hooks, automations, and OAuth module; masked as secret in the dashboard |
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
Agents keep a disabled tool in their config, including the tools a generated agent's owner chose, and offer it again once the plugin loads.
Two plugins whose manifests share a `name` stop the whole configured plugin set from loading.
`mindroom config validate` and `mindroom plugins check` report the same problems as errors instead.

When a save breaks a plugin during hot reload, MindRoom skips that plugin the same way and keeps the other plugins active.
When the configured plugins cannot load together, such as two plugins sharing a `name`, MindRoom logs `Plugin reload failed; the previous plugins stay active` and keeps the previous plugins with their tools and hooks.
The next valid save reloads normally.
For hooks that raise while handling an event, see [Errors and timeouts](https://docs.mindroom.chat/hooks/#errors-and-timeouts).

MindRoom logs `Loading non-bundled plugin` once for each plugin outside the MindRoom source tree; this is informational.

### `!reload-plugins`

Force-reload every configured plugin from disk, for example when the watcher missed a change:

```
!reload-plugins
```

The reply lists the active plugins, the number of cancelled background tasks, and any plugin that failed to load:

```
✅ Reloaded N plugins; cancelled K tasks; active: <plugin names>; skipped: <plugin path> (<error>)
```

Only users listed in `administrators` in `config.yaml` may run it; others get `❌ Admin only.`
A reload whose plugins cannot load together keeps the previous plugins and replies `❌ Plugin reload failed: <error>`.
`!reload_plugins` is an alias.

## Compatibility checks

Validate a plugin against the installed MindRoom version before deployment:

```bash
mindroom plugins check ./my-plugin
mindroom plugins check pkg:my_skill_pack:plugins/demo
```

The argument takes every form a `plugins:` entry takes, as listed in [Path resolution](#path-resolution) and [Python package plugins](#python-package-plugins), and relative paths resolve against the current directory instead of the config directory.
The check strictly validates the manifest, imports declared modules, validates tool, hook, automation, and OAuth registrations, rejects automation names that are built-in or registered twice, and verifies that declared skill directories exist.
It prints the discovered tools, hooks, automations, and skill directories, and exits nonzero on failure.
It also warns about each `import` statement of a `mindroom` module or name outside the [plugin API](#plugin-structure) in the plugin's Python files, with its file and line, even when the plugin fails to load.
Pass `--strict` to make these warnings errors that fail the check, for example in CI.
With `--json`, it prints only one JSON document on stdout and keeps the same exit codes: `status` is `compatible`, with `name` and the `tools`, `hooks`, `automations`, and `skills` lists, or `failed`, with the error in `detail`.
The document also lists the imports outside the plugin API in `internal_imports`, each with its `path`, `line`, and `name`, and with `--strict` any of them makes the `status` `failed`.
It does not parse `SKILL.md` contents or evaluate skill eligibility, and it does not touch your running configuration.

## Plugin structure

```
my-plugin/
├── mindroom.plugin.json   # Required manifest
├── tools.py               # Tool factories (optional)
├── oauth.py               # OAuth providers (optional)
├── routes.py              # HTTP routes (optional)
├── hooks.py               # Event hooks and automations (optional)
└── skills/                # Skill directories (optional)
    └── my-skill/
        └── SKILL.md
```

Only the manifest `name` is required; a plugin may provide any combination of tools, hooks, automations, OAuth providers, HTTP routes, and skills.
Import MindRoom's plugin entry points from `mindroom.plugin_api`; only `GoogleCloudToolkit`, the memory helpers, and the HTTP route helpers stay in the modules their sections name.
Other `mindroom` modules can change in any release.

## Manifest format

```json
{
  "name": "my-plugin",
  "tools_module": "tools.py",
  "oauth_module": "oauth.py",
  "hooks_module": "hooks.py",
  "routes_module": "routes.py",
  "skills": ["skills"],
  "reserved_namespaces": ["com.example.my_plugin."]
}
```

| Field | Type | Required | Description |
| --- | --- | --- | --- |
| `name` | string | **yes** | Plugin identifier using only lowercase ASCII letters, digits, `-`, and `_`; must be unique among configured plugins |
| `tools_module` | string | no | Relative path to the module with `@register_tool_with_metadata` factories |
| `oauth_module` | string | no | Relative path to the module defining `register_oauth_providers(settings, runtime_paths)` |
| `hooks_module` | string | no | Relative path to the module with `@hook` functions and `@automation` checks; when omitted, `tools_module` is scanned for them |
| `routes_module` | string | no | Relative path to the module defining `router`, a FastAPI `APIRouter` served under `/api/plugins/<name>/`; see [HTTP routes](#http-routes) |
| `skills` | list of strings | no | Relative directories containing skill subdirectories |
| `reserved_namespaces` | list of non-empty strings | no | Prefixes of the Matrix event types and content keys that only the plugin writes; see [Reserved namespaces](#reserved-namespaces) |

Every declared module file and skill directory must exist.
Unknown fields are ignored.
Pointing `tools_module` and `hooks_module` at the same file is allowed.

## Tools module

A tools module registers tool factories with `@register_tool_with_metadata`.
Each factory returns a toolkit class, not an instance, and MindRoom constructs it when building agents.

### Minimal example

```python
from mindroom.plugin_api import (
    SetupType,
    ToolCategory,
    ToolFileAccess,
    Toolkit,
    ToolStatus,
    register_tool_with_metadata,
    tool,
)


class GreeterTools(Toolkit):
    name = "greeter"
    instructions = "Greet people by the name they gave."

    @tool
    def greet(self, name: str) -> str:
        """Greet someone by name.

        Args:
            name: The person's name.
        """
        return f"Hello, {name}!"


@register_tool_with_metadata(
    name="greeter",
    file_access=ToolFileAccess.NONE,
    display_name="Greeter",
    description="A simple greeting tool",
    category=ToolCategory.DEVELOPMENT,
    status=ToolStatus.AVAILABLE,
    setup_type=SetupType.NONE,
)
def greeter_tools() -> type[GreeterTools]:
    return GreeterTools
```

Each `@tool` method, synchronous or `async`, is a function the model can call, and `@tool(name="...")` offers it under another name.
The model reads its docstring as its description, its `Args:` entries as parameter descriptions, and its annotations as parameter types.
A `@tool` method receives only the model's arguments, so MindRoom refuses one that takes `agent`, `team`, or `run_context`; read the call through `current_call()` instead.
Annotations are read when an agent is built, so import the types they name at runtime, not only under `TYPE_CHECKING`.
An agent leaves out a toolkit with a refused parameter or an annotation that does not resolve, and logs `Could not load tool for agent construction`.
`mindroom plugins check` reports both for the `@tool` methods of a `mindroom.plugin_api.Toolkit` class, naming the tool and function.
`instructions` is guidance on the toolkit as a whole: it appears in the system prompt of every agent that offers one of the toolkit's functions.
A factory may also return an Agno toolkit class, for example to offer one of Agno's own toolkits.

A `str` parameter receives the model's string unchanged, and a `Literal[...]` parameter each of its choices, so a choice may be named `none` or `true`.
A parameter with a default, such as `title: str | None = None`, receives a model's `"none"` or `"null"`, in any case, as `None` unless that exact string is one of its choices.
Other parameters receive a model's `"none"`, `"null"`, `"true"`, or `"false"` string, in any case, as `None` or a boolean.

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
- `AGENT`: the tool reads and writes every model-supplied path through `resolve_agent_file` and `write_agent_file` (see [Agent files](#agent-files)), so it follows the agent's `file_access` setting.
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
| `requires_conversation` | bool | `False` | The tool needs the calling agent's conversation but no room, so it is hidden from the MCP gateway |
| `executes_code` | bool | `False` | The tool runs arbitrary programs; MindRoom warns at startup when such a tool runs in a worker while the same agent keeps unconfined tools in the primary process |
| `supports_toolkit_filters` | bool | `True` | Accept the [`include_tools` and `exclude_tools`](https://docs.mindroom.chat/tools/#filtering-toolkit-functions) inline overrides |
| `worker_inert_agent_functions` | tuple of strings | `()` | Agno toolkit functions whose injected `agent` parameter is unused and can receive `None` in a worker |
| `approval_aliases` | dict of function name to `ApprovalAlias` | `None` | Functions whose calls another function's approval rules judge too (see [Approval aliases](#approval-aliases)) |

Set `requires_primary_runtime=True` when the toolkit depends on primary-process services, keeps state or an open resource between calls, or needs what [`current_call()` or `get_tool_runtime_context()`](#tool-runtime-context) returns, since both return `None` in a worker.
A worker builds a fresh toolkit for every call and accepts only JSON values and paths as arguments.
An Agno toolkit function that reads its injected `agent`, `team`, or `run_context` needs the primary runtime too.
If it accepts an `agent` parameter but never uses it, list it in `worker_inert_agent_functions` instead; never do this for functions that depend on agent identity, configuration, or session state.
`requires_room_context` and `requires_primary_runtime` describe where a tool can run; they do not grant or replace tool authorization.

### Approval aliases

A function that does what another function does, such as one that types command lines into a shell, can have that function's [approval rules](https://docs.mindroom.chat/tool-approval/) judge its calls too.
Map its name to an `ApprovalAlias` whose `judged_as` names the other function and whose `judge` returns the arguments of each call of that function one call amounts to:

```python
from mindroom.plugin_api import ApprovalAlias, ToolCategory, ToolFileAccess, Toolkit, register_tool_with_metadata, tool


class LineTools(Toolkit):
    name = "lines"

    @tool
    def run_line(self, command: str) -> str:
        """Run one command line in the agent's shell session."""
        ...


def shell_calls(arguments):
    command = arguments.get("command")
    return [{"args": command}] if isinstance(command, str) else None


@register_tool_with_metadata(
    name="lines",
    display_name="Lines",
    description="Run command lines",
    category=ToolCategory.DEVELOPMENT,
    file_access=ToolFileAccess.UNCONFINED,
    executes_code=True,
    approval_aliases={"run_line": ApprovalAlias(judged_as="run_shell_command", judge=shell_calls)},
)
def lines_tools() -> type[LineTools]:
    return LineTools
```

A call then needs approval when its own rules ask for it or when the `judged_as` rules would ask about any call `judge` returns, so a rule that auto-approves `run_line` does not lift a `run_shell_command` rule.
`judge` reads only the call's arguments, since an approval covers exactly those: return an empty list for a call that amounts to no call of `judged_as`, and `None` when the arguments cannot tell, which asks whenever the `judged_as` rules may ask.
The returned calls are judged by the `judged_as` function's own rules, never by an alias of its own, and an alias applies only to the toolkit that declares it.
Each key must name one of the toolkit's functions: a toolkit with an alias for any other name fails to build, and the log names the alias.
When an option can turn an aliased function off, pass every function the toolkit can offer as `function_names` to `register_tool_with_metadata`, so the toolkit still builds without it.
In a [background script](https://docs.mindroom.chat/tools/background-scripts/#which-tools-a-script-can-call-and-when-it-needs-approval) or a [Dynamic Workflow](https://docs.mindroom.chat/tools/agent-orchestration/#allowing-participant-tools), listing the toolkit in `allowed_tools` does not pre-approve the `judged_as` function: unless a toolkit with that function is pre-approved for the same script or participant, a call asks whenever `judge` returns a call that no rule auto-approves or lets through, and a workflow participant gets the aliased function only when it would not ask.

### Dependencies

Plugin tool dependencies are never installed automatically, except for the tools of [additional Google workspaces](#additional-google-workspaces), which install the extra of the built-in tool they reuse, such as `mindroom[gmail]` for `secondary_gmail`.
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
| `env` | tuple of strings | `()` | Environment variables the field falls back to, in order, when nothing else sets it (see [How Settings Combine](https://docs.mindroom.chat/tools/#how-settings-combine)) |

A `ConfigField` `default` only pre-fills the dashboard form and is not passed to the constructor.
When no value is stored or configured, the constructor's Python default applies, so give optional parameters matching defaults, such as `units: str = "metric"` below.

```python
import httpx

from mindroom.plugin_api import (
    ConfigField,
    SetupType,
    ToolCategory,
    ToolFileAccess,
    Toolkit,
    ToolStatus,
    register_tool_with_metadata,
    tool,
)


class WeatherTools(Toolkit):
    name = "weather"

    def __init__(self, *, api_key: str, units: str = "metric") -> None:
        super().__init__()
        self.api_key = api_key
        self.units = units

    @tool
    def current_weather(self, city: str) -> str:
        """Get the current weather in a city."""
        response = httpx.get(
            "https://api.openweathermap.org/data/2.5/weather",
            params={"q": city, "units": self.units, "appid": self.api_key},
            timeout=10,
        )
        return response.text


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
def weather_tools() -> type[WeatherTools]:
    return WeatherTools
```

### Managed init args

Declare MindRoom-managed constructor values with `managed_init_args`.
Only declared values are passed; MindRoom does not detect them from parameter names.

| Value | Constructor kwarg | Description |
| --- | --- | --- |
| `RUNTIME_PATHS` | `runtime_paths` | Storage paths, environment values, and data directory access; in a worker, the worker's own, so a variable set only for the primary reads as unset |
| `CREDENTIALS_MANAGER` | `credentials_manager` | Read and write the per-tool credentials store |
| `RUNTIME_CONFIG` | `runtime_config` | The active MindRoom config, when available |
| `AGENT_NAME` | `agent_name` | The constructing agent's name, when available |
| `FILE_ACCESS` | `file_access` | The constructing agent's effective `file_access` setting |
| `WORKER_TARGET` | `worker_target` | Resolved worker routing context (scope, execution identity, worker key) |
| `TOOL_OUTPUT_WORKSPACE_ROOT` | `tool_output_workspace_root` | The constructing agent's workspace root, where managed tool-output saves go |
| `WORKSPACE_IN_WORKER` | `workspace_in_worker` | Whether the constructing agent's workspace-path tools run in a worker, which is where its attachments are saved |
| `CURRENT_ROOM_ID` | `current_room_id` | Active Matrix room ID when available |
| `AGENT_STATE_ROOT` | `agent_state_root` | The constructing agent's state root in the primary runtime, requester-scoped for private agents; `None` inside workers |
| `SHARED_DIRECTORY_ROOTS` | `shared_directory_roots` | A function that returns the [shared directories](https://docs.mindroom.chat/tools/execution-and-coding/#shared_directories) the tool can reach where it runs, each with its `path` and whether it is `writable`: in the primary process the constructing agent's, or none when tools run in dedicated workers or through the sandbox proxy, and in a worker every directory it mounts (see [Access by deployment](https://docs.mindroom.chat/tools/execution-and-coding/#access-by-deployment)); call it on each use, since the agent can join or leave one while it replies |

```python
from mindroom.plugin_api import (
    ToolCategory,
    ToolFileAccess,
    Toolkit,
    ToolManagedInitArg,
    register_tool_with_metadata,
    tool,
)


class NeedsRuntimeTools(Toolkit):
    name = "needs_runtime"

    def __init__(self, *, runtime_paths) -> None:
        super().__init__()
        self.runtime_paths = runtime_paths

    @tool
    def greeting(self) -> str:
        """Return the greeting set in MindRoom's environment."""
        return self.runtime_paths.env_value("GREETING", default="Hello!")


@register_tool_with_metadata(
    name="needs_runtime",
    file_access=ToolFileAccess.NONE,
    display_name="Needs Runtime",
    description="Example tool that needs runtime paths",
    category=ToolCategory.DEVELOPMENT,
    managed_init_args=(ToolManagedInitArg.RUNTIME_PATHS,),
)
def needs_runtime_tools() -> type[NeedsRuntimeTools]:
    return NeedsRuntimeTools
```

### Agent files

A tool that takes file paths from the model reads them with `resolve_agent_file` and writes them with `write_agent_file` from `mindroom.plugin_api`, passing the values of the `TOOL_OUTPUT_WORKSPACE_ROOT` and `FILE_ACCESS` managed init args.
Both resolve relative paths from the agent's workspace and follow the agent's `file_access`: under `workspace` they raise `ValueError` for a path outside the workspace, and under `unrestricted` they reach any path the tool's process can.
They work the same when the tool runs in a worker, where the workspace is the agent's workspace in that worker.
`resolve_agent_file` returns an existing regular file, which its `open()` opens for reading, and names `field_name` in its errors.
`write_agent_file` creates missing directories, replaces an existing file whole, and returns the path it wrote.

```python
from pathlib import Path
from typing import Literal

from mindroom.plugin_api import (
    ToolCategory,
    ToolFileAccess,
    ToolManagedInitArg,
    Toolkit,
    register_tool_with_metadata,
    resolve_agent_file,
    tool,
    write_agent_file,
)


class NotesTools(Toolkit):
    name = "notes"

    def __init__(
        self,
        *,
        tool_output_workspace_root: Path | None,
        file_access: Literal["workspace", "unrestricted"],
    ) -> None:
        super().__init__()
        self.workspace_root = tool_output_workspace_root
        self.file_access = file_access

    @tool
    def save_note(self, path: str, text: str) -> str:
        """Save a note.

        Args:
            path: Where to save the note, relative to the workspace.
            text: The note.
        """
        written = write_agent_file(path, text.encode(), workspace_root=self.workspace_root, file_access=self.file_access)
        return f"Saved {written}."

    @tool
    def read_note(self, path: str) -> str:
        """Read a note.

        Args:
            path: The note's path, relative to the workspace.
        """
        note = resolve_agent_file(path, workspace_root=self.workspace_root, file_access=self.file_access, field_name="path")
        with note.open() as file:
            return file.read().decode()


@register_tool_with_metadata(
    name="notes",
    file_access=ToolFileAccess.AGENT,
    display_name="Notes",
    description="Save and read notes in the agent workspace",
    category=ToolCategory.PRODUCTIVITY,
    managed_init_args=(ToolManagedInitArg.TOOL_OUTPUT_WORKSPACE_ROOT, ToolManagedInitArg.FILE_ACCESS),
)
def notes_tools() -> type[NotesTools]:
    return NotesTools
```

### Tool runtime context

Inside a Matrix-connected agent run, `mindroom.plugin_api.get_tool_runtime_context()` returns the current `ToolRuntimeContext`, or `None` outside one.
It carries `room_id`, `thread_id`, `resolved_thread_id`, `requester_id`, `agent_name`, the Matrix client, the active config, and runtime paths.
`thread_id` is the thread the inbound message named, while `resolved_thread_id` is the conversation thread after plain replies are resolved to their thread.
`context.execution_identity` names who the call runs for: the agent, inside a team the member that called the tool, the account that answers, the requester, and the conversation.
`mindroom.plugin_api.current_execution_identity()` returns that identity also for calls without a Matrix conversation, such as OpenAI-compatible API and MCP gateway calls, or `None` outside a tool call.
`context.hooks` is a `HookBindings` with the turn's hook capabilities, such as `message_sender`, `room_state_querier`, `room_state_putter`, and `matrix_admin`, each `None` when the turn has none.

Inside a tool call, `mindroom.plugin_api.current_call()` returns a `CurrentCall`, or `None` outside one, such as when code calls the method directly.
It carries the provider's `call_id`, the agent's `tools:` entry that offered the function as `toolkit`, the `session_id`, the `session_state` dict the function may change in place and MindRoom stores after the run, the team's stored conversation as `history_scope` in a team run, and the same `ToolRuntimeContext` as `runtime`.

### Calling a configured model

A tool that runs its own model calls, such as a research loop, builds a provider for a model named in `config.yaml` and sends it one prompt at a time:

```python
from pydantic import BaseModel

from mindroom.plugin_api import (
    ModelScope,
    build_model_provider,
    complete_prompt,
    get_tool_runtime_context,
    resolve_turn_model,
)


class Finding(BaseModel):
    claim: str
    source_url: str


async def find(question: str) -> Finding | str:
    context = get_tool_runtime_context()
    if context is None:
        return "This tool runs only inside an agent's Matrix conversation."
    scope = ModelScope(
        entity=context.agent_name,
        room_id=context.room_id,
        thread_id=context.resolved_thread_id,
        explicit=context.active_model_name,
    )
    provider = build_model_provider(
        context.config,
        context.runtime_paths,
        resolve_turn_model(context.config, context.runtime_paths, scope, None).model_name,
        execution_identity=context.execution_identity,
    )
    result = await complete_prompt(provider, question, instructions=["Cite one source."], schema=Finding)
    return result.content
```

`resolve_turn_model` names the model the calling agent's turn runs: the model the turn is running with (`active_model_name`, such as a scheduled task's model or a switch), else the model chosen for its thread or room.
Inside an agent's reply the active model already reflects the requester's [budget](https://docs.mindroom.chat/budgets/) fallback; where no model is active, `None` as the last argument skips that fallback.
Pass a model name from `config.yaml` to `build_model_provider` instead to call a fixed model.
`build_model_provider` returns a `ModelProvider`, the type to annotate a provider with.
The provider uses the model entry's credentials, base URL, and request settings.
With `schema`, `result.content` is an instance of that model, or the raw text when the answer does not parse; without it, `result.content` is the answer text.
Each call is one request with no tools and no conversation history.

### Fetching public URLs

To download a page or file from a URL a model or user supplied, use `fetch_public_url` from `mindroom.plugin_api` in async code, or `fetch_public_url_sync` in synchronous code that runs off the event loop, such as a synchronous tool:

```python
from mindroom.plugin_api import PublicFetchError, fetch_public_url


async def feed_xml(url: str) -> str:
    try:
        feed = await fetch_public_url(url, max_bytes=1_000_000)
    except PublicFetchError as error:
        return f"Could not read the feed: {error}"
    return feed.content.decode("utf-8", errors="replace")
```

A fetch is a GET that follows up to ten redirects and refuses every hop to a loopback, private, link-local, or cloud-metadata address, including an address a hostname resolves to when it connects.
It sends no credentials, refuses a URL that carries a user name or password, and fails when the whole fetch takes longer than 30 seconds.
It returns a `PublicFetchResult` with the `url` its redirects ended on, the `content_type` header, and the `content` bytes of the 2xx response.
Any failure raises `PublicFetchError`, whose message never contains the URL and whose `code` is `url_not_allowed`, `timeout`, `too_many_redirects`, `too_large` (more than `max_bytes`), `content_encoding` (a compressed or otherwise encoded body), `http_status` (with the response's `status_code`), or `request_failed`.
In a worker whose internet traffic must go through the proxy that `HTTP_PROXY`, `HTTPS_PROXY`, or `ALL_PROXY` names, pass `use_env_proxy=True`, which also honors `NO_PROXY`; that proxy resolves hostnames itself, so it must refuse private addresses as well.

### Dispatch-time worker targets

A tool that builds other registered tools while handling a call, for example a sub-toolkit from `mindroom.plugin_api.get_tool_by_name`, should use the same worker target as agent toolkit construction so scoped credentials and OAuth MCP sessions resolve identically, and the tools the call's config can build:

```python
from mindroom.plugin_api import get_tool_by_name, get_tool_runtime_context

context = get_tool_runtime_context()
toolkit = get_tool_by_name(
    "github",
    context.runtime_paths,
    tools=context.plugins.tools,
    runtime_config=context.config,
    worker_target=context.resolve_worker_target(),
)
```

A plugin tool always runs as an agent, inside a team as the member that called it, so `resolve_worker_target()` returns that agent's target.
A sub-toolkit built with that target runs in a worker when the sandbox settings and the tool's default target route it there; with `runtime_config=context.config` it also does when the agent's explicit `worker_tools` list names it, and a list that leaves it out never moves it to the primary runtime.

Call a sub-toolkit's functions through `toolkit_functions(toolkit)` from `mindroom.plugin_api`, which returns each function by name as an agent's run calls it: with its arguments validated, its output saved to a file when the call asks, and its calls sent to the worker when it runs in one.
A function that runs in a worker is async, so await a function `inspect.iscoroutinefunction` reports and run any other off the event loop, for example with `asyncio.to_thread`.
A tool that calls other tools this way must itself be async.
Calling `toolkit.functions[name]` directly skips all of that: the validation, the output file, and the worker.

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

from mindroom.plugin_api import OAuthProvider


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
from mindroom.plugin_api import OAuthDiscoveryConfig, OAuthProvider, oauth_runtime_bootstrapper

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
Build the link with `oauth_connect_url(provider, runtime_paths, worker_target=...)` and the message with `build_oauth_connect_instruction(provider, connect_url)`, both from `mindroom.plugin_api`.

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
from mindroom.plugin_api import (
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
See [Google Services OAuth](https://docs.mindroom.chat/deployment/google-services-oauth/#providers) for the connection's scope, client setup, service-account option, and account restrictions.

Subclass `GoogleCloudToolkit`, set `_oauth_provider` and `_oauth_tool_name`, and pass the managed constructor values through:

```python
import json

from mindroom.custom_tools.google_service import GoogleCloudToolkit
from mindroom.plugin_api import google_cloud_oauth_provider


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
        from google.api_core.exceptions import GoogleAPIError
        from google.cloud import storage

        try:
            client = self._google_cloud_client(
                "storage",
                lambda creds: storage.Client(project=self.project, credentials=creds),
            )
            return json.dumps({"buckets": [bucket.name for bucket in client.list_buckets()]})
        except GoogleAPIError as exc:
            return self._google_cloud_error_result("Cloud Storage", "list_buckets", exc)
```

Register the tool in the same module with an OAuth setup, the Google Cloud provider, the primary runtime, and the four managed init args that the constructor receives:

```python
from mindroom.plugin_api import (
    ConfigField,
    SetupType,
    ToolCategory,
    ToolFileAccess,
    ToolManagedInitArg,
    ToolStatus,
    register_tool_with_metadata,
)


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
    dependencies=[
        "google-cloud-storage",
        "google-api-python-client",
        "google-auth-httplib2",
    ],
)
def cloud_storage_browser_tools() -> type[CloudStorageBrowserTools]:
    return CloudStorageBrowserTools
```

The base class provides three helpers:

| Method | What it does |
| --- | --- |
| `self._google_cloud_credentials()` | Returns credentials for the requester's connection or the configured service account; when the account is not connected, the tool call returns the standard connect prompt instead |
| `self._google_cloud_client(name, factory)` | Returns a client built by `factory(credentials)` and reuses it only for the same requester's current connection |
| `self._google_cloud_error_result(service_name, operation, exc)` | Returns a JSON error that exposes only the HTTP status, and turns an HTTP 401 into a reconnect prompt |

The plugin must list and install its own client library, such as `google-cloud-storage`, and also the Google packages the base class relies on: `google-api-python-client` and `google-auth-httplib2`.
MindRoom installs them only as extras of its built-in Google tools, so a clean install without them fails to load the tool; see [Dependencies](#dependencies).
Build clients only from the credentials the helpers return, without copies such as `with_quota_project`, and call them from the tool function itself rather than background threads or transfer managers; otherwise an expired or revoked connection returns a generic error instead of a connect prompt.

### Additional Atlassian connections

Use `AtlassianConnectionConfig` the same way to add another Atlassian Cloud site alongside the built-in `atlassian` tool.
See [Atlassian Cloud](https://docs.mindroom.chat/tools/atlassian/#add-more-connections) for the plugin example, fields, and callback setup.

## MCP servers

Configure MCP servers under `mcp_servers:` in `config.yaml` as described in [MCP](https://docs.mindroom.chat/mcp/).
MindRoom does not run plugin toolkits that subclass Agno `MCPTools`: `mindroom plugins check` rejects them once their dependencies are installed, and agents skip them with a warning that names `mcp_servers:`.

## HTTP routes

A plugin serves HTTP routes from the module its manifest names as `routes_module`, which must define a module-level `router`, a FastAPI `APIRouter`.
MindRoom serves that router under `/api/plugins/<name>/` on its API, where `<name>` is the manifest `name`, so this route answers `GET /api/plugins/my-plugin/rooms/<room_id>/hello`:

```python
# routes.py
from typing import Annotated

from fastapi import APIRouter, Depends

from mindroom.plugin_http import ClientUser, require_room_member

router = APIRouter()


@router.get("/rooms/{room_id}/hello")
async def hello(user: Annotated[ClientUser, Depends(require_room_member)]) -> dict[str, str]:
    return {"hello": user.user_id}
```

!!! warning "Plugin routes share the dashboard's origin"
    MindRoom serves plugin routes from the same origin as its dashboard, so script in a page a plugin route returns would run with the dashboard's signed-in session.
    Never return `text/html`, or any other content a browser would render, such as SVG, built from untrusted input such as Matrix messages, room names, or request parameters.
    Return JSON.
    MindRoom answers every response under `/api/plugins/` with `X-Content-Type-Options: nosniff` and `Content-Security-Policy: sandbox; default-src 'none'`, whatever the route sets, so a browser neither guesses another type nor runs script or loads anything in a page a route returns; that is a backstop, not a licence to return pages.

MindRoom builds each plugin's app from its router as the plugin loads, and a reload builds a new one.
A routes module that raises as it loads, has no `router` (`Plugin routes module must define router: APIRouter`), or has a router FastAPI refuses to serve (`Plugin router cannot be served: ...`), such as one with a route at `""` rather than `"/"`, costs the plugin only its routes: MindRoom logs `Plugin routes failed to load; serving none` with the reason, its paths answer 404, and its tools and hooks keep working, after a reload too.
Only `mindroom plugins check` fails on such a routes module; `mindroom config validate` and `mindroom doctor` import it too, but only log it and still pass the config.
Routes follow plugin reloads, so the next request after a reload runs the new code.
A request for a plugin that is not loaded or has no routes, or for a path its router does not define, answers 404.
A route that raises answers 500 with no detail of the error, and the other routes keep serving.
Only the primary serves plugin routes, and workers never import a routes module, so it may import packages that workers lack.
That holds only while the routes module is its own file that the tools and hooks modules do not import, because workers load those.
MindRoom adds no authentication of its own to these routes, so a route without `require_client_user` or `require_room_member` is public.

Each plugin's router runs in that FastAPI app of its own, served under `/api/plugins/<name>/`:

- The router's `lifespan` and `on_event` startup and shutdown handlers never run.
- A `GET` route answers `HEAD` with 405, as in any FastAPI app; list both in `@router.api_route(path, methods=["GET", "HEAD"])` if a client needs `HEAD`.
- The `dependency_overrides` of MindRoom's main app do not reach plugin routes, so a test that overrides a dependency there does not change a plugin route.
- `request.url_for` finds only the plugin's own route names, and `request.base_url` is the plugin's `/api/plugins/<name>/`.

A plugin route that serves a Matrix web client authenticates its caller with `require_client_user` from `mindroom.plugin_http`, as a FastAPI dependency: `user: ClientUser = Depends(require_client_user)`.
`ClientUser.user_id` is the caller's Matrix user ID.
The client signs in by posting a Matrix OpenID token to `POST /api/client-sessions`, which returns `session_token`, `user_id`, and `expires_at` in Unix seconds, and then sends `Authorization: Bearer <session_token>`.
Sign-in needs a homeserver that binds OpenID tokens to this deployment, MindRoom's Tuwunel fork with `mindroom_openid_audience` on (see [Token Binding](https://docs.mindroom.chat/connections-portal/#token-binding)); on any other homeserver `POST /api/client-sessions` answers 403, with no setting to allow it, because another backend the user opens could replay their token here.
Sessions last one hour, and the route answers 401 for a missing, unknown, or expired token; the client then signs in again.
To end its session at once, as at sign-out, the client sends `DELETE /api/client-sessions` with the same header, which answers 204; from then on that token answers 401 like an expired one.
A response a route is already streaming continues after its session expires or is revoked.
Browser origins that may call these routes and `/api/client-sessions` come from `MINDROOM_COMPUTER_ALLOWED_ORIGINS`, the same list that lets MindRoom Chat open [worker computers](https://docs.mindroom.chat/tools/worker-computer/); there is no separate setting for plugin routes.
Every response under `/api/plugins/` carries `Cache-Control: no-store`, whatever the route sets, besides the two headers above, and of the headers beyond those every browser lets pages read, such as `Content-Type`, other origins can read only `WWW-Authenticate`, so a client on another origin cannot read `ETag`, `Content-Disposition`, or `Retry-After`.
A route that acts on one room depends on `require_room_member` instead, as in the example above: `user: ClientUser = Depends(require_room_member)`.
It authenticates the caller as `require_client_user` does and takes the room from the route's `{room_id}` path parameter, and it returns the same `ClientUser` once that user is joined to the room.
It never takes the room from the query string: as a dependency of a route without `{room_id}` in its path, it answers 422.
A route that finds its room some other way awaits `require_room_member(request, room_id)` itself.
Either way the user is always the session's own: no parameter of the request can name another.
It reads live membership through the Matrix client of the router bot if it is in the room, else of any running MindRoom bot in the room, and answers 403 unless the user is joined.
It answers 503 when membership is unavailable: `No running MindRoom bot is in this room.` lasts until a bot joins the room, `Room membership lookup failed; try again.` means the Matrix call failed or timed out, and `Room membership is unavailable.` means the API is served without MindRoom's bots, as outside `mindroom run` or while it stops.

## Plugin skills

Each directory listed in the manifest `skills` array becomes a skill search root, and each skill subdirectory needs a `SKILL.md`.
See [Skills](https://docs.mindroom.chat/skills/) for the `SKILL.md` format, precedence, and the per-agent `skills:` allowlist that plugin skills also require.

## Reserved namespaces

A plugin whose Matrix events clients trust, such as cards it sends, lists their prefixes in the manifest `reserved_namespaces` so agents cannot forge them.
While the plugin is loaded, the `matrix_api` tool refuses an event type starting with one of those prefixes (`Event type '...' is reserved by a plugin and cannot be written by a tool.`) and `content` holding such a key at any depth, as in the `m.new_content` of an edit (`content must not set plugin-reserved keys (...)`).
Every other built-in tool fixes the event type and top-level content keys it sends, and the plugin's own sends are not affected.
An agent whose code execution tools run in the primary process instead of a worker is fully trusted and can still send anything; see [Security posture](https://docs.mindroom.chat/architecture/security-posture/).

## Hooks

Plugins can ship typed event hooks for message enrichment, response transformation, lifecycle observation, tool-call gating, reactions, schedules, and custom events.
See [Hooks](https://docs.mindroom.chat/hooks/) for the `@hook` decorator, events, execution modes, timeouts and errors, the hook context and Matrix helpers, and testing.

## Automations

An automation is a check a plugin runs on each agent's cron schedule; when the check finds work, the agent answers a visible prompt, and code steps decide what happens after each answer.
Use one for recurring agent maintenance whose trigger or follow-up needs code, such as the built-in [`prompt_curation`](https://docs.mindroom.chat/scheduling/#prompt_curation) and [`dreaming`](https://docs.mindroom.chat/scheduling/#dreaming), which use this same API.
Operators enable it per agent under [`automations:`](https://docs.mindroom.chat/scheduling/#automations).

### Minimal example

`mindroom.plugin.json`:

```json
{"name": "weekly-digest", "hooks_module": "hooks.py"}
```

`hooks.py`:

```python
from datetime import UTC, datetime
from functools import partial

from mindroom.plugin_api import Ask, AutomationContext, Done, automation, read_regular_file_within_root


# The digest lives in the agent's file memory, so skip agents without it.
@automation("weekly_digest", requires_file_memory=True)
def check(ctx: AutomationContext) -> Ask | None:
    if not ctx.workspace.is_dir():
        return None
    target = ctx.options.get("target", "digest.md")
    # A stamp only this run asks for tells its result apart from last week's file.
    stamp = f"Digest {datetime.now(UTC):%Y-%m-%d %H:%M}"
    prompt = f"Summarize this week's threads into `{target}`, and end the file with the line `{stamp}`."
    return Ask(prompt, new_thread=True, then=partial(after_run, ctx, target, stamp))


def after_run(ctx: AutomationContext, target: str, stamp: str, config, thread_id: str, timed_out: bool) -> Done:
    # The run ending says nothing about success: check the result before claiming it.
    try:
        written = read_regular_file_within_root(ctx.workspace, target).decode()
    except (OSError, ValueError):
        written = ""
    if stamp not in written:
        return Done(f"⚠️ The digest run ended without writing `{target}`.")
    return Done(f"Digest written to `{target}`.", resolve=(thread_id,))
```

`config.yaml`:

```yaml
plugins:
  - path: plugins/weekly-digest
agents:
  mind:
    display_name: Mind
    memory_backend: file   # weekly_digest requires file memory
    tools: [file]          # the agent writes the digest
    automations:
      - {name: weekly_digest, cron: "0 9 * * 1", options: {target: digest.md}}
```

### The check and its steps

- `@automation(name, requires_file_memory=False)` marks a synchronous check; the name starts with a lowercase letter and continues with lowercase letters, digits, and `_`, must not be `prompt_curation`, `dreaming`, or `exploration`, and must be unique across loaded plugins, or the later registration is ignored with a warning.
  Set `requires_file_memory=True` when the automation maintains file memory, so it never runs for an agent without the file backend.
- The check returns `None` when there is nothing to do, which posts nothing and calls no model, or an `Ask(text, new_thread=..., then=...)` that the agent answers.
- `then(config, thread_id, timed_out)` runs after that answer and returns the next `Ask` or a `Done(notice, resolve=..., on_loop=...)` that posts `notice` in the thread, marks the listed threads resolved, and runs `on_loop` on the event loop.
  Bind anything a step needs from the check with `functools.partial`.
- `timed_out` is true when no answer ended within an hour of the prompt being posted; `False` only means the answer ended, which includes a failed run, so check the result before reporting success.
- The check's `Ask` and every later `Ask` with `new_thread=True` get their own thread and session, even for an agent with `thread_mode: room`; a later `Ask` with `new_thread=False` follows up in the current thread.
  Every thread an automation starts is tagged `automation`; generated topic tags only go on threads without tags, so automation threads get none.
- `Ask(..., new_thread=True, summary="...")` pins that text as the title of the thread the prompt starts, so no generated summary replaces it; without it, the thread gets the usual automatic summary unless `Done` resolves it first.
  `summary` with `new_thread=False` raises `ValueError`, since only the step that starts a thread titles it.

`AutomationContext` fields:

| Field | Description |
| --- | --- |
| `agent_name` | The agent the automation runs for |
| `config` | The config when the automation fired; steps receive the current one |
| `runtime_paths` | Storage and config paths of this MindRoom runtime |
| `entry` | The agent's entry for this automation |
| `options` | The entry's `options`, set per agent, read-only |
| `settings` | The plugin's `settings`, set once per plugin, read-only |
| `requester_id` | For a [private agent](https://docs.mindroom.chat/scheduling/#automations), the requester whose instance this run maintains; `None` for a shared agent |
| `execution_identity` | The identity that resolves that requester's instance, for helpers that take one; `None` for a shared agent |
| `workspace` | The workspace root, which may not exist yet: a shared agent's own workspace, or for a private agent the requester's private root |
| `state_dir` | The automation state directory of the agent, or of the requester's instance, in MindRoom storage outside the workspace; name your files after your automation and create the directory when you first write |

### What MindRoom guarantees

The rules for [scheduling](https://docs.mindroom.chat/scheduling/#automations) and [memory auto-flush](https://docs.mindroom.chat/memory/#file-auto-flush-worker) apply to plugin automations as they do to the built-ins.
For plugin code, MindRoom also guarantees:

- Checks and steps run off the event loop, so ordinary replies and other agents are not held back by them.
- The one-hour limit applies to the agent's answer, not to a check or step that hangs.
- `OSError` and `ValueError` from a check or step post a warning in the room and end the chain; other exceptions are logged.
- `automation_threads(runtime_paths)` from `mindroom.plugin_api` returns the threads automations started, so an automation that reads exported conversations can skip them.
- A chain finishes with the code that started it; after a plugin reload, the next fire uses the new code on the same schedule, but resources the old module kept, such as tasks in module-level variables, may already be cancelled.

### Helpers the built-ins use

Agent workspaces can contain files worker code wrote, so read and write them without following links: `read_regular_file_within_root`, `write_file_within_root`, and `open_directory_within_root` from `mindroom.plugin_api` do that.
To write memory files, use `mindroom.memory.write_scope_markdown_file`, and to make changed memory searchable at once, return `Done(..., on_loop=partial(refresh_agent_memory_search, agent_name, root, config, runtime_paths, ctx.execution_identity))` with `refresh_agent_memory_search` from `mindroom.memory`.
[`prompt_curation.py`](https://github.com/mindroom-ai/mindroom/blob/main/src/mindroom/automations/prompt_curation.py) and [`dreaming.py`](https://github.com/mindroom-ai/mindroom/blob/main/src/mindroom/automations/dreaming.py) are complete examples: a single prompt with a re-check, and a proposal reviewed in a second thread before code applies it.
