# Configuration

MindRoom is configured through one `config.yaml` file, optionally split across included files.
This page covers where the file lives, a minimal working configuration, which page owns each top-level section, environment variables, and the root-level settings that have no dedicated page.

## Configuration File

MindRoom searches for the configuration file in this order (first match wins):

1. `MINDROOM_CONFIG_PATH` environment variable (if set)
2. `./config.yaml` (current working directory)
3. `~/.mindroom/config.yaml` (home directory)

Data storage (`mindroom_data/`) is placed next to the config file by default.
`config.yaml` is watched at runtime, and edits are applied by hot reload without restarting MindRoom, except [event journal](https://docs.mindroom.chat/deployment/storage/#event-journal) changes, which need a restart.

Validate a specific file with:

```bash
mindroom config validate --path /path/to/config.yaml
```

## Minimal Configuration

All top-level sections are optional; configure at least one agent for conversational replies.
This configuration runs one agent in a `lobby` room, with `ANTHROPIC_API_KEY` set in the environment or the config-adjacent `.env`:

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
| `agents` | Individual AI agents | [Agents](https://docs.mindroom.chat/configuration/agents/) |
| `models` | Model providers and IDs | [Models](https://docs.mindroom.chat/configuration/models/) |
| `teams` | Multi-agent collaboration | [Teams](https://docs.mindroom.chat/configuration/teams/) |
| `router` | Message routing | [Router](https://docs.mindroom.chat/configuration/router/) |
| `defaults` | Fallback values for agent settings and global-only behavior | [Agents — Defaults](https://docs.mindroom.chat/configuration/agents/#defaults) |
| `memory` | Memory backends and embedder | [Memory](https://docs.mindroom.chat/memory/) |
| `knowledge_bases` | File-backed knowledge bases | [Knowledge Bases](https://docs.mindroom.chat/knowledge/) |
| `mcp_servers` | External Model Context Protocol servers | [MCP](https://docs.mindroom.chat/mcp/) |
| `plugins` | Plugin loading | [Plugins](https://docs.mindroom.chat/plugins/) |
| `voice` | Speech-to-text for voice messages | [Voice](https://docs.mindroom.chat/voice/) |
| `calls` | Agents joining Element Call voice calls | [Voice Calls](https://docs.mindroom.chat/voice-calls/) |
| `administrators`, `room_defaults`, `rooms`, `authorization`, `bot_accounts` | Access control, managed rooms, invitations, bridge aliases, and bridge bots | [Authorization](https://docs.mindroom.chat/authorization/) |
| `room_models` | Map of room alias to model name, the authored model default for a room | [Chat Commands](https://docs.mindroom.chat/chat-commands/) (`!room_model`) |
| `room_thread_summary_models` | Map of room alias or Matrix room ID to automatic thread summary model | [Matrix & Attachments](https://docs.mindroom.chat/tools/matrix-and-attachments/#related-matrix-runtime-features) |
| `matrix_space` | Root Matrix Space for managed rooms | [Matrix Space](https://docs.mindroom.chat/rooms/#matrix-space) |
| `timezone`, `scheduler_catch_up_grace_seconds` | Scheduled task timezone (default `UTC`) and missed-run catch-up | [Scheduling](https://docs.mindroom.chat/scheduling/) |
| `external_trigger_policy` | Inbound external triggers | [External Triggers](https://docs.mindroom.chat/external-triggers/) |
| `tool_approval` | Human approval for tool calls | [Tool Approval](https://docs.mindroom.chat/tool-approval/#tool-approval) |
| `personal_rooms` | One private room per onboarded user | [Personal Agent Rooms](https://docs.mindroom.chat/personal-rooms/#personal-rooms) |
| `prompts` | Built-in prompt overrides | [Built-In Prompt Overrides](#built-in-prompt-overrides) |
| `matrix_sync` | Matrix sync transport and limits | [Matrix Sync](https://docs.mindroom.chat/matrix/#matrix-sync) |
| `event_journal` | Storage for the Matrix event journal | [Event Journal](https://docs.mindroom.chat/deployment/storage/#event-journal) |
| `debug` | Provider request logging | [Debug Logging](https://docs.mindroom.chat/deployment/operational-log-events/#debug-logging) |
| `mindroom_user` | Internal MindRoom user account | [Internal User](https://docs.mindroom.chat/matrix/#internal-user) |

Optional tools that agents load on demand are configured as described in [Dynamic Tools](https://docs.mindroom.chat/tools/dynamic-tools/), and skills in [Skills](https://docs.mindroom.chat/skills/).

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
- Include cycles, missing files, duplicate keys across `!include_dir_merge_named` files, and duplicate filename stems under `!include_dir_named` are errors.
  Each error names the file and line of the failing tag, cycle errors show the full include chain, and duplicate errors name both files.

Editing any file in the include tree triggers the same hot reload as editing `config.yaml`.
A reload waits until the watched files have been unchanged for about a second, so a multi-file update such as `git pull` is applied in one reload.
Deleting an included file does not trigger a reload by itself; remove its `!include` reference too.
After a failed reload, fixing any file the failed attempt read triggers a retry.

When migrating an existing monolith, diff the fully merged config before and after the split:

```bash
mindroom config resolve > before.yaml
# split config.yaml into include files
mindroom config resolve > after.yaml
diff before.yaml after.yaml
```

When the configuration uses include tags, structured saves from the dashboard and self-config tools are rejected with a request to edit the source files instead; the raw config editor keeps editing the top-level file's text.
See [Dashboard](https://docs.mindroom.chat/dashboard/) for the API responses.
`MINDROOM_CONFIG_TEMPLATE` seeding copies only the single template file, so bundled templates that use includes must ship the whole directory.

See [Tool Approval](https://docs.mindroom.chat/tool-approval/#tool-approval).

## Environment Variables

Set `LOG_LEVEL`, `MINDROOM_LOGGER_LEVELS`, `MINDROOM_LOG_FORMAT`, `NO_COLOR`, and `MINDROOM_TIMING` in the process environment, because the config-adjacent `.env` does not supply them.
Container `env_file`/`--env-file` injection sets process variables and therefore works.

### Core

| Variable | Description | Default |
|----------|-------------|---------|
| `MINDROOM_CONFIG_PATH` | Path to `config.yaml` | `./config.yaml`, then `~/.mindroom/config.yaml` |
| `MINDROOM_STORAGE_PATH` | Data storage directory | `mindroom_data/` next to config |
| `MINDROOM_SESSION_STORAGE_PATH` | Separate root for agent and team session databases; relative paths resolve from the config directory, and other state stays under `MINDROOM_STORAGE_PATH` | `MINDROOM_STORAGE_PATH` |
| `MINDROOM_CONFIG_TEMPLATE` | Template copied to the config path when `config.yaml` does not exist, used by Docker images to seed config | Same as config path |
| `MINDROOM_CREDENTIALS_ENCRYPTION_KEY` | Key for encrypted-at-rest credential files (see [Credential Storage Encryption](https://docs.mindroom.chat/oauth-framework/#credential-storage-encryption)) | unset |
| `LOG_LEVEL` | Logging level for `mindroom run` (`DEBUG`, `INFO`, `WARNING`, `ERROR`); `mindroom run --log-level` takes precedence | `INFO` |
| `MINDROOM_LOGGER_LEVELS` | Comma- or semicolon-separated logger level overrides, for example `mindroom:DEBUG,httpx:WARNING,nio:WARNING` | unset |
| `MINDROOM_LOG_FORMAT` | `text` for readable logs or `json` for structured logs | `text` |
| `NO_COLOR` | Any nonempty value disables console colors and styled tracebacks; installed background services set this to `1` | unset |

### Matrix

| Variable | Description | Default |
|----------|-------------|---------|
| `MATRIX_HOMESERVER` | Matrix homeserver URL | `http://localhost:8008` |
| `MATRIX_SERVER_NAME` | Server name for federation | _(derived from homeserver)_ |
| `MATRIX_SSL_VERIFY` | Verify the TLS certificate of `MATRIX_HOMESERVER`; provisioning service requests and other homeservers are always verified | `true` |
| `MINDROOM_MATRIX_HOMESERVER_STARTUP_TIMEOUT_SECONDS` | Seconds to wait at startup for the homeserver to respond (`0` = wait indefinitely); a certificate verification failure stops startup immediately (see [Matrix TLS trust](https://docs.mindroom.chat/matrix/#tls-trust)) | _(wait indefinitely)_ |
| `MINDROOM_MATRIX_SYNC_STARTUP_TIMEOUT_SECONDS` | Positive seconds allowed for the first Matrix sync response | `600` |
| `MINDROOM_MATRIX_INGESTION_GRACE_SECONDS` | Positive seconds the sync watchdog and `/api/health` keep waiting while event catch-up is still making progress; set it above the deployment's observed healthy catch-up time | `600` |

Desktop setup variables are listed in [Desktop](https://docs.mindroom.chat/tools/desktop/).

MindRoom uses `mindroom-nio` for Matrix communication with SSL context handling and encryption key storage.


| Variable | Default | Description |
|----------|---------|-------------|
| `MATRIX_HOMESERVER` | `http://localhost:8008` | Matrix homeserver URL |
| `MATRIX_SERVER_NAME` | (from homeserver) | Federation server name |
| `MATRIX_SSL_VERIFY` | `true` | Set to `false` for a dev or self-signed homeserver; it covers `MATRIX_HOMESERVER` and the MatrixRTC discovery and authorization requests made for it, but not the provisioning service or any other homeserver, such as the desktop bridge's |
| `MATRIX_MANAGED_ACCOUNT_AUTH` | `password` | Authentication for accounts created and operated by MindRoom: `password` or `appservice` |
| `MATRIX_APPSERVICE_TOKEN` | -- | Application-service token used when managed account auth is `appservice` |
| `MATRIX_APPSERVICE_TOKEN_FILE` | -- | File alternative to `MATRIX_APPSERVICE_TOKEN` |

Streaming behavior is configured in `config.yaml` with `defaults.enable_streaming` (default: `true`).

### Model Providers

Provider API keys such as `ANTHROPIC_API_KEY` and `OPENAI_API_KEY`, their `_FILE` variants, and Vertex AI and Bedrock settings are listed in [Models — Environment Variables](https://docs.mindroom.chat/configuration/models/#environment-variables), and `codex login` and `CODEX_HOME` in [Codex Models with ChatGPT Login](https://docs.mindroom.chat/configuration/models/#codex-models-with-chatgpt-login).

| Variable | Description |
|----------|-------------|
| `OPENAI_BASE_URL` | Base URL for `provider: openai` models, such as a local inference server; the process environment wins over `.env`, a model's `extra_kwargs.base_url` overrides it, and `extra_kwargs.client_params.base_url` overrides both |

See [Credential Storage](https://docs.mindroom.chat/oauth-framework/#credential-storage).

### Dashboard and API

| Variable | Description | Default |
|----------|-------------|---------|
| `MINDROOM_API_KEY` | API key for dashboard/API requests; `mindroom config init` and first-run `mindroom run` add a generated key to `.env` when it has none; unset or empty means open access | _(none)_ |
| `MINDROOM_PORT` | Port used by Google OAuth callback URL construction and deployment tooling; the API server bind port is set with `mindroom run --api-port` instead | `8765` |
| `MINDROOM_DASHBOARD_ALLOWED_HOSTS` | Comma-separated extra host names that unauthenticated dashboard or `/v1` requests may address (see [Authorization](https://docs.mindroom.chat/authorization/#dashboard-configuration)) | _(none)_ |
| `MINDROOM_DASHBOARD_CORS_ALLOWED_ORIGINS` | Comma-separated origins allowed credentialed dashboard CORS responses; cookie and trusted-upstream mutations still require the app's own origin | `http://localhost:3003`, `http://localhost:5173`, `http://127.0.0.1:3003`, `http://127.0.0.1:5173` |
| `MINDROOM_DASHBOARD_CORS_ALLOW_ALL_ORIGINS` | `true` allows every dashboard API origin while disabling credentialed CORS responses; without dashboard authentication, origins outside `MINDROOM_DASHBOARD_ALLOWED_HOSTS` are still refused | _(unset)_ |
| `OPENAI_COMPAT_API_KEYS` | Comma-separated API keys for `/v1/*` requests (see [OpenAI-Compatible API](https://docs.mindroom.chat/openai-api/)) | _(none; `/v1` is locked without this or the flag below)_ |
| `OPENAI_COMPAT_ALLOW_UNAUTHENTICATED` | `true` allows unauthenticated `/v1/*` access for local development only | _(unset)_ |
| `MINDROOM_AUTO_BUILD_FRONTEND` | Set to `0` to skip the automatic dashboard frontend build | _(enabled)_ |

Hosted pairing variables written by `mindroom connect` are described in [Hosted Matrix](https://docs.mindroom.chat/deployment/hosted-matrix/).

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

See [Background Scripts](https://docs.mindroom.chat/tools/background-scripts/#worker-and-network-requirements) for when each script gateway variable is required, and [Sandbox Proxy](https://docs.mindroom.chat/deployment/sandbox-proxy/#environment-variable-reference) for the other `MINDROOM_SANDBOX_*` and Kubernetes worker variables.

See [Credential Seeds](https://docs.mindroom.chat/oauth-framework/#credential-seeds), [Credential Storage Encryption](https://docs.mindroom.chat/oauth-framework/#credential-storage-encryption), [Event Journal](https://docs.mindroom.chat/deployment/storage/#event-journal), [Matrix Sync](https://docs.mindroom.chat/matrix/#matrix-sync), [Automatic Restart Resumption](https://docs.mindroom.chat/configuration/threads/#automatic-restart-resumption), and [Debug Logging](https://docs.mindroom.chat/deployment/operational-log-events/#debug-logging).

## Built-In Prompt Overrides

Use the optional root `prompts` block to override MindRoom's built-in prompts without editing Python code.
Keys must match the uppercase global names in `src/mindroom/prompts.py` exactly, and unknown keys fail config validation.

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

The allowed names are listed in `PROMPT_DEFAULT_NAMES` and the allowed placeholder fields per prompt in `PROMPT_TEMPLATE_FIELDS`, both in `mindroom.prompts`.
Unsupported placeholders fail validation at config load.
Placeholders are exact `{field_name}` replacements, not Jinja or Python `str.format`, so `{message.text}`, `{message!r}`, and `{message:.2f}` are rejected.
Escape literal braces as `{{` and `}}` in overrides that use placeholders.
Changed prompt overrides take effect through hot reload.
Overrides built into agents when they start, such as `HIDDEN_TOOL_CALLS_PROMPT`, restart every agent, team, and the router when changed; the others apply without restarts.

See [Managed Avatars](https://docs.mindroom.chat/matrix/#managed-avatars), [Internal User](https://docs.mindroom.chat/matrix/#internal-user), and [Personal Agent Rooms](https://docs.mindroom.chat/personal-rooms/#personal-rooms).
