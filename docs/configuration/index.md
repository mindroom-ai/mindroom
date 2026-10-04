---
icon: lucide/settings
---

# Configuration

MindRoom is configured through one `config.yaml` file, optionally split across included files, plus environment variables.
This page covers where the file lives, a minimal working configuration, which page owns each top-level section, splitting the file, built-in prompt overrides, and the environment variable reference.

## Configuration File

If `MINDROOM_CONFIG_PATH` is set, MindRoom uses that path, even when no file exists there.
Otherwise, it uses `./config.yaml` in the current working directory if present, then `~/.mindroom/config.yaml`.

Data storage (`mindroom_data/`) is placed next to the config file by default.

Validate a file with:

```bash
mindroom config validate --path /path/to/config.yaml
```

Edits to `config.yaml` apply by hot reload without restarting MindRoom, except [event journal](../deployment/storage.md#event-journal) changes, which need a restart.
A reload waits for active responses to finish, for at most 600 seconds.
To confirm that a reload finished, use [`mindroom config check-applied`](../deployment/config-bundles.md#config-fingerprint-and-config-check-applied).

## Minimal Configuration

All top-level sections are optional; configure at least one agent for conversational replies.
This configuration runs one agent in a `lobby` room, with `ANTHROPIC_API_KEY` set in the environment or in the `.env` next to `config.yaml`:

```yaml
models:
  default:
    provider: anthropic
    id: claude-sonnet-5-5

agents:
  assistant:
    display_name: Assistant
    role: A helpful AI assistant
    model: default
    rooms: [lobby]

timezone: America/Los_Angeles
```

Keep `models.default` configured, because room topic generation and automatic thread summaries fall back to it even when agents, teams, and the router select other models.

## Top-Level Sections

| Section | Purpose | Documented in |
| --- | --- | --- |
| `agents` | Individual AI agents | [Agents](agents.md) |
| `models` | Model providers and IDs | [Models](models.md) |
| `teams` | Multi-agent collaboration | [Teams](teams.md) |
| `router` | Message routing | [Router](router.md) |
| `defaults` | Fallback values for agent settings and global-only behavior | [Agents — Defaults](agents.md#defaults) |
| `memory` | Memory backends and embedder | [Memory](../memory.md) |
| `knowledge_bases` | File-backed knowledge bases | [Knowledge Bases](../knowledge.md) |
| `mcp_servers` | External Model Context Protocol servers | [MCP](../mcp.md) |
| `plugins` | Plugin loading | [Plugins](../plugins.md) |
| `voice` | Speech-to-text for voice messages | [Voice](../voice.md) |
| `calls` | Agents joining Element Call voice calls | [Voice Calls](../voice-calls.md#calls-configuration) |
| `administrators`, `authorization`, `bot_accounts` | Platform administrators, command access, bridge aliases, and bridge bots | [Authorization](../authorization.md) |
| `room_defaults`, `rooms` | Managed room policy, invitations, and room admins | [Rooms & Spaces](../rooms.md#room-policy) |
| `room_models` | Map of managed room name to model name, the authored model default for a room | [Models](models.md#room_model) (`!room_model`) |
| `room_thread_summary_models` | Map of room alias or Matrix room ID to automatic thread summary model | [Matrix & Attachments](../tools/matrix-and-attachments.md#automatic-thread-summaries) |
| `matrix_space` | Root Matrix Space for managed rooms | [Matrix Space](../rooms.md#matrix-space) |
| `timezone`, `scheduler_catch_up_grace_seconds` | Scheduled task timezone (default `UTC`) and missed-run catch-up | [Scheduling](../scheduling.md) |
| `external_trigger_policy` | Inbound external triggers | [External Triggers](../external-triggers.md) |
| `tool_approval` | Human approval for tool calls | [Tool Approval](../tool-approval.md#tool-approval) |
| `personal_rooms` | One private room per onboarded user | [Personal Rooms](../personal-rooms.md#personal-rooms) |
| `prompts` | Built-in prompt overrides | [Built-In Prompt Overrides](#built-in-prompt-overrides) |
| `matrix_sync` | Matrix sync transport and limits | [Matrix Sync](../matrix.md#matrix-sync) |
| `event_journal` | Storage for the Matrix event journal | [Event Journal](../deployment/storage.md#event-journal) |
| `debug` | Provider request logging | [Debug Logging](../deployment/operational-log-events.md#debug-logging) |
| `mindroom_user` | Internal MindRoom user account | [Internal User](../matrix.md#internal-user) |

Optional tools that agents load on demand are configured as described in [Dynamic Tools](../tools/dynamic-tools.md), and skills in [Skills](../skills.md).

## Splitting the Configuration Into Multiple Files

Large configs can be split across multiple files with Home-Assistant-style include tags.

| Tag | Result |
|-----|--------|
| `!include rel/path.yaml` | The parsed content of that YAML file, with nested include tags resolved recursively |
| `!expand rel/path.yaml` | Splices the file's YAML list into the surrounding list at this item |
| `!include_text rel/path.md` | The file's raw text as a string (one trailing newline stripped), for long prompt or instruction blocks |
| `!include_dir_list rel/dir` | A list with one item per YAML file in the directory |
| `!include_dir_named rel/dir` | A mapping of filename-without-extension to each file's parsed content |
| `!include_dir_merge_list rel/dir` | The concatenation of the lists contained in each file |
| `!include_dir_merge_named rel/dir` | The merge of the mappings contained in each file |

A realistic split keeps each agent in its own file, shared lists in `_`-prefixed snippets, and long prompts in Markdown:

```yaml
# config.yaml
agents: !include_dir_merge_named agents/
models: !include models.yaml
```

```yaml
# agents/researcher.yaml
researcher:
  display_name: Researcher
  role: Deep research and analysis
  model: default
  tools:
    - !expand _shared/web_tools.yaml
    - calculator
  instructions:
    - !include_text ../prompts/researcher.md
```

```yaml
# agents/_shared/web_tools.yaml
- duckduckgo
- website
- browser
```

Here `tools` resolves to `[duckduckgo, website, browser, calculator]`.

- Relative paths resolve against the directory of the file containing the tag.
- Absolute paths are rejected, and every include must stay inside the top-level config file's directory after symlinks and `..` are resolved.
- Explicit include paths may not contain hidden components starting with `.`, so `!include_text .env` fails.
- Directory includes recurse into subdirectories, take only `.yaml`/`.yml` files in lexicographic order of their relative path, and skip files and directories whose name starts with `.` or `_`.
- YAML anchors cannot cross an include boundary, so share values through `_`-prefixed snippet files.
- `!expand` is only valid as a list item, may appear several times in one list, and splices one level while keeping order and duplicates.
  The expanded file must be a YAML list; `[]` adds nothing, while an empty file, `null`, a scalar, or a mapping is an error.
  An ordinary `- !include tools.yaml` inserts the included value as one item, and `tools: !include tools.yaml` replaces the whole list.
- An empty included file resolves to `null` under `!include` and contributes nothing to directory includes.
- Include cycles, missing files, duplicate keys across `!include_dir_merge_named` files, and duplicate filename stems under `!include_dir_named` are errors that name the file and line of the failing tag.

Editing any file in the include tree triggers the same hot reload as editing `config.yaml`, and a multi-file update such as `git pull` applies as one reload.
Adding a file to an included directory or deleting an included file does not trigger a reload by itself; save `config.yaml` again (for example with `touch config.yaml`) or restart MindRoom to apply it.
After a failed reload, fixing any file the failed attempt read triggers a retry.

When migrating an existing monolith, diff the fully merged config before and after the split:

```bash
mindroom config resolve > before.yaml
# split config.yaml into include files
mindroom config resolve > after.yaml
diff before.yaml after.yaml
```

When the configuration uses include tags, structured saves from the dashboard and the self-config tools are rejected, so edit the source files instead; the raw config editor still edits the top-level file's text (see [Dashboard](../dashboard.md#configurations-split-with-include)).
`MINDROOM_CONFIG_TEMPLATE` copies only the single template file, so a template that uses includes needs its included files shipped next to the config path.

## Built-In Prompt Overrides

Use the optional root `prompts` block to replace MindRoom's built-in prompts, keyed by the prompt's uppercase name:

```yaml
prompts:
  HIDDEN_TOOL_CALLS_PROMPT: |
    Tool calls are hidden from users.
    Answer directly without narrating internal tool use.
  ROUTER_AGENT_SELECTION_PROMPT_TEMPLATE: |
    Decide which agent or team should respond to this message.

    Available agents and teams:

    {agents_info}

    User message: {message}

    Return only the agent or team name.
```

An unknown key fails config validation with an error that lists every allowed prompt name.
Template prompts accept only their own `{field_name}` placeholders, and an unsupported placeholder fails validation with an error that lists the allowed ones.
Placeholders are exact `{field_name}` replacements, not Jinja or Python `str.format`, so `{message.text}`, `{message!r}`, and `{message:.2f}` are rejected.
Escape literal braces as `{{` and `}}` in overrides that use placeholders.
Changed overrides apply through hot reload.
Changing a prompt that agents build in when they start, such as `HIDDEN_TOOL_CALLS_PROMPT` or `AGENT_IDENTITY_CONTEXT_TEMPLATE`, restarts every agent, team, and the router; other prompt changes apply without restarts.

## Environment Variables

Set variables in the process environment or in the `.env` file next to `config.yaml`; the process environment wins.
`MINDROOM_CONFIG_PATH`, `LOG_LEVEL`, `MINDROOM_LOGGER_LEVELS`, `MINDROOM_LOG_FORMAT`, `NO_COLOR`, and [`MINDROOM_TIMING`](../deployment/operational-log-events.md#debug-logging) are read only from the process environment, so the `.env` file does not set them.
Container `env_file`/`--env-file` injection sets process variables and therefore works.

### Core

| Variable | Description | Default |
|----------|-------------|---------|
| `MINDROOM_CONFIG_PATH` | Path to `config.yaml` | `./config.yaml`, then `~/.mindroom/config.yaml` |
| `MINDROOM_STORAGE_PATH` | Data storage directory | `mindroom_data/` next to config |
| `MINDROOM_SESSION_STORAGE_PATH` | Separate root for agent and team session databases; relative paths resolve from the config directory, and other state stays under `MINDROOM_STORAGE_PATH` (see [Storage](../deployment/storage.md)) | `MINDROOM_STORAGE_PATH` |
| `MINDROOM_CONFIG_TEMPLATE` | File copied to the config path when `config.yaml` does not exist, used by Docker images to seed config | _(unset)_ |
| `MINDROOM_CREDENTIALS_ENCRYPTION_KEY` | Key for encrypted-at-rest credential files (see [Credential Storage Encryption](../oauth-framework.md#credential-storage-encryption)) | _(unset)_ |
| `LOG_LEVEL` | Logging level for `mindroom run` (`DEBUG`, `INFO`, `WARNING`, `ERROR`); `mindroom run --log-level` takes precedence | `INFO` |
| `MINDROOM_LOGGER_LEVELS` | Comma- or semicolon-separated logger level overrides, for example `mindroom:DEBUG,httpx:WARNING,nio:WARNING` | _(unset)_ |
| `MINDROOM_LOG_FORMAT` | `text` for readable logs or `json` for structured logs | `text` |
| `NO_COLOR` | Any nonempty value disables console colors and styled tracebacks; installed background services set this to `1` | _(unset)_ |

### Matrix

| Variable | Description | Default |
|----------|-------------|---------|
| `MATRIX_HOMESERVER` | Matrix homeserver URL | `http://localhost:8008` |
| `MATRIX_SERVER_NAME` | Server name for federation | _(derived from homeserver)_ |
| `MATRIX_SSL_VERIFY` | Set to `false` for a dev or self-signed homeserver; it covers `MATRIX_HOMESERVER` and its MatrixRTC voice-call requests, never the provisioning service or other homeservers (see [TLS Trust](../matrix.md#tls-trust)) | `true` |
| `MATRIX_REGISTRATION_TOKEN` | Registration token for creating managed agent accounts on a self-hosted homeserver | _(unset)_ |
| `MATRIX_REGISTRATION_SHARED_SECRET`, `MATRIX_REGISTRATION_SHARED_SECRET_FILE` | Synapse shared registration secret, or a file containing it, for creating managed agent accounts without pairing | _(unset)_ |
| `MATRIX_MANAGED_ACCOUNT_AUTH` | `password` or `appservice` authentication for accounts MindRoom creates and operates (see [Agent Users](../matrix.md#agent-users)) | `password` |
| `MATRIX_APPSERVICE_TOKEN`, `MATRIX_APPSERVICE_TOKEN_FILE` | Application-service token, or a file containing it, used when `MATRIX_MANAGED_ACCOUNT_AUTH=appservice` | _(unset)_ |
| `MINDROOM_MATRIX_HOMESERVER_STARTUP_TIMEOUT_SECONDS` | Seconds to wait at startup for the homeserver to respond (`0` waits indefinitely); a certificate verification failure stops startup immediately | _(wait indefinitely)_ |
| `MINDROOM_MATRIX_SYNC_STARTUP_TIMEOUT_SECONDS` | Positive seconds allowed for the first Matrix sync response | `600` |
| `MINDROOM_MATRIX_INGESTION_GRACE_SECONDS` | Positive seconds the sync watchdog and `/api/health` keep waiting while event catch-up is still making progress; set it above the deployment's observed healthy catch-up time | `600` |

Hosted pairing variables written by `mindroom connect` are described in [Hosted Matrix](../deployment/hosted-matrix.md#connect), and desktop setup variables in [Desktop](../tools/desktop.md).

### Model Providers

Provider API keys such as `ANTHROPIC_API_KEY` and `OPENAI_API_KEY`, their `_FILE` variants, and Vertex AI and Bedrock settings are listed in [Models — Environment Variables](models.md#environment-variables), and `codex login` and `CODEX_HOME` in [Codex Models with ChatGPT Login](models.md#codex-models-with-chatgpt-login).
Provider keys from the environment or `.env` are also copied into the shared credentials store, as described in [Automatic Credential Import](../oauth-framework.md#automatic-credential-import).

| Variable | Description |
|----------|-------------|
| `OPENAI_BASE_URL` | Base URL for `provider: openai` models, such as a local inference server; a model's `extra_kwargs.base_url` overrides it, and `extra_kwargs.client_params.base_url` overrides both |

### Dashboard and API

| Variable | Description | Default |
|----------|-------------|---------|
| `MINDROOM_API_KEY` | API key for dashboard/API requests; `mindroom config init` and first-run `mindroom run` add a generated key to `.env` when it has none; unset or empty means open access | _(none)_ |
| `MINDROOM_PORT` | Port in the default OAuth callback URL when `MINDROOM_PUBLIC_URL` and `MINDROOM_BASE_URL` are unset, also read by deployment tooling; set the API server bind port with `mindroom run --api-port` instead | `8765` |
| `MINDROOM_DASHBOARD_ALLOWED_HOSTS` | Comma-separated extra host names that unauthenticated dashboard or `/v1` requests may address (see [Authorization](../authorization.md#unauthenticated-dashboard-host-names)) | _(none)_ |
| `MINDROOM_DASHBOARD_CORS_ALLOWED_ORIGINS` | Comma-separated origins allowed credentialed dashboard CORS responses; cookie and trusted-upstream mutations still require the app's own origin | `http://localhost:3003`, `http://localhost:5173`, `http://127.0.0.1:3003`, `http://127.0.0.1:5173`, or the hosted public origins when `MINDROOM_PUBLIC_URL` is set |
| `MINDROOM_DASHBOARD_CORS_ALLOW_ALL_ORIGINS` | `true` allows every dashboard API origin while disabling credentialed CORS responses; without dashboard authentication, origins outside `MINDROOM_DASHBOARD_ALLOWED_HOSTS` are still refused | _(unset)_ |
| `MINDROOM_ENABLE_API_DOCS` | `false` hides the generated API docs at `/docs`, `/redoc`, and `/openapi.json` | `true` (`false` on hosted platform instances) |
| `OPENAI_COMPAT_API_KEYS` | Comma-separated API keys for `/v1/*` requests (see [OpenAI-Compatible API](../openai-api.md)) | _(none; `/v1` is locked without this or the flag below)_ |
| `OPENAI_COMPAT_ALLOW_UNAUTHENTICATED` | `true` allows unauthenticated `/v1/*` access for local development only | _(unset)_ |
| `OPENAI_COMPAT_API_KEY_REQUESTERS` | JSON object mapping `/v1` API keys to Matrix user IDs (see [Requester identities](../openai-api.md#requester-identities-and-delegation)) | _(none)_ |
| `MINDROOM_AUTO_BUILD_FRONTEND` | Set to `0` to skip the automatic dashboard frontend build | _(enabled)_ |

### Workers and Background Scripts

| Variable | Description | Default |
|----------|-------------|---------|
| `MINDROOM_NAMESPACE` | Installation namespace for Matrix identity isolation (4–32 lowercase alphanumeric chars) | _(none)_ |
| `MINDROOM_NO_AUTO_INSTALL_TOOLS` | `1`/`true`/`yes` disables automatic tool dependency installation | _(unset; auto-install enabled)_ |
| `MINDROOM_WORKER_BACKEND` | Worker backend for tool execution (`static_runner`, `docker`, or `kubernetes`) | `static_runner` |
| `MINDROOM_SANDBOX_PROXY_URL` | Sandbox proxy endpoint URL (static runner) | _(none)_ |
| `MINDROOM_SANDBOX_PROXY_TOKEN` | Auth token for the sandbox proxy | _(none)_ |
| `MINDROOM_SCRIPT_GATEWAY_URL` | Worker-reachable background-script gateway base URL, including `/api/script-gateway` | _(none)_ |
| `MINDROOM_SCRIPT_GATEWAY_PORT` | Port for a second listener that serves only the script gateway | _(none)_ |
| `MINDROOM_SCRIPT_GATEWAY_ISOLATED` | `true` attests that the Kubernetes script-gateway listener exposes only the gateway | `false` |
| `MINDROOM_KUBERNETES_DEFAULT_SCRIPT_RESOURCE_PROFILE` | Default Kubernetes background-script profile (`small`, `standard`, or `large`) when `start_script` omits `resource_profile` | `small` |
| `MINDROOM_KUBERNETES_SCRIPT_RESOURCE_PROFILES_JSON` | JSON object defining CPU and memory requests and limits for the `small`, `standard`, and `large` profiles | Built-in profiles |
| `MINDROOM_SCRIPT_RETENTION_SECONDS` | Positive seconds to retain finished background-script runs, tool-call receipts, and approval records | `2592000` (30 days) |

See [Background Scripts](../tools/background-scripts.md#worker-and-network-requirements) for when each script gateway variable is required, and [Sandbox Proxy](../deployment/sandbox-proxy.md#environment-variable-reference) for the other `MINDROOM_SANDBOX_*` and Kubernetes worker variables.
Credential seed variables are described in [Credential Seeds](../oauth-framework.md#credential-seeds).
