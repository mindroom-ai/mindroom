---
icon: lucide/puzzle
---

# Plugins

> [!WARNING]
> **Plugins execute arbitrary Python code in the same process as MindRoom.**
> A malicious plugin has full access to your credentials, Matrix sessions, file system, and network.
> Only install plugins you trust and have reviewed.

Plugins extend MindRoom with custom tools, [hooks](hooks.md), [automations](#automations), OAuth providers, and skills without changing MindRoom itself.
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
| [response-audit-plugin](https://github.com/mindroom-ai/response-audit-plugin) | Hooks | Audits delivered answers with a [judgment backend](configuration/threads.md#judgment-backends) and asks the agent to verify facts that no lookup supported. |
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
| `required` | bool | `false` | Set to `true` to make startup fail, and a reload be refused, when the plugin is skipped for any reason, instead of running without it; see [Broken plugins](#broken-plugins) |
| `settings` | dict | `{}` | Free-form values passed to the plugin's hooks, automations, and OAuth module, and read by its routes and tools through its [plugin context](#plugin-context); masked as secret in the dashboard |
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
In a dotted name such as `python:my_skill_pack.plugins.demo`, each part after the first must be a package directory, or a namespace package directory, inside the one before.
MindRoom finds those parts on disk without running any `__init__.py`, so it does not find a part that only a `pkgutil.extend_path` call or a custom import hook would provide.
The resolved directory must contain `mindroom.plugin.json`.
MindRoom imports a package plugin's modules under their package names, such as `my_skill_pack.plugins.demo.tools`, so loading the plugin runs the `__init__.py` of each package in that name first, as any import of it would; a plugin inside another plugin's directory, such as `plugins/demo` when `my_skill_pack` has its own `mindroom.plugin.json`, keeps names of its own instead.

## Live development (hot reload)

Plugins hot-reload automatically.
Saving any file inside a configured plugin directory makes the new hooks and tools live for the next event, usually 1-2 seconds after the save.
No service restart or agent session reset is needed.
Edits to `__pycache__/`, `*.pyc`, `*.pyo`, editor swap and backup files (`*.swp`, `*.swo`, `*~`, `.#*`, `*.tmp`), `.git/`, and tool caches (`.ruff_cache/`, `.mypy_cache/`, `.pytest_cache/`) are ignored.

> [!IMPORTANT]
> Every plugin reload, automatic or manual, interrupts all unfinished [background scripts](tools/background-scripts.md), including scripts that do not use the changed plugin.

Watch reloads in the logs:

```bash
journalctl -u mindroom.service -f | grep -E 'Reloading plugins|Plugin reload complete'
```

Keep in mind:

- Only plugins already listed under `plugins:` are watched; a new plugin directory on disk is not loaded until you add it to `config.yaml`.
- Callbacks already running finish on the old code; the new code applies to new events.
- An editor that saves a file in two writes can briefly trigger an import error, followed by a successful reload on the second write.
- Reload cancels `asyncio` tasks the plugin keeps in module-level variables, before its old [lifespan](#plugin-lifecycle)'s stop code runs; close connections and other resources in the lifespan, and keep tasks its stop code still needs in the lifespan's own variables or `process_state`.

### Broken plugins

`mindroom run` startup does not crash on a broken plugin unless its entry sets `required: true`, which stops startup with `Required plugin <path> failed to load: <reason>`.
It logs `Failed to load plugin, skipping` (or `Plugin path does not exist, skipping`) and disables that plugin's tools.
Only the primary process enforces `required`; a worker that cannot load the plugin skips it like any other.
If a broken tools module hides which tools it would have registered, unknown tool names in agent configs are also disabled with a warning.
Agents keep a disabled tool in their config, including the tools a generated agent's owner chose, and offer it again once the plugin loads.
Two plugins whose manifests share a `name` stop the whole configured plugin set from loading.
`mindroom config validate` and `mindroom plugins check` report the same problems as errors instead.

When a save breaks a plugin during hot reload, MindRoom skips that plugin the same way and keeps the other plugins active.
When the configured plugins cannot load together, such as two plugins sharing a `name`, or a required plugin would be skipped, MindRoom logs `Plugin reload failed; the previous plugins stay active` and keeps the previous plugins with their tools and hooks.
A config change that would leave a required plugin skipped is refused, and the running config stays.
The next valid save reloads normally.
For hooks that raise while handling an event, see [Errors and timeouts](hooks.md#errors-and-timeouts).

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
A reload whose plugins cannot load together, or that would skip a required plugin, keeps the previous plugins and replies `❌ Plugin reload failed: <error>`.
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
It leaves out the plugin's top-level `tests/` directory, the `conftest.py` and `test_*.py` files beside its manifest unless the manifest names one as a module, and any directory inside the plugin with its own manifest, which is a separate plugin.
It also accepts `mindroom.script_sdk`, the client that [background scripts](tools/background-scripts.md#calling-agent-tools-from-a-script), such as a skill's scripts, call tools with.
Pass `--strict` to make these warnings errors that fail the check, for example in CI.
With `--json`, it prints only one JSON document on stdout and keeps the same exit codes: `status` is `compatible`, with `name` and the `tools`, `hooks`, `automations`, and `skills` lists, or `failed`, with the error in `detail`.
The document also lists the imports outside the plugin API in `internal_imports`, each with its `path`, `line`, and `name`, and with `--strict` any of them makes the `status` `failed`.
It does not parse `SKILL.md` contents or evaluate skill eligibility, and it does not touch your running configuration.

## Plugin API versions

`PLUGIN_API_VERSION` in `mindroom.plugin_api` is the version, as `(major, minor)`, of what this page and [Hooks](hooks.md) let plugins use: the manifest format, and the names they say to import from `mindroom.plugin_api` and the other modules they name.
A change that only adds to the API bumps the minor version, so a plugin written for an earlier minor of the same major keeps working.
A breaking change, one that removes or changes anything a plugin uses, bumps the major version.
Anything a major bump removes or changes is first deprecated for at least 30 days, announced in this section with the date from which it may go.

A plugin states the version it needs as `mindroom_plugin_api` in its manifest, such as `"1.2"` for major 1 at minor 2 or later.
MindRoom checks it before running any of the plugin's code and skips a plugin that needs another major or a later minor, logging `Failed to load plugin, skipping` with a reason such as `Plugin 'my-plugin' needs plugin API 1.3, but this MindRoom provides 1.2: upgrade MindRoom`.
`mindroom plugins check` fails with the same reason, so `mindroom plugins install` and `mindroom plugins update` refuse such a plugin too.
A plugin without `mindroom_plugin_api` loads on every version.

## Plugin structure

```
my-plugin/
├── mindroom.plugin.json   # Required manifest
├── tools.py               # Tool factories (optional)
├── oauth.py               # OAuth providers (optional)
├── routes.py              # HTTP routes (optional)
├── origin.py              # Pages on hosts of the plugin's own (optional)
├── hooks.py               # Event hooks and automations (optional)
├── lifecycle.py           # Start and stop code (optional)
└── skills/                # Skill directories (optional)
    └── my-skill/
        └── SKILL.md
```

Only the manifest `name` is required; a plugin may provide any combination of tools, hooks, automations, OAuth providers, HTTP routes, a lifespan, and skills.
Import MindRoom's plugin entry points from `mindroom.plugin_api`; only `GoogleCloudToolkit`, the memory helpers, the HTTP route helpers, and the [testing kit](#testing-plugins) stay in the modules their sections name.
`mindroom.plugin_api` also exports the types to annotate with: `Config` and `RuntimePaths` for the `config` and `runtime_paths` that hooks, tools, and automations receive, `HookContext` for any hook's context, `HookMessageSender` and `HookMatrixAdmin` for `message_sender` and `matrix_admin`, and `ToolTraceEntry` for the entries of a response draft's `tool_trace`.
Other `mindroom` modules can change in any release.

## Manifest format

```json
{
  "name": "my-plugin",
  "mindroom_plugin_api": "1.30",
  "tools_module": "tools.py",
  "oauth_module": "oauth.py",
  "hooks_module": "hooks.py",
  "routes_module": "routes.py",
  "lifecycle_module": "lifecycle.py",
  "origin_module": "origin.py",
  "skills": ["skills"],
  "reserved_namespaces": ["com.example.my_plugin."]
}
```

| Field | Type | Required | Description |
| --- | --- | --- | --- |
| `name` | string | **yes** | Plugin identifier using only lowercase ASCII letters, digits, `-`, and `_`; must be unique among configured plugins |
| `mindroom_plugin_api` | string | no | Plugin API version the plugin needs, as `"MAJOR.MINOR"`; see [Plugin API versions](#plugin-api-versions) |
| `tools_module` | string | no | Relative path to the module with `@register_tool_with_metadata` factories |
| `oauth_module` | string | no | Relative path to the module defining `register_oauth_providers(settings, runtime_paths)` |
| `hooks_module` | string | no | Relative path to the module with `@hook` functions and `@automation` checks; when omitted, `tools_module` is scanned for them |
| `routes_module` | string | no | Relative path to the module defining `router`, a FastAPI `APIRouter` served under `/api/plugins/<name>/`; see [HTTP routes](#http-routes) |
| `lifecycle_module` | string | no | Relative path to the module defining `lifespan(lifecycle)`, the plugin's start and stop code; see [Plugin lifecycle](#plugin-lifecycle) |
| `origin_module` | string | no | Relative path to the module defining `router`, a FastAPI `APIRouter` served on the plugin's own hosts, and optionally `async def serves(label)`; see [Origins of your own](#origins-of-your-own) |
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

See [Security Posture](architecture/security-posture.md#file-access) for how each class is treated.

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
| `workspace_writers` | tuple of strings | `()` | The functions that change files in the agent's workspace, by writing them or by running code there, which decide `files` as [Workspace Files](workspace-files.md#who-may-open-and-change-a-workspace) describes; a tool that names none counts every function when it sets `executes_code`; a plugin that passes it states `"mindroom_plugin_api": "1.19"` |
| `supports_toolkit_filters` | bool | `True` | Accept the [`include_tools` and `exclude_tools`](tools/index.md#filtering-toolkit-functions) inline overrides |
| `worker_inert_agent_functions` | tuple of strings | `()` | Agno toolkit functions whose injected `agent` parameter is unused and can receive `None` in a worker |
| `approval_aliases` | dict of function name to `ApprovalAlias` | `None` | Functions whose calls another function's approval rules judge too (see [Approval aliases](#approval-aliases)) |

Set `requires_primary_runtime=True` when the toolkit depends on primary-process services, keeps state or an open resource between calls, or needs what [`current_call()` or `get_tool_runtime_context()`](#tool-runtime-context) returns, since both return `None` in a worker.
A worker builds a fresh toolkit for every call and accepts only JSON values and paths as arguments.
An Agno toolkit function that reads its injected `agent`, `team`, or `run_context` needs the primary runtime too.
If it accepts an `agent` parameter but never uses it, list it in `worker_inert_agent_functions` instead; never do this for functions that depend on agent identity, configuration, or session state.
`requires_room_context` and `requires_primary_runtime` describe where a tool can run; they do not grant or replace tool authorization.

### Approval aliases

A function that does what another function does, such as one that types command lines into a shell, can have that function's [approval rules](tool-approval.md) judge its calls too.
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
In a [background script](tools/background-scripts.md#which-tools-a-script-can-call-and-when-it-needs-approval) or a [Dynamic Workflow](tools/agent-orchestration.md#allowing-participant-tools), listing the toolkit in `allowed_tools` does not pre-approve the `judged_as` function: unless a toolkit with that function is pre-approved for the same script or participant, a call asks whenever `judge` returns a call that no rule auto-approves or lets through, and a workflow participant gets the aliased function only when it would not ask.

### Dependencies

Plugin tool dependencies are never installed automatically, except for the tools of [additional Google workspaces](#additional-google-workspaces), which install the extra of the built-in tool they reuse, such as `mindroom[gmail]` for `secondary_gmail`.
When a listed package is missing, the tool fails to load with `Missing dependencies for tool '<name>': <packages>` instead of failing mid-conversation.
Document the dependencies in the plugin README so users can install them, for example `pip install openviking-client aiohttp`.
Built-in tools install their own dependencies as described in [Automatic Dependency Installation](tools/index.md#automatic-dependency-installation).

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
| `authored_override` | bool | `True` | Set to `False` to forbid setting the field inline in `config.yaml`; `password` fields are never allowed inline (see [Per-Agent Tool Configuration](tools/index.md#per-agent-tool-configuration)) |
| `env` | tuple of strings | `()` | Environment variables the field falls back to, in order, when nothing else sets it (see [How Settings Combine](tools/index.md#how-settings-combine)) |

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
| `SHARED_DIRECTORY_ROOTS` | `shared_directory_roots` | A function that returns the [shared directories](tools/execution-and-coding.md#shared_directories) the tool can reach where it runs, each with its `path` and whether it is `writable`: in the primary process the constructing agent's, or none when tools run in dedicated workers or through the sandbox proxy, and in a worker every directory it mounts (see [Access by deployment](tools/execution-and-coding.md#access-by-deployment)); call it on each use, since the agent can join or leave one while it replies |

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
`await context.hooks.message_sender(room_id, body, thread_id, source_hook, extra_content, *, trigger_dispatch=False, mentions=None, wake_mentioned=True)` sends a message from the turn's bot, with `source_hook` such as `"my-plugin:my-tool"` naming what sent it; `extra_content`, `trigger_dispatch`, `mentions`, and `wake_mentioned` work as in a hook's [`ctx.send_message`](hooks.md#sending-messages), but the message does not carry the turn's requester.
`context.turn_id` names the agent turn the call belongs to, which that turn's hooks carry as `ctx.turn_id`, or is `None` for a call outside a turn, such as a background script's; in a delegated subagent's turn, `context.parent_turn_id` names the turn that delegated it.

Inside a tool call, `mindroom.plugin_api.current_call()` returns a `CurrentCall`, or `None` outside one, such as when code calls the method directly.
It carries the provider's `call_id`, the agent's `tools:` entry that offered the function as `toolkit`, the `session_id`, the `session_state` dict the function may change in place and MindRoom stores after the run, the team's stored conversation as `history_scope` in a team run, and the same `ToolRuntimeContext` as `runtime`.

### Redacting credentials

To keep or show text from a tool call, such as a shell command or a search query, without the credentials it may carry, pass it through `redact_sensitive_text` from `mindroom.plugin_api`, which needs plugin API 1.17:

```python
from mindroom.plugin_api import ToolBeforeCallContext, hook, redact_sensitive_text


@hook("tool:before_call")
async def record_shell_commands(ctx: ToolBeforeCallContext) -> None:
    if ctx.tool_name != "run_shell_command":
        return
    args = ctx.arguments.get("args", "")
    command = args if isinstance(args, str) else " ".join(args)
    line = redact_sensitive_text(" ".join(command.split()), max_length=500)
    with (ctx.state_root / "commands.log").open("a") as log:
        log.write(f"{ctx.room_id} {line}\n")
```

It replaces bearer tokens, API keys, tokens with a known prefix such as `sk-` or `ghp_`, values assigned to secret names such as `password=` or `API_KEY=`, passwords given as `curl -u user:password` or mysql's `-ppassword`, secret URL query parameters, and private keys with `***redacted***`, and a URL's user name and password with `***`, as MindRoom's logs and reply tool traces do.
A secret name that ends its line, such as `password:`, or a quoted value left open, hides everything after it, since the value may continue on the next line.
`max_length` cuts the result to that many characters, ending in `... [truncated]`.
It never raises: text it cannot check, such as text over 65,536 characters without `max_length`, comes back as `[redaction failed]`.
What a plugin logs through `ctx.logger` or `get_logger` is already redacted, so call it for text that goes elsewhere, such as a file, a message, or room state.
To show a tool call's whole arguments as its reply's tool trace does, use the hook context's [`chat_preview()`](hooks.md#event-specific-fields) instead.

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
Inside an agent's reply the active model already reflects the requester's [budget](budgets.md) fallback; where no model is active, `None` as the last argument skips that fallback.
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
Pass `user_agent`, which needs plugin API 1.3, to send your own `User-Agent` header instead of HTTPX's default.
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

An OAuth module defines providers, and MindRoom core handles the connect, callback, credential storage, status, and disconnect flows described in the [OAuth framework](oauth-framework.md#oauth-framework).
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

The [OAuth framework](oauth-framework.md#oauth-framework) owns the service naming rules and the PKCE option.

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

A provider whose authorization server offers no registration endpoint but accepts client ID metadata documents also gets a client under the conditions in [OAuth Client Registration](mcp.md#oauth-client-registration), which include a public client, dynamic registration left enabled, and an HTTPS `MINDROOM_PUBLIC_URL` on a public DNS hostname; a plugin that relies on this states `"mindroom_plugin_api": "1.29"` in its manifest.

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

1. Provision its OAuth client through a [shared credential seed](oauth-framework.md#credential-seeds) with `client_id` and `client_secret` under the `client_config_service`.
2. For each service, enable the matching Google API and register its callback on that client, such as `https://your-host/api/oauth/secondary_google_gmail/callback`.
3. Enable the plugin and add the prefixed tools, such as `secondary_gmail` beside `gmail`, to the agent's tools.

When a workspace tool needs a login, it returns its own chat authorization link.
Workspace tools never fall back to the default Google client or a global service account, and like the built-in Google tools they always run in the primary runtime.
Functions are prefixed, such as `secondary_get_latest_emails` and `secondary_send_email`, so approval rules, script-tool allowlists, and function filters must use those names; a rule for `send_email` does not match `secondary_send_email`.

### Google Cloud tools

A plugin tool that calls Google Cloud client libraries, such as `google-cloud-storage`, can reuse the built-in read-only **Google Cloud** connection instead of defining its own OAuth provider.
See [Google Services OAuth](deployment/google-services-oauth.md#providers) for the connection's scope, client setup, service-account option, and account restrictions.

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
See [Atlassian Cloud](tools/atlassian.md#add-more-connections) for the plugin example, fields, and callback setup.

## MCP servers

Configure MCP servers under `mcp_servers:` in `config.yaml` as described in [MCP](mcp.md).
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
    Return JSON, and serve pages from [origins of your own](#origins-of-your-own).
    MindRoom answers every response under `/api/plugins/` with `X-Content-Type-Options: nosniff` and `Content-Security-Policy: sandbox; default-src 'none'`, whatever the route sets, so a browser neither guesses another type nor runs script or loads anything in a page a route returns; that is a backstop, not a licence to return pages.

MindRoom builds each plugin's app from its router as the plugin loads, and a reload builds a new one.
A routes module that raises as it loads, has no `router` (`Plugin routes module must define router: APIRouter`), or has a router FastAPI refuses to serve (`Plugin router cannot be served: ...`), such as one with a route at `""` rather than `"/"`, costs the plugin only its routes: MindRoom logs `Plugin routes failed to load; serving none` with the reason, its paths answer 404, and its tools and hooks keep working, after a reload too.
Only `mindroom plugins check` fails on such a routes module; `mindroom config validate` and `mindroom doctor` import it too, but only log it and still pass the config.
Routes follow plugin reloads, so the next request after a reload runs the new code.
Every plugin reload, and every config reload that changes `plugins:`, closes each open plugin websocket with code 1012 (service restart), so its client reconnects to the new code; a reload that fails leaves them open.
A request for a plugin that is not loaded or has no routes, or for a path its router does not define, answers 404.
A route that raises answers 500 with no detail of the error, and the other routes keep serving.
A route that uses its [plugin context](#plugin-context) after a config reload disabled or removed its plugin answers 503 instead.
Only the primary serves plugin routes, and workers never import a routes module, so it may import packages that workers lack.
That holds only while the routes module is its own file that the tools and hooks modules do not import, because workers load those.
MindRoom adds no authentication of its own to these routes, so a route without `require_client_user`, `require_room_member`, `require_room_reader`, `require_room_messenger`, or [`require_platform_administrator`](#people-agents-and-administrators) is public.

Each plugin's router runs in that FastAPI app of its own, served under `/api/plugins/<name>/`:

- The router's `lifespan` and `on_event` startup and shutdown handlers never run; put such code in the plugin's [lifespan](#plugin-lifecycle).
- A `GET` route answers `HEAD` with 405, as in any FastAPI app; list both in `@router.api_route(path, methods=["GET", "HEAD"])` if a client needs `HEAD`.
- The `dependency_overrides` of MindRoom's main app do not reach plugin routes, so a test that overrides a dependency there does not change a plugin route.
- `request.url_for` finds only the plugin's own route names, and `request.base_url` is the plugin's `/api/plugins/<name>/`.

A plugin route that serves a Matrix web client authenticates its caller with `require_client_user` from `mindroom.plugin_http`, as a FastAPI dependency: `user: ClientUser = Depends(require_client_user)`.
`ClientUser.user_id` is the caller's Matrix user ID.
The client signs in by posting a Matrix OpenID token to `POST /api/client-sessions`, which returns `session_token`, `user_id`, and `expires_at` in Unix seconds, and then sends `Authorization: Bearer <session_token>`.
Sign-in needs a homeserver that binds OpenID tokens to this deployment, MindRoom's Tuwunel fork with `mindroom_openid_audience` on (see [Token Binding](connections-portal.md#token-binding)); on any other homeserver `POST /api/client-sessions` answers 403, with no setting to allow it, because another backend the user opens could replay their token here.
Sessions last one hour, and the route answers 401 for a missing, unknown, or expired token; the client then signs in again.
To end its session at once, as at sign-out, the client sends `DELETE /api/client-sessions` with the same header, which answers 204; from then on that token answers 401 like an expired one.
A response a route is already streaming continues after its session expires or is revoked; a [websocket](#websocket-routes) instead closes.
Browser origins that may call these routes and `/api/client-sessions` come from `MINDROOM_COMPUTER_ALLOWED_ORIGINS`, the same list that lets MindRoom Chat open [worker computers](tools/worker-computer.md); there is no separate setting for plugin routes.
Every response under `/api/plugins/` carries `Cache-Control: no-store`, whatever the route sets, besides the two headers above, and of the headers beyond those every browser lets pages read, such as `Content-Type`, other origins can read only `WWW-Authenticate`, so a client on another origin cannot read `ETag`, `Content-Disposition`, or `Retry-After`.
A route that acts on one room depends on `require_room_member` instead, as in the example above: `user: ClientUser = Depends(require_room_member)`.
It authenticates the caller as `require_client_user` does and takes the room from the route's `{room_id}` path parameter, and it returns the same `ClientUser` once that user is joined to the room.
It never takes the room from the query string: as a dependency of a route without `{room_id}` in its path, it answers 422.
A route that finds its room some other way awaits `require_room_member(request, room_id)` itself.
Either way the user is always the session's own: no parameter of the request can name another.
It reads live membership through the Matrix client of the router bot if it is in the room, else of any running MindRoom bot in the room, and answers 403 unless the user is joined.
It answers 503 when membership is unavailable: `No running MindRoom bot is in this room.` lasts until a bot joins the room, `Room membership lookup failed; try again.` means the Matrix call failed or timed out, and `Room membership is unavailable.` means the API is served without MindRoom's bots, as outside `mindroom run` or while it stops.

A route that shows a room's people, checks their power, reads or changes the room's modes, or shows what happened in the room depends on `require_room_reader` instead.
It checks the caller exactly as `require_room_member` does, with the same answers, before it reads anything else of the room, and then returns a `RoomReader`:

```python
from fastapi import HTTPException

from mindroom.plugin_http import RoomReader, require_room_reader


@router.get("/rooms/{room_id}/roster")
async def roster(reader: Annotated[RoomReader, Depends(require_room_reader)]) -> dict[str, str]:
    power = await reader.power()
    if not power.is_admin(reader.user.user_id):
        raise HTTPException(403, "Only room admins see the roster.")
    return {user_id: name or user_id for user_id, name in reader.members.items()}
```

| Member | Value |
|--------|-------|
| `user` | The caller's `ClientUser` |
| `members` | A read-only mapping from each joined user's ID to their display name in the room, or `None` when they have none, from the lookup that checked the caller |
| `await power()` | A `RoomPower` read from the room's power levels at that moment |
| `await is_direct()` | Whether MindRoom's agents treat the room as a direct message: the bot's direct-message list names it, it is an unnamed room of two without a topic, or a member joined it as direct; show it, but never grant anything by it |
| `await agents()` | An `AgentWorkspace` for each agent joined to the room that the caller may use there, in config order |
| `await agent(agent_user_id)` | The `AgentWorkspace` of the agent whose Matrix user ID is `agent_user_id`, once the caller may use it there |
| `await terminals(workspace, *, create=False)` | The agent's terminals for an `AgentWorkspace` that `agents()` or `agent()` returned, or `None` while its `terminal` is `none`; see [An agent's terminals](#an-agents-terminals) |
| `modes()` | A `RoomModes` with the room's [thread mode](configuration/threads.md#thread_mode) override `thread` and [reply mode](configuration/threads.md#reply_mode) override `reply`, each `None` while the room follows the config, and `reply_in_effect`, the reply mode the room uses |
| `await set_modes(*, thread=..., reply=...)` | Changes the room's overrides as `!thread_mode` and `!reply_mode` do |
| `await history()` | A `RoomHistory` read at that moment: the room's history visibility and where the caller's view of its history begins |

`RoomPower.level(user_id)` is the user's power level, `math.inf` for a creator of a room version 12 room, which JSON cannot carry, so a route turns it into a value of its own before returning it.
`is_admin(user_id)` is true at level 100 or more and for such a creator.
`may_send(user_id, event_type)` tells whether that level lets the user send message events of the type, which needs the type's level in the power levels' `events`, else their `events_default`, and in an encrypted room also the level of `m.room.encrypted`, the type messages go out as.
`RoomPower` also carries the `power_levels` content it read, the room's `creators`, and whether it is `encrypted`.

`RoomHistory.visibility` is the room's `m.room.history_visibility`, `shared` when it has none.
`since(recorded_under=None)` returns where the caller's view of the room's history begins, in milliseconds since the epoch, or `None` when they may see all of it: `None` under `shared` or `world_readable`, the invite their current membership came from (else their join) under `invited`, and their join under `joined` or any value Matrix does not define.
A route that shows records it kept of a room, such as notes, keeps with each one the `visibility` the room had then and passes it as `recorded_under`, and the stricter of it and the visibility now applies, so making a room's history stricter also hides the records kept before, unlike its messages:

```python
@router.get("/rooms/{room_id}/notes")
async def notes(room_id: str, reader: Annotated[RoomReader, Depends(require_room_reader)]) -> list[dict[str, object]]:
    history = await reader.history()
    return [
        note
        for note in NOTES.get(room_id, [])
        if (since := history.since(note["visibility"])) is None or note["at_ms"] > since
    ]
```

The start is that of the caller's current membership: a display name or avatar change keeps it, but leaving and joining again starts it anew.
An invite sent from another homeserver carries that homeserver's clock, so under `invited` it counts no further than the join.
The start falls later, so the caller sees less, never more, when MindRoom cannot read back to their join or invite: after about 20 profile changes, or past an event from before its bot joined the room.

Each call to `power()` or `history()` reads again, and `members` is read anew for each request, so a changed name, power level, or history visibility shows on the next one.
`power()`, `is_direct()`, and `history()` pick their bot as membership does and answer 503 with `No running MindRoom bot is in this room.` once none is in the room.
`power()` answers 503 rather than `False` with `Room state read failed; try again.` when its read fails or times out.
`history()` reads the caller's membership again and answers 403 like `require_room_member` once they are no longer joined, and 503 with `Room history read failed; try again.` when its read fails or times out.
`is_direct()` keeps its answer for a few minutes; it answers 503 with `Direct message lookup failed; try again.` when the bot's direct-message list or the room's state cannot be read and nothing else marks the room as direct, or when the lookup raises or times out.

`set_modes()` takes `thread` as `"thread"` or `"room"` and `reply` as `"auto"` or `"mentions"`; `None` clears that override, and one left out stays.
Before it changes anything, it answers 403 with `Only room admins change the room's modes.` unless the caller holds room admin power by a `power()` read, 503 when that read fails, and 503 with `The router is not running in this room.` while the router is stopped or not in the room, as in a direct message with one agent.
As with `!thread_mode`, the router then updates the room's [`io.mindroom.thread_mode`](configuration/threads.md#thread-mode-resolution) state, which clients choose the room's view by, before `set_modes()` returns.
When it cannot, as without permission to send state events in the room, `set_modes()` answers 503 with `The modes were saved, but the room's thread mode is not published yet; try again.`: the change stays saved, and calling it again retries the update.

```python
from typing import Literal

from pydantic import BaseModel

from mindroom.plugin_http import RoomModes


class ModesChange(BaseModel):
    thread: Literal["thread", "room"] | None = None
    reply: Literal["auto", "mentions"] | None = None


@router.put("/rooms/{room_id}/modes")
async def change_modes(
    change: ModesChange,
    reader: Annotated[RoomReader, Depends(require_room_reader)],
) -> RoomModes:
    # A field the request leaves out stays as it is; one sent as null clears that override.
    await reader.set_modes(**change.model_dump(exclude_unset=True))
    return reader.modes()
```

`agents()` and `agent()` check what the [worker Computer](tools/worker-computer.md) checks before it opens: the agent is a current agent, it and the caller are both joined to the room, and its [access rules](authorization.md#responder-access) let the caller use it there.
`agent()` answers 403 with `The selected user is not a current agent.`, `Requester and agent must both be joined to this room.`, or `Requester cannot use this agent.`, and 503 with `Agent identities are not ready.` while the ID matches no ready agent and some agent's Matrix account is not ready, or `Responder membership policy is unresolved.` until MindRoom can decide.
`agents()` leaves out each agent that fails, and also, logging `room_agent_left_out`, one whose Matrix account is not ready, whose room grants Matrix has not confirmed, or whose contacts or stored tool settings cannot be read; it answers 503 with `Responder membership policy is unresolved.` only while MindRoom cannot list the router's rooms and an agent's access waits on them.
Both answer 503 with `Agent access is unavailable; try again.` while the router is not running or MindRoom has not yet applied a saved config that adds or removes an agent or team or changes an `access.current_room_members` or `access.members_of_rooms` rule.
A contact room opens no agent: `agents()` leaves out its agent, and `agent()` answers 403 with `No workspace opens through a contact room.`.
Each answer follows the config the API serves when the request arrives, so a saved change to an agent's access shows on the next request.
An `AgentWorkspace` holds the agent's `name`, `user_id`, `display_name`, whether it is `private`, the `config` it was resolved under, and what the caller may do there:

| Field | Value |
|-------|-------|
| `root` | The folder the agent's tools work in when this caller talks to it in this room: one folder for every caller of a shared agent, each caller's own for a private one; it may not exist before the agent's first turn for them |
| `files` | `none`, `read`, or `write`, as [Who may open and change a workspace](workspace-files.md#who-may-open-and-change-a-workspace) describes, from each toolkit's [`workspace_writers`](#decorator-fields) |
| `worker_target` | The worker the agent's turns for this caller use, which [`worker_relay`](#reaching-the-agents-worker) and [`ensure_worker_service`](#worker-services) take |
| `terminal` | `none` when the agent's [`terminals`](configuration/agents.md) setting is `none`, its default, or the agent has no [`shell`](tools/execution-and-coding.md#shell) that offers `run_shell_command`, or that shell runs in the primary process, where no [terminal](tools/execution-and-coding.md#terminal) runs. `type` when the setting is `type`, the shell runs in a worker that mounts the agent's workspace, a dedicated Docker or Kubernetes worker or a `static_runner` [that mounts the agents' storage](deployment/sandbox-proxy.md#filesystem-isolation-and-agent-data), and the caller may post messages in the room. Else `watch` |

`agents()` and `agent()` read the room's power levels for `files` and `terminal` as `power()` does, with its answers.
MindRoom resolves `root` itself, never from the request; a route that opens files there uses `resolve_agent_file` and `write_agent_file` (see [Agent files](#agent-files)) with `file_access="workspace"`, which refuse a path or link that leaves it, since code the agent runs can plant links in its workspace.

`await client_rooms(request, candidates)` returns those of the room IDs in `candidates` that the session's user is joined to now, in their order.
It checks each room as `require_room_member` does and leaves out a room whose check fails, and it answers 401 without a live session and 503 with `Room membership is unavailable.` outside `mindroom run`.
A plugin that uses `require_room_reader` or `client_rooms` states `"mindroom_plugin_api": "1.7"` in its manifest, one that uses `modes()` or `set_modes()` states `"1.9"`, one that calls `history()` states `"1.11"`, one that uses `agents()` or `agent()` states `"1.13"`, one that relies on `terminal` being `none` for a shell in the primary process, or `watch` for a `static_runner` that does not mount the agents' storage, states `"1.18"`, and one that relies on `files` being `read` for writing toolkits on such a `static_runner` states `"1.19"`.

A route that posts to a room depends on `require_room_messenger`.
It checks the caller exactly as `require_room_member` does, with the same answers, and returns a `RoomMessenger`, which posts as the router, never from the caller's account:

```python
from pydantic import BaseModel

from mindroom.plugin_http import RoomMessenger, require_room_messenger


class Ask(BaseModel):
    agent_user_id: str
    question: str


@router.post("/rooms/{room_id}/ask")
async def ask(body: Ask, messenger: Annotated[RoomMessenger, Depends(require_room_messenger)]) -> dict[str, str]:
    return {"event_id": await messenger.relay(body.question, mentions=[body.agent_user_id])}
```

| Member | Value |
|--------|-------|
| `user` | The caller's `ClientUser` |
| `await notice(text, *, thread_id=None, content=None)` | Posts `text` as the router's `m.notice`, which mentions no one, acts for no one, and no agent answers; it checks no power level, so the route decides who may have the router post |
| `await relay(text, *, mentions, thread_id=None, content=None)` | Posts `text` as the router for the caller, below a line `Sent for <their Matrix ID>:`, mentioning exactly the agents and teams whose Matrix IDs `mentions` lists; each one answers the caller, and only if its access rules admit the caller |

Both return the message's event ID, and post in the thread `thread_id` names, else in the room's timeline.
`content` adds namespaced keys of the plugin's own to the message, such as a card under one of its [reserved namespaces](#reserved-namespaces).
It answers 400 and sends nothing when `content` sets a key of Matrix's own, one without a namespace such as `body` or `msgtype` or one under `m.` or `org.matrix.`, or, anywhere in it, a key under `com.mindroom.`, `io.mindroom.`, MindRoom Chat's `chat.mindroom.`, or a namespace another loaded plugin reserves and the posting plugin does not, so a route may pass a client's content through without letting it change the text, edit another message, forge another plugin's card, or claim to be someone else.
Plugins that reserve the same namespace, such as a family of plugins sharing one, can each post keys in it, but a narrower namespace another plugin reserves inside it, such as `com.example.cards.` inside `com.example.`, stays refused.
`relay()` answers 400 unless `mentions` names at least one agent or team and only agents and teams joined to the room, never a person or the router.
Just before it sends, `relay()` checks the caller again, with the answers of `require_room_member` and `power()`: 401 once their session has ended, 403 once they have left the room or may no longer send `m.room.message` there, and 503 when either cannot be read.
Both answer 503 with `MindRoom's router is not running; try again.` while the router is down, with `MindRoom's router is not in this room.` in a room the router has not joined, such as a direct message with one agent, where an agent can call [`invite_router`](tool-approval.md#when-the-router-is-missing) first, and with `The message could not be sent; try again.` when Matrix refuses the message.
A plugin that uses `require_room_messenger` states `"mindroom_plugin_api": "1.12"` in its manifest, and `"1.15"` when it posts keys in a namespace another plugin also reserves.

A signed-in client asks which plugins the server serves with `GET /api/plugins`, with the same `Authorization` header and from the same browser origins, and a missing, unknown, or expired session answers 401.
The answer's `plugin_api_version` is this MindRoom's [plugin API version](#plugin-api-versions) as `"MAJOR.MINOR"`, and `plugins` lists each enabled plugin that loaded, in config order, with its manifest `name` and `mindroom_plugin_api`, which is `null` when the manifest leaves it out:

```json
{"plugin_api_version": "1.0", "plugins": [{"name": "my-plugin", "mindroom_plugin_api": "1.0"}]}
```

### An agent's terminals

`await terminals(workspace, *, create=False)` returns a `WorkerTerminals` for the agent's [terminals](tools/execution-and-coding.md#terminal), those the chat's Terminal tab shows the caller for that agent in this room, or `None` while `workspace.terminal` is `none`.
It checks nothing beyond what `agent()` checked, so the route enforces what the caller may do, as MindRoom's own terminal routes do: opening, closing, and typing in a terminal require `workspace.terminal == "type"`, and listing and watching require `watch` or `type`.
Pass `create=True` before opening a terminal, so the agent's workspace exists, as its first turn would make it.

| Call | Effect |
|------|--------|
| `await terminals.open(*, cols=80, rows=24, cwd=None)` | Starts a terminal in `cwd`, a folder of the workspace given relative to it, by default the workspace itself, starting the agent's worker as a shell call would, and returns its `id`, `cols`, `rows`, `alive`, `exit_code`, and `created_at` |
| `await terminals.sessions()` | The workspace's terminals, oldest first, each described as `open()` describes it |
| `await terminals.close(session_id)` | Ends a terminal |
| `async with terminals.attach(session_id) as terminal` | Attaches to a terminal; `await terminal.send_input(data)` types `data`, at most 16384 characters at once |

A refused call raises `TerminalError`, from `mindroom.plugin_http`, whose `status_code` and `detail` a route answers with, such as 404 for a terminal the workspace does not have, 413 for longer input, or 503 while the agent's worker is unavailable.
This route types a command its caller confirmed into a new terminal, where the chat's Terminal tab shows it run, and refuses a command too long to type at once before it opens one:

```python
# routes.py
from typing import Annotated

from fastapi import APIRouter, Body, Depends, HTTPException

from mindroom.plugin_http import RoomReader, TerminalError, require_room_reader

router = APIRouter()


@router.post("/rooms/{room_id}/agents/{agent_user_id}/run")
async def run(
    agent_user_id: str,
    command: Annotated[str, Body(embed=True, max_length=16383)],
    room: RoomReader = Depends(require_room_reader),
) -> dict[str, str]:
    workspace = await room.agent(agent_user_id)
    if workspace.terminal != "type":
        raise HTTPException(403, "You may not type in this agent's terminals.")
    terminals = await room.terminals(workspace, create=True)
    if terminals is None:
        raise HTTPException(403, "This agent has no terminals.")
    try:
        info = await terminals.open()
        async with terminals.attach(info.id) as terminal:
            await terminal.send_input(f"{command}\r")
    except TerminalError as error:
        raise HTTPException(error.status_code, error.detail) from None
    return {"terminal_id": info.id}
```

A plugin that uses `terminals()` states `"mindroom_plugin_api": "1.27"` in its manifest.

### Websocket routes

A browser cannot set a websocket's `Authorization` header, so a websocket route signs its caller in with `client_stream` from `mindroom.plugin_http` instead:

```python
# routes.py
from fastapi import APIRouter, WebSocket

from mindroom.plugin_http import client_stream

router = APIRouter()


@router.websocket("/rooms/{room_id}/echo")
async def echo(websocket: WebSocket, room_id: str) -> None:
    async with client_stream(websocket, room_id) as user:
        async for text in websocket.iter_text():
            await websocket.send_text(f"{user.user_id}: {text}")
```

The client offers two subprotocols, `mindroom-session` and `mindroom-session.<session_token>`, as in `new WebSocket(url, ["mindroom-session", "mindroom-session." + sessionToken])`, and the server accepts with `mindroom-session`, never echoing the token.
`client_stream` accepts the websocket only from an origin `MINDROOM_COMPUTER_ALLOWED_ORIGINS` lists, for a live session, and, given `room_id`, for a user joined to that room, checked as `require_room_member` checks it; leave `room_id` out for a stream that serves no room.
It refuses any other handshake with an HTTP answer before accepting, as `require_room_member` answers: 401 for a missing, unknown, or expired session, 403 for an unlisted origin or a non-member, and 503 when membership is unavailable; a browser reports only a failed connection.
Once accepted, the websocket closes with code 4401 as soon as its session ends: it expires, the client revokes it, or the same user's ninth session replaces it, since MindRoom keeps eight per user; the client then signs in again.
Each user may hold 16 open streams at once across every route on `client_stream`, MindRoom's own included, whatever their sessions; past that, a handshake that passed every other check and the route's `admit` below is refused with 503 and `This user has too many open streams; close one and try again.` until one of them closes.
Given `room_id`, it rechecks membership every 60 seconds, closing with 1008 once the user has left the room and with 1013 when membership is unavailable, with the same reason `require_room_member` gives for its 503, which the client may retry.
Closing cancels the `async with` block where it waits, and the route runs on after the block.
A client that leaves ends the block only once the route reads or sends on the websocket, so a route that only waits to send also reads it in a task beside its sends; otherwise each stream a client left keeps its place among the user's 16 until its next send or the session's end.

A route with an admission rule of its own, such as a tighter cap on each user's streams on that route, passes it as `admit`: a function, plain or `async`, that gets the signed-in `ClientUser` once the checks above pass and returns whether to accept.
A plain function runs on the server's event loop, so it must not block.
When it returns false, `client_stream` refuses the handshake with 503 and `The route refused this stream; try again later.`, so the client never sees the stream open, as it would if the route closed it after accepting.
A check that raises `HTTPException` refuses with that answer instead, and one that raises anything else answers 500, as a route that raises does.

```python
# routes.py
from collections import Counter

from fastapi import APIRouter, WebSocket

from mindroom.plugin_http import ClientUser, client_stream

router = APIRouter()
open_streams: Counter[str] = Counter()


def under_cap(user: ClientUser) -> bool:
    return open_streams[user.user_id] < 4


@router.websocket("/feed")
async def feed(websocket: WebSocket) -> None:
    async with client_stream(websocket, admit=under_cap) as user:
        open_streams[user.user_id] += 1
        try:
            async for text in websocket.iter_text():
                await websocket.send_text(text)
        finally:
            open_streams[user.user_id] -= 1
```

A plugin that uses `client_stream` states `"mindroom_plugin_api": "1.6"` in its manifest, and one that passes `admit` states `"1.14"`.
Browsers apply no CORS to websockets, so a websocket route without `client_stream` is public and accepts any origin.

Inside the block, `await stream_room_reader(websocket, user, room_id)` returns the `RoomReader` a route gets from `require_room_reader`, for the user `client_stream` yields.
It raises `HTTPException`, with 409 when MindRoom now serves another runtime and otherwise as `require_room_member` answers when it checks membership again, and its `agents()` and `agent()` answer under the config MindRoom serves at that moment.
The websocket is already accepted then, so the route catches the exception and closes the websocket itself, for example with 1008 for a refusal and 1013 for a check it cannot answer.
A stream that keeps checking what its caller may do, such as whether they may still type, calls it again for each check.
A plugin that uses `stream_room_reader` states `"mindroom_plugin_api": "1.18"` in its manifest.

## Plugin context

A plugin's HTTP routes and tools read their own plugin's settings, state directory, and config from a `PluginContext`.
A route gets it from `plugin_context` in `mindroom.plugin_http`, as a FastAPI dependency or by calling it with the request or websocket:

```python
# routes.py
from typing import Annotated

from fastapi import APIRouter, Depends

from mindroom.plugin_api import PluginContext
from mindroom.plugin_http import plugin_context, require_client_user

router = APIRouter()


@router.get("/greeting", dependencies=[Depends(require_client_user)])
async def greeting(ctx: Annotated[PluginContext, Depends(plugin_context)]) -> dict[str, str]:
    return {"greeting": ctx.settings.get("greeting", "hello")}
```

Each call returns the context of the config MindRoom serves at that moment, so a long-lived websocket calls `plugin_context(websocket)` again to see a later config reload; a reload that changes `plugins:`, such as this plugin's `settings`, closes the websocket instead, and the reconnected client reads the new settings.

A tool calls `current_plugin_context(plugin_name)` from `mindroom.plugin_api` with its plugin's manifest `name`, during a tool call in a Matrix conversation or in an OpenAI-compatible API request whose API key is [mapped to a user](openai-api.md#requester-identities-and-delegation).
Anywhere else, such as in the MCP gateway or a worker, it raises `RuntimeError`, so a tool that uses it sets [`requires_primary_runtime=True`](#decorator-fields).
A plugin that uses its context states `"mindroom_plugin_api": "1.4"` in its manifest, so an older MindRoom skips it with a clear reason.

| Member | Value |
|--------|-------|
| `plugin_name` | The plugin's manifest `name` |
| `settings` | The `settings` of the plugin's entry in `config`, as a read-only mapping whose nested values are the context's own copy, so no change reaches the config |
| `config` | The config the request or tool call runs under |
| `runtime_paths` | The runtime's paths |
| `state_root` | The plugin's [state directory](hooks.md#plugin-state), the one its hooks get as `ctx.state_root` |
| `is_active()` | Whether the config MindRoom runs now still loads the plugin |

A context keeps the settings of the config it was made under, and a tool's context has its turn's or request's config.
Once a config reload disables or removes the plugin, `is_active()` returns `False` and `settings`, `config`, and `state_root` raise an error.
A route that meets that error answers 503 with `Plugin '<name>' is not active: the current config does not load it.`, and a websocket that meets it closes with code 1013 and that reason, though the reload that disables the plugin closes its websockets with 1012 first.

## Plugin lifecycle

A plugin runs start and stop code from the module its manifest names as `lifecycle_module`, which defines `lifespan(lifecycle)` returning an async context manager.
Code before its `yield` starts the plugin, and code after it stops the plugin, so use it to open connections, start background tasks, or launch child processes, and to close them again.
This keeps a ticker running across reloads without a gap, and stops it once the plugin is disabled or MindRoom stops:

```python
# lifecycle.py
import asyncio
from contextlib import asynccontextmanager
from pathlib import Path

from mindroom.plugin_api import PluginLifecycle


async def tick(log_path: Path) -> None:
    while True:
        with log_path.open("a") as log:
            log.write("tick\n")
        await asyncio.sleep(60)


@asynccontextmanager
async def lifespan(lifecycle: PluginLifecycle):
    state = lifecycle.process_state
    previous = state.get("ticker")
    state["ticker"] = asyncio.create_task(tick(lifecycle.context().state_root / "ticks.log"))
    if previous is not None:
        previous.cancel()
    yield
    if lifecycle.stop_reason != "reload":
        state.pop("ticker").cancel()
```

| Member | Value |
|--------|-------|
| `plugin_name` | The plugin's manifest `name` |
| `context()` | The plugin's [context](#plugin-context) under the config MindRoom serves now; it raises once that config no longer loads the plugin |
| `process_state` | A dict that every start of this plugin shares for as long as the MindRoom process runs; a reload cancels no task kept in it |
| `stop_reason` | Set before the stop code runs: `"reload"` when the plugin's new lifespan already started and can take over `process_state`, `"disabled"` when no new lifespan did because a config change removed or disabled the plugin or its new code failed to load or start, and `"shutdown"` when MindRoom stops, after its agents have stopped |

MindRoom starts each plugin's lifespan once the plugin loads at startup, in the process that runs the agents.
`mindroom plugins check`, `mindroom config validate`, and `mindroom doctor` import the lifecycle module but never run `lifespan`, and workers never run it either.
The plugin's routes, tools, and hooks can import the lifecycle module, such as `from .lifecycle import STATE`, to use what its lifespan set up, and they share one copy of it with the lifespan.
Any of the plugin's modules can import another the same way and gets the copy MindRoom serves, and so does importing a [package plugin](#python-package-plugins)'s module by its package name, such as `from my_plugin.hooks import STATE`.
Importing a module by its package name gives a separate copy for a directory plugin and for a plugin inside another plugin's directory, and so does importing a module by its bare name, such as `import hooks`, from a plugin that puts its own directory on `sys.path`.
A worker that loads a module importing the lifecycle module imports it too.
Every plugin reload, and every config reload that changes `plugins:`, runs every plugin's code again, so it restarts every lifespan, including those of plugins that did not change.
The new lifespan starts while the old one still runs, so hand over what only one holder can have, such as a listening port or a lock file, through `process_state` instead of opening it again, or the new start fails.
The plugin's new tools, hooks, and routes are live while its new lifespan starts, so code that uses what the lifespan sets up handles it not being there yet.
A start or stop that raises or takes longer than 10 seconds logs `Plugin lifespan failed to start` or `Plugin lifespan failed to stop` with the plugin's name, and MindRoom cancels one that overran and carries on; a lifespan whose start failed never runs its stop code.
A lifecycle module that fails to import or defines no `lifespan` logs `Plugin lifecycle module failed to load; running no lifespan` with the reason, and `mindroom plugins check` fails on it.
Either failure costs the plugin only its lifespan, and its tools, hooks, and routes keep working, except that a lifecycle module that fails to import also fails a module that imports it: MindRoom then skips the plugin when that is its tools or hooks module, and serves no routes when it is its routes module.
A plugin with a lifecycle module states `"mindroom_plugin_api": "1.8"` in its manifest, so an older MindRoom skips it with a clear reason.

## Worker services

A worker service is a long-lived program of a plugin, such as a web server, that runs inside an agent's dedicated worker and never in the process that runs the agents.
Declare it once with `WorkerService` from `mindroom.plugin_api`, and start it from a tool with `requires_primary_runtime=True`, which has the plugin's [context](#plugin-context) and the agent's worker target:

```python
# tools.py
from mindroom.plugin_api import WorkerService, current_plugin_context, ensure_worker_service, get_tool_runtime_context

HELLO = WorkerService("hello", "hello_service.py", idle_timeout_seconds=600)


async def open_hello() -> str:
    runtime = get_tool_runtime_context()
    state = await ensure_worker_service(current_plugin_context("my-plugin"), HELLO, runtime.resolve_worker_target())
    return f"{state.state} on port {state.port}"
```

The program is a Python file in the plugin's folder that the worker runs with its own Python and environment.
Every process in the worker, including code the agent runs, can reach the service's port, so the worker hands each start a fresh secret on the first line of its stdin, and the program must refuse every request whose `X-MindRoom-Service-Secret` header does not carry it:

```python
# hello_service.py
import hmac
import os
import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

SECRET = sys.stdin.readline().strip()


class Hello(BaseHTTPRequestHandler):
    def do_GET(self) -> None:
        if not hmac.compare_digest(self.headers.get("X-MindRoom-Service-Secret", ""), SECRET):
            self.send_error(403)
            return
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b"hello")


ThreadingHTTPServer(("127.0.0.1", int(os.environ["MINDROOM_WORKER_SERVICE_PORT"])), Hello).serve_forever()
```

| Call | Effect |
|------|--------|
| `ensure_worker_service(context, service, target)` | Starts the service in the worker `target` names, starting that worker as a tool call does, unless the service already runs there, and returns a `WorkerServiceState` |
| `stop_worker_service(context, service, target)` | Stops the service; a stopped worker is not started |

`target` is the worker an agent's turns use for one requester: in a tool call, `get_tool_runtime_context().resolve_worker_target()`.
Neither call checks who may use the agent, so check that before you start a service for someone.
`WorkerService(name, module, idle_timeout_seconds=600.0)` takes a `name` of 1-64 lowercase letters, digits, `-`, or `_`, starting with a letter or digit, a `module` path relative to the plugin's folder, and a positive, finite `idle_timeout_seconds`.
`WorkerServiceState` has `state`, `port`, and `exit_code`.
`state` is `running` once the program, or a process it started in the program's process group, listens on its port and no other process does, `starting` when it has not within 10 seconds, so ensure it again, and `exited` once it ended, with its `exit_code`.
Each worker runs one instance of a service, so ensuring a running service returns the same port.
A service that exited stays reported as exited, and starts again only after `stop_worker_service` or its idle timeout, or when its program file changed.
The program's output goes to the worker's log.
The program runs in the worker's own workspace folder, not in the agent's workspace where its shell and file tools work, so a file it writes by a relative path is not in the agent's workspace.
It finds its port in `MINDROOM_WORKER_SERVICE_PORT` and the loopback address of the worker's runner in `MINDROOM_WORKER_SERVICE_RUNNER_URL`.
The port normally lies between 12000 and 14999, so a server the agent starts on a fixed port in that range can find it taken.

A service stops when `idle_timeout_seconds` pass without an ensure or a [relayed](#reaching-the-agents-worker) request, though never while a relayed answer or WebSocket to it is open, when it is stopped, when its worker stops, and when MindRoom reloads plugins or stops.
A service that replaced plugin code starts during a reload keeps running until its idle timeout.
Without a dedicated worker backend, set by `MINDROOM_WORKER_BACKEND` as `docker` or `kubernetes`, both calls raise `WorkerServiceError`, as they do when the worker is unavailable or refuses, with the reason.
A plugin with worker services states `"mindroom_plugin_api": "1.20"` in its manifest.

### The agent's browser

A worker service can watch the page of its agent's browser and, while it holds the Computer's control, act on it, for example to show and steer that page in a plugin's own panel.
This needs a worker that runs the [Computer](tools/worker-computer.md) and an agent that browses with the built-in `browser` tool; `browser_mcp` runs its browser in another process, which offers no page.
The service finds the bridge's WebSocket address in `MINDROOM_AGENT_BROWSER_URL` and opens it with its secret in the `X-MindRoom-Service-Secret` header.
The worker refuses a connection without the running instance's own secret, and every connection in a worker without a Computer.
Opening the bridge starts the Computer's display when it is stopped.
Each connection is a CDP session of its own on the agent's current page, so open one per viewer.

The bridge speaks the JSON of the [Chrome DevTools Protocol](https://chromedevtools.github.io/devtools-protocol/): send `{"id": 1, "method": "Page.startScreencast", "params": {}}` and read `{"id": 1, "result": {}}` or `{"id": 1, "error": {"code": -32000, "message": "..."}}`.
Commands run side by side and each answers when it completes, so a navigation waiting on a slow server holds up no other command.

| Methods | Allowed |
|---------|---------|
| `Page.startScreencast`, `Page.stopScreencast`, `Page.screencastFrameAck`, `Page.enable`, `Page.getNavigationHistory`, `Page.getLayoutMetrics`, `Target.getTargetInfo` | Always |
| `Input.dispatchMouseEvent`, `Input.dispatchKeyEvent`, `Input.insertText`, `Page.navigate`, `Page.navigateToHistoryEntry`, `Page.reload`, `Page.stopLoading`, `Emulation.setDeviceMetricsOverride`, `Emulation.clearDeviceMetricsOverride` | While this connection holds control |
| `MindRoom.takeControl`, `MindRoom.releaseControl` | Always: take or release the Computer's control |

Every other method, such as `Runtime.evaluate`, answers an error and does nothing, and so do `Page.reload` with `scriptToEvaluateOnLoad`, `Target.getTargetInfo` with a `targetId`, a number JSON cannot carry, such as `NaN`, and any command while the agent's browser shows no page.
`Page.navigate` opens only `http` and `https` addresses and `about:blank`, and the browser then checks every request as it does the agent's.
The bridge passes on its session's `Page.screencastFrame`, `Page.screencastVisibilityChanged`, `Page.frameNavigated`, `Page.navigatedWithinDocument`, `Page.frameStartedLoading`, and `Page.frameStoppedLoading` events; all but the first two need `Page.enable`.
Two events are the bridge's own:

| Event | Sent |
|-------|------|
| `{"method": "MindRoom.page", "params": {"page": true}}` | On connect and whenever the agent's browser shows another page or none (`false`); each page has a new session, so start the screencast and `Page.enable` again |
| `{"method": "MindRoom.control", "params": {"held": true}}` | When this connection takes control, and with `false` when it loses it: by releasing it, or when the Computer stops or restarts |

Control is the Computer's own, shared with the viewers of its desktop: one holder at a time, and while it is held the agent's browser calls fail with `Computer is under user control; resume the agent first.`
`MindRoom.takeControl` answers an error while someone else holds control or the Computer is stopped, and works while the agent's browser shows no page, so a service that shows a browser of its own can gate it by the same control.
Releasing control or leaving releases every key and mouse button the connection still holds, and a viewport override lasts until the service clears it or leaves.

The worker trusts every service it runs, so check the person in your plugin's route: admit a viewer with `RoomReader.agent()`, the Computer's own check, before you relay them to the service, and never offer the bridge's control to a viewer your route did not admit.
A service that keeps the newest frame of the agent's page in `frame.jpg`:

```python
# page_service.py
import asyncio
import base64
import json
import os
import sys
from pathlib import Path

from websockets.asyncio.client import connect

SECRET = sys.stdin.readline().strip()


async def main() -> None:
    # The worker reports a service as running once it accepts connections on its port.
    port = int(os.environ["MINDROOM_WORKER_SERVICE_PORT"])
    server = await asyncio.start_server(lambda _reader, writer: writer.close(), "127.0.0.1", port)
    headers = {"X-MindRoom-Service-Secret": SECRET}
    async with server, connect(os.environ["MINDROOM_AGENT_BROWSER_URL"], additional_headers=headers, max_size=None) as bridge:
        async for raw in bridge:
            message = json.loads(raw)
            if message.get("method") == "MindRoom.page" and message["params"]["page"]:
                await bridge.send(json.dumps({"id": 1, "method": "Page.startScreencast", "params": {"format": "jpeg"}}))
            elif message.get("method") == "Page.screencastFrame":
                frame = message["params"]
                Path("frame.jpg").write_bytes(base64.b64decode(frame["data"]))
                ack = {"id": 2, "method": "Page.screencastFrameAck", "params": {"sessionId": frame["sessionId"]}}
                await bridge.send(json.dumps(ack))


asyncio.run(main())
```

A plugin that uses the bridge states `"mindroom_plugin_api": "1.21"` in its manifest.

### Reaching the agent's worker

A route reaches a worker service of its own plugin, or a server the agent runs in its worker, such as a web app it started from its shell, through `await worker_relay(connection, target)` from `mindroom.plugin_http`.
`target` is the `worker_target` of an `AgentWorkspace` that `agent()` or `agents()` returned, so the caller's room and agent checks have run, or one a tool of the plugin recorded, also [across restarts](#keeping-a-target-across-restarts).
A tool with `requires_primary_runtime=True` passes its plugin's [context](#plugin-context) instead of `connection`, and only its own agent's `resolve_worker_target()`, since the relay checks no caller.
It never starts the worker: it answers 503 with `The agent's worker is not running.` until the agent's first tool call there or an `ensure_worker_service` starts it, and after idle cleanup stops it, and 503 without a dedicated worker backend.

A WebSocket service greets each message it gets, and like every service refuses a connection without its secret:

```python
# greeter_service.py
import asyncio
import hmac
import os
import sys

from websockets.asyncio.server import serve

SECRET = sys.stdin.readline().strip()


def check_secret(connection, request):
    if not hmac.compare_digest(request.headers.get("X-MindRoom-Service-Secret", ""), SECRET):
        return connection.respond(403, "Forbidden\n")
    return None


async def greet(websocket) -> None:
    async for message in websocket:
        await websocket.send(f"hello {message}")


async def main() -> None:
    port = int(os.environ["MINDROOM_WORKER_SERVICE_PORT"])
    async with serve(greet, "127.0.0.1", port, process_request=check_secret) as server:
        await server.serve_forever()


asyncio.run(main())
```

This websocket route lets a room member who may use the agent talk to it, refusing anyone else at the handshake:

```python
# routes.py
from fastapi import APIRouter, WebSocket

from mindroom.plugin_api import WorkerService, ensure_worker_service, stop_worker_service
from mindroom.plugin_http import AgentWorkspace, ClientUser, client_stream, plugin_context, stream_room_reader, worker_relay

GREETER = WorkerService("greeter", "greeter_service.py")
router = APIRouter()


@router.websocket("/rooms/{room_id}/agents/{agent_user_id}/greeter")
async def greeter(websocket: WebSocket, room_id: str, agent_user_id: str) -> None:
    agents: list[AgentWorkspace] = []

    async def may_use_agent(user: ClientUser) -> bool:
        agents.append(await (await stream_room_reader(websocket, user, room_id)).agent(agent_user_id))
        return True

    async with client_stream(websocket, room_id, admit=may_use_agent):
        target = agents[0].worker_target
        context = plugin_context(websocket)
        state = await ensure_worker_service(context, GREETER, target)
        if state.state == "exited":
            await stop_worker_service(context, GREETER, target)
            state = await ensure_worker_service(context, GREETER, target)
        if state.state != "running":
            await websocket.close(1013, f"The greeter is {state.state}.")
            return
        relay = await worker_relay(websocket, target)
        async with relay.websocket("/", service="greeter") as upstream:
            await relay.relay_messages(websocket, upstream)
```

The route starts an `exited` service again, and relays only once the ensure reports `running`, since a `starting` service does not listen yet.
The relay ends with the client's stream, which `client_stream` closes when the session ends or the caller leaves the room, with its own close code; a route that must also notice the agent's access rules changing calls `stream_room_reader(...).agent()` again as often as it needs.
Plugin routes answer JSON, never pages for a browser, so this HTTP route reads the status a server the agent started on port 5173 reports:

```python
# routes.py
from fastapi import APIRouter, Depends, HTTPException, Request

from mindroom.plugin_http import RoomReader, require_room_reader, worker_relay

router = APIRouter()


@router.get("/rooms/{room_id}/agents/{agent_user_id}/build-status")
async def build_status(request: Request, agent_user_id: str, room: RoomReader = Depends(require_room_reader)) -> object:
    relay = await worker_relay(request, (await room.agent(agent_user_id)).worker_target)
    async with relay.http("GET", "/status.json", port=5173) as answer:
        if answer.status != 200:
            raise HTTPException(502, "The agent's app did not answer.")
        return await answer.json()
```

| Call | Effect |
|------|--------|
| `async with relay.http(method, path, *, service=None, port=None, headers=(), body=None) as answer` | Sends one request, with `body` as `bytes` or streamed from an async iterator of `bytes`, and yields the answer, an `aiohttp.ClientResponse`, as it streams in |
| `async with relay.websocket(path, *, service=None, port=None, protocols=(), headers=()) as upstream` | Opens a WebSocket, its handshake carrying `headers`, and yields it, an `aiohttp.ClientWebSocketResponse`; a refused handshake raises `aiohttp.WSServerHandshakeError` with the status and headers |
| `await relay.relay_messages(websocket, upstream)` | Passes messages both ways between the route's accepted websocket and `upstream` until either side closes, then closes both with the code that ended it |
| `await relay.ports()` | The ports the relay reaches: those that servers the worker's own processes run listen on at its loopback; a refusal raises `HTTPException` with the relay's answer |

Each call names exactly one of `service`, the name of one of the calling plugin's own worker services, or `port`, a port on the worker's loopback, and raises `ValueError` otherwise.
`path` is percent-encoded and may carry a query; it always stays below the service or port, `..` included.
`http()` never follows redirects or decompresses, and the relay keeps no cookies, so a route can pass an answer through as it came.
`websocket()` never follows a redirect either: a handshake the app redirects raises `aiohttp.WSServerHandshakeError` with the app's status and headers, `Location` included.
The relay sends only the `headers` and `protocols` the route passes: never pass on the client's `Authorization` header or its `mindroom-session.<session_token>` subprotocol, which carry its MindRoom session.
A route on MindRoom's API origin passes the app only the cookies and headers it needs, never the browser's whole `Cookie` header, which there carries MindRoom's own sign-in cookies.
Of the `headers` a WebSocket's handshake carries, the app gets only `Accept-Language`, `Authorization`, `Cookie`, `Origin`, and `User-Agent`.
A route on a [plugin host](#origins-of-your-own) may pass the browser's `Origin` and the app's own cookies, which carry no MindRoom session there, but never its own gate cookie.
A service gets its secret from the relay, and never one the route sends.
The relay refuses with its own answer, marked with the `x-mindroom-relay-refused` header, so a route tells it from the app's: 403 for a port it never reaches, such as the worker runner's own or one a process outside the worker holds, 502 when nothing answers there, and 503 for a service that is not running: ensure it, and stop it first when the ensure reports it `exited`.
The marked 502 also answers a relay to a service that is still `starting`: ensure it again instead of stopping it.
A marked 403 answers a relay to a service whose port a process other than the service's own holds: such a service cannot start on that port, so stop it and ensure it again.
While an answer or WebSocket is open, idle cleanup leaves the worker running.
Without an answer from the worker, the calls raise `aiohttp.ClientError`.
A plugin that uses `worker_relay` or `worker_target` states `"mindroom_plugin_api": "1.22"` in its manifest, one that passes `headers` to `websocket()` states `"1.24"`, and one whose tool calls `worker_relay` states `"1.25"`.

This tool tells the agent whether its app on port 5173 answers yet, and once it does, returns the URL of the plugin's host `demo`, which serves nothing until the plugin's origin module decides who may open it and relays to the app, as in [A gate for an agent's app](#a-gate-for-an-agents-app):

```python
# tools.py
from fastapi import HTTPException

from mindroom.plugin_api import current_plugin_context, get_tool_runtime_context
from mindroom.plugin_http import plugin_origin_url, worker_relay


async def share_app() -> str:
    context = current_plugin_context("my-plugin")
    try:
        relay = await worker_relay(context, get_tool_runtime_context().resolve_worker_target())
    except HTTPException as refused:
        return refused.detail
    async with relay.http("GET", "/", port=5173) as answer:
        if "x-mindroom-relay-refused" in answer.headers:
            return "Nothing answers on port 5173 yet." if answer.status == 502 else (await answer.json())["detail"]
    url = plugin_origin_url(context, "demo")
    return f"The app is at {url}" if url is not None else "Plugin origins are off."
```

### Keeping a target across restarts

A target lives only in memory, so a plugin that lets people come back to something an agent started in its worker, such as a web app it shared, stores the target to reach that worker again after MindRoom restarts.
`dump_worker_target(target)` from `mindroom.plugin_api` returns a JSON-safe `dict`, and `load_worker_target(data)` returns the target again.
A tool that stores one, like `share_app` below, sets [`requires_primary_runtime=True`](#decorator-fields), since it reads its plugin's context and the call's runtime context.
`load_worker_target` raises `ValueError` for data it does not know, such as data a later MindRoom stored, and for a target whose worker is not the one its own agent and requester resolve to.
A loaded target reaches the agent's worker for the requester whose turn recorded it, for a private agent that requester's own, whoever calls the route later.
A target that `resolve_worker_target()` returned in the turn of a delegated subagent with a worker of its own reaches that subagent's worker, in memory and once loaded; a plugin that relies on this states `"mindroom_plugin_api": "1.28"` in its manifest.
So store a target with what it belongs to in the plugin's [state directory](hooks.md#plugin-state), load only what the plugin stored there itself, never a target a client sends, and before relaying a caller check that the record belongs to the room and agent the route serves and that the caller may use that agent there:

```python
# tools.py
import json
import secrets

from mindroom.plugin_api import current_plugin_context, dump_worker_target, get_tool_runtime_context


async def share_app() -> str:
    runtime = get_tool_runtime_context()
    app_id = secrets.token_hex(8)
    record = {"room_id": runtime.room_id, "agent": runtime.agent_name, "target": dump_worker_target(runtime.resolve_worker_target())}
    (current_plugin_context("my-plugin").state_root / f"app-{app_id}.json").write_text(json.dumps(record))
    return f"Shared app {app_id}."
```

```python
# routes.py
import json
import re

from fastapi import APIRouter, Depends, HTTPException, Request

from mindroom.plugin_api import load_worker_target
from mindroom.plugin_http import RoomReader, plugin_context, require_room_reader, worker_relay

router = APIRouter()


@router.get("/rooms/{room_id}/agents/{agent_user_id}/apps/{app_id}/status")
async def app_status(
    request: Request, room_id: str, agent_user_id: str, app_id: str, room: RoomReader = Depends(require_room_reader)
) -> object:
    path = plugin_context(request).state_root / f"app-{app_id}.json"
    if not re.fullmatch(r"[0-9a-f]{16}", app_id) or not path.is_file():
        raise HTTPException(404, "No such app.")
    record = json.loads(path.read_text())
    agent = await room.agent(agent_user_id)
    if (record["room_id"], record["agent"]) != (room_id, agent.name):
        raise HTTPException(404, "No such app.")
    relay = await worker_relay(request, load_worker_target(record["target"]))
    async with relay.http("GET", "/status.json", port=5173) as answer:
        return await answer.json()
```

A plugin that uses `dump_worker_target` or `load_worker_target` states `"mindroom_plugin_api": "1.26"` in its manifest.

## Origins of your own

A plugin serves pages a browser renders, such as a web app an agent runs in its worker, on hosts of its own, `<label>.<plugin>.<domain>`, where `<plugin>` is the manifest `name` and `<label>` is one DNS label the plugin picks, such as one per app it shows.
Each host is an origin of its own, so its pages share no origin with the dashboard, the API, or another plugin's hosts.

| Variable | Description | Default |
| --- | --- | --- |
| `MINDROOM_PLUGIN_ORIGIN_PORT` | Port of the listener that serves plugin hosts; setting it turns plugin origins on | _(unset; off)_ |
| `MINDROOM_PLUGIN_ORIGIN_DOMAIN` | Domain the plugin hosts are under | `localhost` |
| `MINDROOM_PLUGIN_ORIGIN_PUBLIC_PORT` | Port in plugin hosts' URLs, when browsers reach the listener through a proxy on another port | The listener's own port on `localhost`, none elsewhere |

URLs are `https`, except under `localhost`, where browsers send every `*.localhost` host to this machine, so `http://<label>.<plugin>.localhost:<port>` works without DNS.
A proxy in front of the listener must pass the browser's `Host` header on.
🔒 Put `MINDROOM_PLUGIN_ORIGIN_DOMAIN` on a registrable domain of its own, such as `example-apps.net` beside a dashboard at `example.org`.
MindRoom's API refuses cookie-authenticated changes from any other origin, but a page can set cookies for every parent domain of its host, which every other site under that domain then receives.

The manifest's `origin_module` names a module defining `router`, a FastAPI `APIRouter`, which answers every request on the plugin's hosts, at their root.
MindRoom loads it as it loads a routes module: `request.app` is MindRoom's API app, so the helpers of `mindroom.plugin_http` work in its routes, except `client_stream`, which admits only MindRoom Chat's origins, a reload closes its open websockets with 1012, and a module that fails to load costs the plugin only its origins, logging `Plugin origin module failed to load; serving no origins`.
A route reads which host it serves from `request.url.hostname`.
A host whose plugin is not loaded or has no `origin_module` answers 404, and a route that raises answers 500 without the error's detail.
A request body over 16 MiB answers 413, as on MindRoom's API.
A plugin name with `_` is not a valid DNS label, so a plugin with such a name and an `origin_module` fails to load, and `mindroom plugins check` says `Plugin 'my_plugin' cannot serve origins`.

MindRoom authenticates nothing on these hosts: the plugin's router decides who sees each page.
Every answer carries `Content-Security-Policy: frame-ancestors 'self'` followed by the origins `MINDROOM_COMPUTER_ALLOWED_ORIGINS` lists, as a policy of its own beside any the route sets, so only MindRoom Chat and the host's own pages may frame it, and a route can tighten that but never loosen it.
MindRoom adds no other header, so the route sets its own caching and content-type rules.

| Helper (`mindroom.plugin_http`) | Returns |
| --- | --- |
| `plugin_origin_url(connection, label)` | The public URL of host `label` of the plugin whose route serves `connection`, on the API or on one of its hosts, or whose tool passes its plugin's [context](#plugin-context) as `connection`; it raises `ValueError` for a `label` that is not one DNS label, raises `RuntimeError` for a plugin without an `origin_module`, and while `MINDROOM_PLUGIN_ORIGIN_PORT` is unset answers 503 with `Plugin origins are off.`, or returns `None` to a tool |
| `client_origins(connection)` | The origins `MINDROOM_COMPUTER_ALLOWED_ORIGINS` lists, MindRoom Chat's, such as for the target origin of a page's `postMessage` to the chat that frames it |
| `await stream_room_reader(request, ClientUser(user_id), room_id)` | The `RoomReader` for a user the route's own gate established, once they are joined to `room_id`, as in [Websocket routes](#websocket-routes) |

### Certificates on demand

On any host that is not a plugin host, such as `127.0.0.1`, the listener answers `GET /ask?domain=<host>`, the check Caddy's on-demand TLS makes before it gets a certificate.
It answers 200 when the host's plugin defines `async def serves(label)` in its origin module and it returns `True` within 5 seconds; it answers 404 otherwise, including for a plugin without `serves`.
A `serves` that raises or takes longer is logged as `Plugin origin serves() failed` with the plugin's name.
A `serves` that is not `async def` fails the module's load with `Plugin origin module's serves must be async def serves(label)`, since only a coroutine can be held to the deadline.
This Caddyfile serves plugin hosts under `example-apps.net` with a listener on port 8770:

```caddy
{
	on_demand_tls {
		ask http://127.0.0.1:8770/ask
	}
}

https:// {
	tls {
		on_demand
	}
	reverse_proxy 127.0.0.1:8770
}
```

### A gate for an agent's app

A plugin that shows a web app an agent runs in its worker, here one on port 5173, to the people in the room hands each of them a link that works once.
A route on the API, signed in with the caller's client session, mints the link, after checking that the caller may use the agent:

```python
# apps.py, shared by both modules
import secrets
import time
from dataclasses import dataclass


@dataclass(frozen=True)
class App:
    label: str
    user_id: str
    room_id: str
    agent_user_id: str
    port: int


LINKS: dict[str, tuple[App, float]] = {}  # one-use secret -> app, expiry
SESSIONS: dict[str, App] = {}  # cookie -> app


def mint(app: App) -> str:
    secret = secrets.token_urlsafe(32)
    LINKS[secret] = (app, time.monotonic() + 60)
    return secret
```

```python
# routes.py
import hashlib

from fastapi import APIRouter, Depends, Request

from mindroom.plugin_http import RoomReader, plugin_origin_url, require_room_reader

from .apps import App, mint

router = APIRouter()


@router.post("/rooms/{room_id}/agents/{agent_user_id}/app")
async def open_app(
    request: Request, room_id: str, agent_user_id: str, room: RoomReader = Depends(require_room_reader)
) -> dict[str, str]:
    await room.agent(agent_user_id)
    # One host per person, room, and agent, so opening the app again needs no new certificate.
    label = hashlib.sha256(f"{room.user.user_id} {room_id} {agent_user_id}".encode()).hexdigest()[:16]
    app = App(label, room.user.user_id, room_id, agent_user_id, 5173)
    return {"url": f"{plugin_origin_url(request, app.label)}/enter?link={mint(app)}"}
```

The origin module redeems the link, sets a cookie only this host gets, and on every request checks again that the caller is in the room and may use the agent, then relays to the app with `worker_relay`:

```python
# origin.py
import secrets
import time
from collections.abc import AsyncIterator
from contextlib import AsyncExitStack

from fastapi import APIRouter, HTTPException, Request, Response
from fastapi.responses import HTMLResponse, StreamingResponse

from mindroom.plugin_http import ClientUser, stream_room_reader, worker_relay

from .apps import LINKS, SESSIONS

COOKIE = "__Host-app"
router = APIRouter()


async def serves(label: str) -> bool:
    return any(app.label == label for app in SESSIONS.values()) or any(app.label == label for app, _ in LINKS.values())


def own_label(request: Request) -> str:
    return (request.url.hostname or "").split(".")[0]


@router.get("/enter")
async def enter(request: Request, link: str) -> Response:
    app, expires = LINKS.pop(link, (None, 0.0))
    if app is None or app.label != own_label(request) or expires < time.monotonic():
        raise HTTPException(403, "This link was used or has expired.")
    session = secrets.token_urlsafe(32)
    SESSIONS[session] = app
    # The app's first page loads from this host's own page, so it is a same-origin navigation too.
    response = HTMLResponse('<meta http-equiv="refresh" content="0;url=/">')
    response.headers.append("set-cookie", f"{COOKIE}={session}; Path=/; Secure; HttpOnly; SameSite=None; Partitioned")
    return response


@router.api_route("/{path:path}", methods=["GET", "HEAD", "POST", "PUT", "PATCH", "DELETE"])
async def relay(request: Request) -> Response:
    app = SESSIONS.get(request.cookies.get(COOKIE, ""))
    if app is None or app.label != own_label(request) or request.headers.get("sec-fetch-site") != "same-origin":
        raise HTTPException(403, "Open this app from its room.")
    room = await stream_room_reader(request, ClientUser(app.user_id), app.room_id)
    target = (await room.agent(app.agent_user_id)).worker_target
    # The path as the browser sent it, still percent-encoded, and only the headers the app needs, never the cookie.
    path = request.scope["raw_path"].decode() + (f"?{request.url.query}" if request.url.query else "")
    headers = [("content-type", request.headers["content-type"])] if "content-type" in request.headers else []
    relay = await worker_relay(request, target)
    stack = AsyncExitStack()
    answer = await stack.enter_async_context(
        relay.http(request.method, path, port=app.port, headers=headers, body=await request.body())
    )

    # The answer streams through as it came, so an app's large or endless answer never fills MindRoom's memory.
    async def body() -> AsyncIterator[bytes]:
        async with stack:
            async for chunk in answer.content.iter_any():
                yield chunk

    names = ("content-type", "content-encoding", "location")
    kept = {name: answer.headers[name] for name in names if name in answer.headers}
    return StreamingResponse(body(), answer.status, headers=kept)
```

Every host of the domain is same-site with every other, so a browser sends a host's cookies with requests that pages on its sibling hosts make to it.
A gate therefore admits only requests whose `Sec-Fetch-Site` is `same-origin`, as `relay` does, and never trusts its cookie alone; a websocket route checks that its `Origin` is the host's own, `plugin_origin_url(websocket, label)`.
The `__Host-` prefix keeps a page on another host from setting the cookie, and `SameSite=None` with `Partitioned` lets MindRoom Chat's frames, which are cross-site, keep it while no other site's frames get it.
Safari keeps no `Secure` cookie on `http://localhost`, so try such a gate in Safari only over HTTPS.
A plugin that uses `origin_module`, `plugin_origin_url`, or `client_origins` states `"mindroom_plugin_api": "1.23"` in its manifest, and `"1.25"` when a tool calls `plugin_origin_url`.

## People, agents, and administrators

These functions in `mindroom.plugin_api` tell people from MindRoom's own accounts and check administrators.
Each takes a `config` and `runtime_paths`, such as a hook's `ctx.config` and `ctx.runtime_paths` or a `PluginContext`'s.
They read the Matrix accounts MindRoom keeps in the primary's storage, so a tool that uses them sets [`requires_primary_runtime=True`](#decorator-fields).

| Function | Returns |
|----------|---------|
| `entity_directory(config, runtime_paths)` | A mapping from the current Matrix user ID of the router and each configured agent and team to its `ManagedEntity` |
| `is_human_user(user_id, config, runtime_paths)` | `False` for the router, an agent, a team, the [internal user](matrix.md#internal-user), and the [`bot_accounts`](authorization.md#bot-accounts), and `True` for anyone else |
| `is_platform_administrator(user_id, config, runtime_paths)` | Whether the user is in `administrators` or is a person's [bridge alias](authorization.md#bridge-aliases) for one |

A `ManagedEntity` has the entity's config `name`, its `kind` (`"router"`, `"agent"`, or `"team"`), its `display_name`, and, for an agent, the `tool_names` its config gives it: the toolkits its `tools` and the defaults it takes name, a preset replaced by its toolkits, with the toolkits those imply, less those its settings turn off, such as `memory` when its memory backend is `none`, and with those its settings add, such as `delegate` for `delegate_to`.
The directory leaves out an entity whose Matrix account MindRoom has not created yet, such as one a config reload just added.
A tool name grants nothing, so never decide what a caller may do from one.

A route only administrators may use depends on `require_platform_administrator` from `mindroom.plugin_http`, which returns the same `ClientUser` as `require_client_user`.
It answers 401 as `require_client_user` does, and 403 unless the caller is an administrator in the config MindRoom serves at that moment, so a config change that removes someone from `administrators` refuses their next request.
A plugin that uses anything in this section states `"mindroom_plugin_api": "1.5"` in its manifest, so an older MindRoom skips it with a clear reason.

## Plugin skills

Each directory listed in the manifest `skills` array becomes a skill search root, and each skill subdirectory needs a `SKILL.md`.
See [Skills](skills.md) for the `SKILL.md` format, precedence, and the per-agent `skills:` allowlist that plugin skills also require.

## Reserved namespaces

A plugin whose Matrix events clients trust, such as cards it sends, lists their prefixes in the manifest `reserved_namespaces` so agents cannot forge them.
While the plugin is loaded, the `matrix_api` tool refuses an event type starting with one of those prefixes (`Event type '...' is reserved by a plugin and cannot be written by a tool.`) and `content` holding such a key at any depth, as in the `m.new_content` of an edit (`content must not set plugin-reserved keys (...)`).
Every other built-in tool fixes the event type and top-level content keys it sends, and the plugin's own sends are not affected.
An agent whose code execution tools run in the primary process instead of a worker is fully trusted and can still send anything; see [Security posture](architecture/security-posture.md).

## Hooks

Plugins can ship typed event hooks for message enrichment, response transformation, lifecycle observation, tool-call gating, reactions, schedules, and custom events.
See [Hooks](hooks.md) for the `@hook` decorator, events, execution modes, timeouts and errors, the hook context and Matrix helpers, and testing.

## Automations

An automation is a check a plugin runs on each agent's cron schedule; when the check finds work, the agent answers a visible prompt, and code steps decide what happens after each answer.
Use one for recurring agent maintenance whose trigger or follow-up needs code, such as the built-in [`prompt_curation`](scheduling.md#prompt_curation) and [`dreaming`](scheduling.md#dreaming), which use this same API.
Operators enable it per agent under [`automations:`](scheduling.md#automations).

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
| `requester_id` | For a [private agent](scheduling.md#automations), the requester whose instance this run maintains; `None` for a shared agent |
| `execution_identity` | The identity that resolves that requester's instance, for helpers that take one; `None` for a shared agent |
| `workspace` | The workspace root, which may not exist yet: a shared agent's own workspace, or for a private agent the requester's private root |
| `state_dir` | The automation state directory of the agent, or of the requester's instance, in MindRoom storage outside the workspace; name your files after your automation and create the directory when you first write |

### What MindRoom guarantees

The rules for [scheduling](scheduling.md#automations) and [memory auto-flush](memory.md#file-auto-flush-worker) apply to plugin automations as they do to the built-ins.
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

## Testing plugins

`mindroom.plugin_testing`, from plugin API 1.16, loads a plugin in its own tests as MindRoom starts it and drives its routes, tools, and hooks without a homeserver or a model.
It patches nothing: client sessions, room membership, and each agent's [access](authorization.md#responder-access) decide as in a deployment, from the config the test gives, so a test that needs another sender grants them access there.
It imports no test runner; with pytest, a fixture wraps `load_plugin`, and async tests such as these need `pytest-asyncio`.
This example tests a plugin named `greeter` whose tool greets with its `greeting` setting, whose `message:received` hook answers `Got: <body>`, and whose route `GET /rooms/{room_id}/hello` greets a member:

```python
from pathlib import Path

import pytest

from mindroom.plugin_testing import TurnRefused, load_plugin

ROOM = "!lobby:example.org"
ALICE = "@alice:example.org"
CONFIG = {"agents": {"helper": {"display_name": "Helper", "tools": ["greeter"], "access": {"users": [ALICE]}}}}


@pytest.fixture
def plugin(tmp_path):
    with load_plugin(Path(__file__).parents[1], tmp_path, config=CONFIG, settings={"greeting": "Howdy"}) as loaded:
        loaded.add_room(ROOM, members={ALICE: "Alice", "@bob:example.org": "Bob"})
        yield loaded


def test_route(plugin):
    assert plugin.client(ALICE).get(f"/api/plugins/greeter/rooms/{ROOM}/hello").json() == {"hello": ALICE}
    assert plugin.client("@carol:example.org").get(f"/api/plugins/greeter/rooms/{ROOM}/hello").status_code == 403


@pytest.mark.asyncio
async def test_tool(plugin):
    result = await plugin.call_tool("greeter", "greet", {"name": "Ann"}, agent="helper", sender=ALICE, room=ROOM)
    assert (result.status, result.value) == ("ok", "Howdy, Ann!")


@pytest.mark.asyncio
async def test_hook(plugin):
    room = plugin.add_room("!side:example.org", members={ALICE: "Alice"})
    await plugin.fire("message:received", agent="helper", sender=ALICE, room=room.room_id, body="hi")
    assert [sent.text for sent in room.sent] == ["Got: hi"]
    with pytest.raises(TurnRefused):
        await plugin.fire("message:received", agent="helper", sender="@bob:example.org", room=ROOM, body="hi")
```

| Call | What it does |
| --- | --- |
| `load_plugin(path, tmp_dir, *, config=None, settings=None)` | Loads the plugin at `path` with `settings` for the `with` block, keeping its data under `tmp_dir`, and yields a handle with `config` and `runtime_paths`. `config` is what a `config.yaml` holds, without `plugins`; one without `models` gets a `default` model that nothing calls. A config that does not validate or a plugin that fails to load, even only its routes, origin, or lifecycle module, raises, naming why, and the block's end restores the plugins loaded before it. |
| `add_room(room_id, members, *, power_levels=None, direct=False, history_visibility="shared", key=None)` | Adds a room the router is joined to and returns it. `members` maps each joined user to their display name, `key` is the room key an agent's `rooms` or `members_of_rooms` names, and `direct` marks the members' joins as a direct message's. The router is a member too and test rooms have no name, so MindRoom also counts a room where one member is alone with the router as direct. The room's `sent` lists what hooks and `RoomMessenger` posted there, each with `text`, `thread_id`, `source` (`<plugin>:<event>`), `content`, and `mentions`. |
| `client(user_id=None)` | Returns a FastAPI `TestClient` of the plugin's routes, signed in as `user_id` when given, which works until the block ends. Its `websocket_connect` offers that session as `client_stream` expects, from an allowed origin. |
| `sign_out(user_id)` | Ends the user's client sessions, so their clients answer 401. |
| `await call_tool(tool, function, arguments, *, agent, sender, room)` | Runs one call as `agent` runs it for `sender` in `room`, through the `tool:*` hooks and with `current_plugin_context`, and returns its `status` (`ok`, `failed`, `declined`, ...), `value`, and `failure_text`. It raises `LookupError` for a tool or function the agent lacks and `PermissionError` for a function that may need approval. |
| `await fire(event, *, agent, sender, room, body="", **fields)` | Runs the hooks of a `message:*`, `system:enrich`, `subagent:changed`, or custom event for `sender`'s message `body` in `room`, and returns what they returned to MindRoom: the enrichment items, the transformed draft, the context, or `None` for a custom event. `fields` are the event context's own required fields, such as `target_entity_name` and `target_member_names` for enrichment, or for `message:before_response`, `message:final_response_transform`, `message:after_response`, and `message:cancelled` those of its draft, result, or info. `subagent:changed` takes the `subagent` snapshot (a `SubagentSnapshot`), ignores `body`, and returns its `SubagentChangedContext`. A custom event takes `payload` and `source_plugin` and runs as a tool call's `emit_custom_event` emits it, so its hooks can emit further events. |

`call_tool` and `fire` raise `TurnRefused` and run nothing unless `sender` is joined to `room` and `agent` would answer them there.
Each `call_tool` and `fire` starts from a new message of `sender`'s, which roots a thread as it would live: in thread mode `resolved_thread_id` is that message's ID, and in room mode it is None.
A sender is a person: for a bridge alias, hooks see the person it stands for as `envelope.requester_id` and the alias as `envelope.sender_id`, and tools run for the person; an agent's or bot account's ID raises `ValueError`.
A fired message mentions no one and carries no attachments.
Adding a room again replaces it, so a test changes who is joined by adding the room again with its new members.
A room never added is one no MindRoom bot is in, so a signed-in caller of a route that checks room membership gets 503 for it.
An agent's Matrix ID, to make it a room's member or a relay's mention, is its key in `entity_directory(plugin.config, plugin.runtime_paths)` from `mindroom.plugin_api`.
Once read, whether a room is a direct message is kept for a few minutes, as running agents keep it, but never shared with another `load_plugin` block.
MindRoom's environment lookups for the plugin, such as `runtime_paths.env_value`, read only a `.env` file in `tmp_dir`; plugin code that reads `os.environ` still sees the test's own environment.

The kit does not run agent turns, models, conversation history, workers, tool approval, OAuth flows, MCP server tools, automations, the plugin's `lifespan`, the main app's middleware, such as its response security headers and request size limit, or more than one plugin at once.
It fires no other built-in event, and hooks get no `matrix_admin` and cannot `put_room_state`; `RoomReader.set_modes` saves a thread mode but answers 503, as it cannot publish one.
A call to the Matrix client other than its member and state reads raises `AttributeError` naming the call.
