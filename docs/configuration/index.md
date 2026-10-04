---
icon: lucide/settings
---

# Configuration

MindRoom is configured through one `config.yaml` file, optionally split across included files.
This page covers where the file lives, a minimal working configuration, which page owns each top-level section, environment variables, and the root-level settings that have no dedicated page.

## Configuration File

MindRoom searches for the configuration file in this order (first match wins):

1. `MINDROOM_CONFIG_PATH` environment variable (if set)
2. `./config.yaml` (current working directory)
3. `~/.mindroom/config.yaml` (home directory)

Data storage (`mindroom_data/`) is placed next to the config file by default.
`config.yaml` is watched at runtime, and edits are applied by hot reload without restarting MindRoom, except [event journal](#event-journal) changes, which need a restart.

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
| `calls` | Agents joining Element Call voice calls | [Voice Calls](../voice-calls.md) |
| `administrators`, `room_defaults`, `rooms`, `authorization`, `bot_accounts` | Access control, managed rooms, invitations, bridge aliases, and bridge bots | [Authorization](../authorization.md) |
| `room_models` | Map of room alias to model name, the authored model default for a room | [Chat Commands](../chat-commands.md) (`!room_model`) |
| `room_thread_summary_models` | Map of room alias or Matrix room ID to automatic thread summary model | [Matrix & Attachments](../tools/matrix-and-attachments.md#related-matrix-runtime-features) |
| `matrix_space` | Root Matrix Space for managed rooms | [Matrix Space](../matrix-space.md) |
| `timezone`, `scheduler_catch_up_grace_seconds` | Scheduled task timezone (default `UTC`) and missed-run catch-up | [Scheduling](../scheduling.md) |
| `external_trigger_policy` | Inbound external triggers | [External Triggers](../external-triggers.md) |
| `tool_approval` | Human approval for tool calls | [Tool Approval](#tool-approval) |
| `personal_rooms` | One private room per onboarded user | [Personal Agent Rooms](#personal-agent-rooms) |
| `prompts` | Built-in prompt overrides | [Built-In Prompt Overrides](#built-in-prompt-overrides) |
| `matrix_sync` | Matrix sync transport and limits | [Matrix Sync](#matrix-sync) |
| `event_journal` | Storage for the Matrix event journal | [Event Journal](#event-journal) |
| `debug` | Provider request logging | [Debug Logging](#debug-logging) |
| `mindroom_user` | Internal MindRoom user account | [Internal User](#internal-user) |

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
See [Dashboard](../dashboard.md) for the API responses.
`MINDROOM_CONFIG_TEMPLATE` seeding copies only the single template file, so bundled templates that use includes must ship the whole directory.

## Tool Approval

<video controls playsinline preload="metadata" aria-label="An agent pauses for approval before it books a meeting" style="width: 100%">
  <source src="https://github.com/user-attachments/assets/6a2033ea-3354-4afd-9d58-fc617b18cd24#t=0.1" type="video/mp4" media="(prefers-color-scheme: dark)">
  <source src="https://github.com/user-attachments/assets/d62d98e8-c066-4e1f-8a8f-d840da7b0bd1#t=0.1" type="video/mp4">
</video>

Use the top-level `tool_approval` block to gate tool calls behind human approval in Matrix conversations.
This example gates Slack message sending and file uploads, plus shell calls selected by a review script:

```yaml
tool_approval:
  default: auto_approve
  timeout_days: 7
  rules:
    - match: send_message*
      action: require_approval
    - match: upload_file
      action: require_approval
    - match: run_shell_command
      script: ./approval_scripts/shell_review.py
      timeout_days: 3
```

| Field | Type | Default | Meaning |
| --- | --- | --- | --- |
| `default` | `auto_approve` or `require_approval` | `auto_approve` | Decision for calls that no rule matches |
| `timeout_days` | number | `7` | How long an approval card stays open before it expires |
| `rules` | list | `[]` | Ordered rules; the first matching rule wins |
| `rules[].match` | string | Required | Case-sensitive glob over exposed function names, not toolkit identifiers, so the same function name in another toolkit also matches |
| `rules[].action` | `auto_approve` or `require_approval` | — | Fixed decision; set exactly one of `action` or `script` |
| `rules[].script` | string | — | Config-relative Python file defining `check(tool_name, arguments, agent_name) -> bool`; approval is required only when it returns `True` |
| `rules[].timeout_days` | number | `tool_approval.timeout_days` | Expiry window for this rule |

### Approving and Denying

- React to the approval card with `✅` to approve the tool call.
- Reply to the card to deny the call; the start of the reply is recorded as the denial reason.
- Only the original human requester can approve or deny their pending call.
- Eligible cards also offer auto-approval for 5, 10, or 30 minutes.
  It covers the original call, matching pending calls, and later calls for the same room, thread, requester, agent, and exact tool operation, with any arguments.
  It needs a thread and complete reviewable arguments, and is not offered for native tool-authored confirmations or background-script approvals.
  The requester can stop auto-approval from the originating card, and changes to configured bindings or room membership end matching grants; calls already approved still run.

While approval is pending, the agent stops typing and the conversation can continue.
Pending approvals survive restarts and config reloads, and an approved call resumes the paused response.
The approved call runs only with the exact arguments shown on the card.
Tool calls authored by agents, the system, or configured bridge bots are denied instead of entering the approval flow.
An agent acting on a request relayed by another agent's reply asks the original human for approval.
See [Agents](agents.md#configuration-options) when approval fails because the router is not in the room.

### Approval Card Contents

Cards show a redacted preview of the tool arguments, and clients can expand the complete redacted arguments when the preview is truncated.
Fields whose names mark them as secret, such as `password`, `credentials`, or an `Authorization` header, are hidden while their field name stays visible.
Inside text, only credentials in known token formats, such as `sk-…`, `ghp_…`, `github_pat_…`, `xoxb-…`, `AIza…`, and JSON Web Tokens, are replaced by numbered placeholders like `⟦secret-1⟧`; equal tokens get the same number.
Other inline secrets, such as `export DB_PASSWORD=hunter2` or an inline `Authorization: Basic …` header, stay visible, so keep secrets in credentials or secret-named fields.
Deny the call when a hidden field or a placeholder where a command, host, path, or delimiter belongs could change what runs.
A card cannot be approved when its complete redacted arguments exceed 2 MB, nest deeper than 32 levels, or fail to upload; approving it then denies the call.

### Tools That Cannot Pause for Approval

The OpenAI-compatible `/v1/chat/completions` API has no approval transport, so it hides every tool function that matches a required-approval rule, including script rules.
Skill, knowledge-search, and learning functions such as `get_skill_script`, `search_knowledge_base`, and `update_user_memory` cannot pause for approval on any channel, so they are hidden when a required-approval rule or `default: require_approval` applies to them.
Add an `auto_approve` rule for such a function to keep it available.

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
| `MINDROOM_CREDENTIALS_ENCRYPTION_KEY` | Key for encrypted-at-rest credential files (see [Credential Storage Encryption](#credential-storage-encryption)) | unset |
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
| `MINDROOM_MATRIX_HOMESERVER_STARTUP_TIMEOUT_SECONDS` | Seconds to wait at startup for the homeserver to respond (`0` = wait indefinitely); a certificate verification failure stops startup immediately (see [Matrix TLS trust](../architecture/matrix.md#tls-trust)) | _(wait indefinitely)_ |
| `MINDROOM_MATRIX_SYNC_STARTUP_TIMEOUT_SECONDS` | Positive seconds allowed for the first Matrix sync response | `600` |
| `MINDROOM_MATRIX_INGESTION_GRACE_SECONDS` | Positive seconds the sync watchdog and `/api/health` keep waiting while event catch-up is still making progress; set it above the deployment's observed healthy catch-up time | `600` |

Desktop setup variables are listed in [Desktop](../tools/desktop.md).

### Model Providers

Provider API keys such as `ANTHROPIC_API_KEY` and `OPENAI_API_KEY`, their `_FILE` variants, and Vertex AI and Bedrock settings are listed in [Models — Environment Variables](models.md#environment-variables), and `codex login` and `CODEX_HOME` in [Codex Models with ChatGPT Login](models.md#codex-models-with-chatgpt-login).

| Variable | Description |
|----------|-------------|
| `OPENAI_BASE_URL` | Base URL for `provider: openai` models, such as a local inference server; the process environment wins over `.env`, a model's `extra_kwargs.base_url` overrides it, and `extra_kwargs.client_params.base_url` overrides both |

**Automatic credential import**: When MindRoom starts or `mindroom doctor` runs, it copies these variables from the process environment or the config-adjacent `.env` into the shared credentials store: `ANTHROPIC_API_KEY`, `AZURE_OPENAI_API_KEY`, `OPENAI_API_KEY`, `GOOGLE_API_KEY`, `OPENROUTER_API_KEY`, `DEEPSEEK_API_KEY`, `CEREBRAS_API_KEY`, `GROQ_API_KEY`, `ZAI_API_KEY`, `OLLAMA_HOST`, `GITHUB_TOKEN` (stored as `github_private`), `EMBEDDER_API_KEY` (stored as `embedder`), and `GOOGLE_APPLICATION_CREDENTIALS` (stored as `google_vertex_adc`).
Every one of these except `GOOGLE_APPLICATION_CREDENTIALS` also accepts a `_FILE` variant, read only when the plain variable is unset or empty.
Services declared through [Credential Seeds](#credential-seeds) are imported the same way.
Environment import never overwrites credentials saved through the dashboard or credentials with no recorded source.
When a stored value is first imported or changes, MindRoom logs a notice naming the service, the supplying variable, and whether it came from the process environment or `.env`; values are never logged.
To stop an import, remove the variable and any `_FILE` variant from both the process environment and `.env`, then delete the stored credential in the dashboard or with `DELETE /api/credentials/{service}`.

### Dashboard and API

| Variable | Description | Default |
|----------|-------------|---------|
| `MINDROOM_API_KEY` | API key for dashboard/API requests; `mindroom config init` and first-run `mindroom run` add a generated key to `.env` when it has none; unset or empty means open access | _(none)_ |
| `MINDROOM_PORT` | Port used by Google OAuth callback URL construction and deployment tooling; the API server bind port is set with `mindroom run --api-port` instead | `8765` |
| `MINDROOM_DASHBOARD_ALLOWED_HOSTS` | Comma-separated extra host names that unauthenticated dashboard or `/v1` requests may address (see [Authorization](../authorization.md#dashboard-configuration)) | _(none)_ |
| `MINDROOM_DASHBOARD_CORS_ALLOWED_ORIGINS` | Comma-separated origins allowed credentialed dashboard CORS responses; cookie and trusted-upstream mutations still require the app's own origin | `http://localhost:3003`, `http://localhost:5173`, `http://127.0.0.1:3003`, `http://127.0.0.1:5173` |
| `MINDROOM_DASHBOARD_CORS_ALLOW_ALL_ORIGINS` | `true` allows every dashboard API origin while disabling credentialed CORS responses; without dashboard authentication, origins outside `MINDROOM_DASHBOARD_ALLOWED_HOSTS` are still refused | _(unset)_ |
| `OPENAI_COMPAT_API_KEYS` | Comma-separated API keys for `/v1/*` requests (see [OpenAI-Compatible API](../openai-api.md)) | _(none; `/v1` is locked without this or the flag below)_ |
| `OPENAI_COMPAT_ALLOW_UNAUTHENTICATED` | `true` allows unauthenticated `/v1/*` access for local development only | _(unset)_ |
| `MINDROOM_AUTO_BUILD_FRONTEND` | Set to `0` to skip the automatic dashboard frontend build | _(enabled)_ |

Hosted pairing variables written by `mindroom connect` are described in [Hosted Matrix](../deployment/hosted-matrix.md).

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

## Credential Seeds

MindRoom can create shared credential services at startup from explicit seed declarations, for deployment-managed credentials.
Set `MINDROOM_CREDENTIAL_SEEDS_FILE` to a JSON file path (relative paths resolve from the config directory), or `MINDROOM_CREDENTIAL_SEEDS_JSON` to equivalent inline JSON.
Credential fields can read from env vars, from files, or from literal values:

```json
[
  {
    "service": "example_oauth_client",
    "credentials": {
      "client_id": {"env": "EXAMPLE_CLIENT_ID"},
      "client_secret": {"env": "EXAMPLE_CLIENT_SECRET"}
    }
  }
]
```

- If an env ref such as `EXAMPLE_CLIENT_SECRET` is unset, MindRoom also checks `EXAMPLE_CLIENT_SECRET_FILE` and reads that file.
- If any declared field is missing or empty, MindRoom skips that seed instead of creating a partial credential.
- Env and file refs always produce strings, so a tool's boolean field accepts the exact strings `true` and `false` as well as JSON booleans, and any other value makes that tool fail to load.
- Seeds are reapplied on later startups but never replace credentials saved through the dashboard or with no recorded source.
- OAuth token services whose names end with `_oauth` cannot be seeded; connect them through OAuth instead.
  OAuth client configuration services whose names end with `_oauth_client` can be seeded.
- To stop seeding one service, remove its entry from each declaration that lists it and delete the stored credential with `DELETE /api/credentials/{service}`.

## Credential Storage Encryption

Set `MINDROOM_CREDENTIALS_ENCRYPTION_KEY` to encrypt stored credential files at rest.
The key must be a base64-encoded 32-byte value and must stay stable across restarts.
Generate one with:

```bash
python - <<'PY'
import base64
import os

print(base64.urlsafe_b64encode(os.urandom(32)).decode("ascii"))
PY
```

Existing plaintext credential files become unreadable and cannot be overwritten once the key is set, so back them up and recreate them under encryption, or remove the key to read them again.
OAuth connections made before the key was set must likewise be reconnected.
Encrypted credentials become readable again when their correct key is restored.
Dedicated Docker and Kubernetes workers never receive the key; see [credential leases](../deployment/sandbox-proxy.md#credential-leases) for how tool settings reach them.

## Event Journal

`event_journal.backend` defaults to `sqlite`, which stores the Matrix event journal at `<storage>/tracking/event_journal.db` with no separate path setting.
To use PostgreSQL, install the `postgres` extra (for example `uvx --from 'mindroom[postgres]' mindroom run`, or `--extra postgres` when syncing a source checkout), then select the backend and provide a connection URL:

```yaml
event_journal:
  backend: postgres
  database_url_env: MINDROOM_EVENT_CACHE_DATABASE_URL
```

| Field | Type | Default | Meaning |
| --- | --- | --- | --- |
| `backend` | `sqlite` or `postgres` | `sqlite` | Journal storage backend; a URL alone does not switch to PostgreSQL |
| `database_url_env` | string | `MINDROOM_EVENT_CACHE_DATABASE_URL` | Variable holding the PostgreSQL URL, read from the process environment or the config-adjacent `.env` (process environment wins); custom names must be `DATABASE_URL` or end in `_DATABASE_URL` |
| `database_url` | string or `null` | `null` | Inline PostgreSQL URL that takes precedence over the variable |

Changes to the event journal apply after restarting MindRoom.
See the [journal binding and migration commands](../cli.md#journal) before moving, restoring, or adopting a journal.

## Matrix Sync

```yaml
matrix_sync:
  mode: classic
  sliding_timeline_limit: 100
  max_response_bytes: 16777216
  max_pending_bytes: 67108864
```

| Field | Type | Default | Meaning |
| --- | --- | --- | --- |
| `mode` | `classic` or `sliding` | `classic` | `classic` uses `/v3/sync`; `sliding` uses MSC4186 Simplified Sliding Sync and requires a homeserver that advertises it |
| `sliding_timeline_limit` | integer ≥ 1 | `100` | Per-room timeline window for Sliding Sync |
| `max_response_bytes` | integer ≥ 1 | `16777216` (16 MiB) | Maximum HTTP sync response size per bot |
| `max_pending_bytes` | integer ≥ 1 | `67108864` (64 MiB) | Maximum encoded pending output per bot |

A durable store stays bound to the `matrix_sync.mode` it first used.
If a large initial sync exceeds the response bound, increase `max_response_bytes` enough to fit the response.
If preparing events exceeds the pending bound, increase `max_pending_bytes`; encoded events can take more space than the HTTP response, so raising the response limit may also require raising the pending limit.
Each bot has its own sync session, so allow sufficient memory and disk space when increasing these limits.
Changing `matrix_sync` through config reload restarts running agents.
Large initial syncs may also need more startup time: `MINDROOM_MATRIX_SYNC_STARTUP_TIMEOUT_SECONDS` sets the first-sync allowance, and the runtime Helm chart's `probes.startup` values set the Kubernetes startup probe allowance.

## Automatic Restart Resumption

`defaults.auto_resume_after_restart` (boolean, default `true`) makes the router post resume prompts after a restart in eligible threads whose conversations were interrupted.
Set it to `false` to suppress those prompts and resume the work manually.
Work that was superseded by later messages, or whose requester or room membership no longer applies, is not resumed.

## Debug Logging

Every completed model request logs an `LLM usage` event with provider and model identifiers and, when the provider reports usage, input tokens, uncached input tokens, and a cache-read ratio, without prompt content.

Set `debug.log_llm_requests: true` to log provider requests as JSONL under `debug.llm_request_log_dir` (default `mindroom_data/logs/llm_requests`).
Those records include prompts, messages, the tools sent to the provider, model parameters, and requester and source Matrix event metadata.
The same flag records successful tool calls with timing in `mindroom_data/tracking/tool_calls.jsonl`; tool failures are always recorded there.
Credential-bearing fields such as tokens, cookies, passwords, API keys, and authorization headers are redacted, but these files can still contain sensitive prompt, argument, and result data, so leave the flag disabled unless you are actively debugging.

Export `MINDROOM_TIMING=1` before startup to log timing for tool calls and one INFO-level `Dispatch pipeline timing` summary per turn, including `time_to_model_request_ms` and context, queue, payload, agent-build, and model spans.

```bash
LOG_LEVEL=DEBUG MINDROOM_LOG_FORMAT=json MINDROOM_TIMING=1 mindroom run
```

To find what grows the primary process's memory, set `MINDROOM_HEAP_PROBE_INTERVAL_SECONDS` in the process environment or the config-adjacent `.env`.
The primary then logs one `heap_type_probe` event per interval with the 25 most common object types and their counts, resident memory (`rss_bytes`), allocator totals under `malloc` (`arena_bytes`, `arena_in_use_bytes`, `arena_free_bytes`, `mmap_bytes`), and `python_allocated_blocks`.
Resident memory that grows while `arena_in_use_bytes` plus `mmap_bytes` and `python_allocated_blocks` stay flat suggests freed memory the allocator keeps, and growth in those suggests live objects, which the type histogram may not show because it counts only garbage-collected container objects, not strings, bytes, or numbers.
Each probe briefly pauses the event loop, so prefer intervals of several minutes on large processes.
Unset or `0` disables the probe (the default), and any other value below `60` fails startup.

See [Operational Log Events](../deployment/operational-log-events.md) for stable log events to alert on.

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

## Managed Avatars

Every agent, team, and managed room gets a Matrix avatar by default, chosen from the painted stock avatars in the [MindRoom assets repository](https://github.com/mindroom-ai/assets/blob/main/avatars/painted/README.md).

- An agent named after a stock avatar (for example `mind`, `code`, `research`, `writer`, or `email`) uses that picture, and any other agent or team receives a stable stock avatar chosen from its name.
- A room served by exactly one agent or team shows that entity's avatar, and any other room receives a stable stock avatar chosen from its key.
- The root Matrix Space uses `avatars/spaces/root_space.png` when present and otherwise the stock `mind-logo` image.
- Avatars are filled in only where the Matrix profile or room has none, so pictures you set yourself are kept.
- Managed room avatars are chosen only when a room is created, so run `mindroom avatars sync` to fill in existing rooms.
- A failed stock avatar download is retried at the first start after 24 hours; `mindroom avatars sync` retries room and root Space avatars immediately.

To choose a picture, place a PNG at `avatars/<agents|teams|rooms|spaces>/<name>.png` next to `config.yaml`; containerized deployments read these overrides from `<storage>/avatars/` instead.
Run `mindroom avatars sync --force` to reset every managed room and the root Space to its default avatar: its own file, its single agent's or team's avatar, or its stock pick.

`mindroom avatars generate` creates a custom avatar file for every entity without a workspace or bundled avatar file, using `gpt-6-astra` for prompt creation and `gpt-image-2.5-sunburst` for rendering; it requires only `OPENAI_API_KEY` or `OPENAI_API_KEY_FILE`.
Run `mindroom avatars generate --force` to overwrite existing workspace avatar files after changing prompts or styles.
Override the generation styles with the root `prompts` block:

```yaml
prompts:
  AVATAR_CHARACTER_STYLE: "professional AI avatar portrait, abstract geometric silhouette"
  AVATAR_ROOM_STYLE: "minimalist wayfinding icon, precise geometry, strong silhouette"
  AVATAR_AGENT_SYSTEM_PROMPT: "You are creating distinctive visual elements for a professional AI agent avatar."
  AVATAR_TEAM_SYSTEM_PROMPT: "You are creating distinctive visual elements for a professional AI team avatar."
  AVATAR_ROOM_SYSTEM_PROMPT: "You are creating a refined, minimalist icon design for a room avatar."
```

## Personal Agent Rooms

The optional `personal_rooms` section creates one private, unlisted room for each eligible human who joins an onboarding room.
The router watches the onboarding rooms; the selected agent does not need to join them.

```yaml
rooms:
  lobby: {}
agents:
  helper:
    display_name: Helper
    rooms: []
    access:
      members_of_rooms: [lobby]
personal_rooms:
  agent: helper
  onboarding_rooms: [lobby]
  commands: ["!personal"]
  welcome_dispatch: true
```

`personal_rooms` is an object or `null` (the default, which disables onboarding).
When enabled, its fields are:

| Field | Type | Default | Meaning |
| --- | --- | --- | --- |
| `agent` | string | Required | Configured agent that owns and answers in personal rooms. |
| `onboarding_rooms` | list of strings | Required | Nonempty list of configured onboarding rooms observed by the router. |
| `commands` | list of strings | `[]` | Exact self-onboarding commands beginning with `!`, without whitespace or arguments. |
| `alias_prefix` | string | `"personal"` | Lowercase alias prefix, matching `[a-z0-9_-]{1,40}`. |
| `name` | string | `"Personal room for {user}"` | New room name template, up to 1,000 characters. |
| `topic` | string | `"Private conversation with {agent}."` | New room topic template, up to 1,000 characters. |
| `welcome` | string | `"Welcome {user}! This is your personal room with {agent}."` | Welcome template, up to 10,000 characters; empty disables new welcomes. |
| `welcome_dispatch` | boolean | `false` | Send the welcome to the selected agent as a trusted message on the human's behalf after the human joins, so the agent starts the conversation; otherwise send it as a notice. |
| `confirmation` | string | `""` | Optional once-only notice in the original onboarding room, visible to everyone there, up to 10,000 characters. |
| `backfill` | boolean | `false` | Also onboard current eligible onboarding-room members on startup and config reload. |
| `auto_join_requester` | boolean | `false` | Use the homeserver admin API to join the requester into a newly created personal room. |
| `requester_admin` | boolean | `false` | Grant the human Matrix room administration; otherwise the agent owns room state. |
| `avatar` | string or `null` | `null` | Room avatar file, relative to the configuration; takes priority over `avatar_from_requester`. |
| `avatar_from_requester` | boolean | `false` | Copy the human's profile avatar into the room when no avatar file is configured. |

Templates support `{user}` (full Matrix user ID), `{room}` (room alias), and `{agent}` (display name).
Aliases are built from `alias_prefix`, a hash of the user ID, and the installation namespace.
Room avatars are set only when the room has none.

### Onboarding Behavior

- A human joining an onboarding room, or sending one of the `commands` there, onboards only that human.
- Existing agent access rules still apply, and agent and service accounts are excluded.
- `auto_join_requester: true` requires a source Matrix account allowed to use the homeserver's Synapse-compatible admin join API.
  It never force-joins someone who already joined, left, or was banned, and does not apply to existing or imported rooms.
- A requester who left or was banned from their personal room is not re-added by restarts or backfill; a fresh join of the onboarding room may re-invite them to a newly created personal room, but never overrides a ban.
- The owner may invite other MindRoom agents into their personal room, and an invited agent sees only messages sent after its invite.
  Anyone else invited into a room MindRoom created is removed with a notice explaining why.
- The welcome is delivered once, and its text and dispatch mode are fixed when the room is created, so later template changes do not affect pending welcomes.
  Disabling `personal_rooms` or revoking the human's access prevents a pending welcome.
- Personal rooms are kept across restarts and room cleanup, including after the feature is disabled, and disabling onboarding does not revoke existing access.

### Troubleshooting Personal Rooms

Onboarding failures affect only that requester, and the onboarding room keeps serving everyone else.
Failed rooms are retried automatically after delays that start at 30 seconds and double up to one hour, and a configuration reload retries them at once.
A join or command that arrives before the personal-room agent connects waits until it does.

| Log message | Meaning |
| --- | --- |
| `Personal-room validation failed for an onboarding trigger` | An existing personal room failed ownership or membership checks, for example because another account already holds the alias, or a guest the agent cannot remove was raised to the agent's power level. |
| `Personal-room imported roster has unattested members` | An imported room contains members that its seed record does not list; the warning names them, and the room is not changed. |
| `Personal-room onboarding trigger failed` | Any other failure, such as an invite refused by the requester's server. |

### Operator-seeded Existing Rooms

Operators can adopt an existing room, keeping its room ID and history, by writing a trusted `PersonalRoomRecord` before enabling onboarding.

1. Stop the runtime.
2. In the room, have the agent author an `org.mindroom.personal_room` state event with an empty state key and exactly `{"user_id": "<requester>", "agent_user_id": "<agent>"}` as content.
3. Make sure the target agent is joined with room admin power, the room's directory visibility is private, and its join rule is invite-only.
4. Write the record with `personal_room_record_path(runtime_paths, agent_name, user_id)` and `write_personal_room(path, record)` from `mindroom.matrix.personal_room_store`.

For a private room with shared history and one already permitted guest, a record looks like this:

```json
{
  "user_id": "@alice:example.test",
  "alias": "#personal-alice:example.test",
  "source_room_id": "!lobby:example.test",
  "room_id": "!existing:example.test",
  "welcome_completed": true,
  "adoption": {
    "creator_user_id": "@router:example.test",
    "agent_user_id": "@helper:example.test",
    "router_user_id": "@router:example.test",
    "expected_history_visibility": "shared",
    "additional_user_ids": ["@guest:example.test"]
  }
}
```

- Adoption requires the exact room ID; an alias alone never authorizes adoption.
- `creator_user_id` must match the room's creator, and `agent_user_id` must match the selected agent's Matrix identity.
- `router_user_id` (optional) lets this installation's router account stay in the room, including during ordinary cleanup after onboarding is disabled.
- `expected_history_visibility` must exactly match the room's current `invited`, `joined`, or `shared` history setting and defaults to `invited`; `world_readable` is never accepted.
- `additional_user_ids` (default `[]`) lists existing participants who may be joined, invited, or knocking.
  It only permits them; it does not invite anyone or grant power.
  No other members besides the requester, agent, seeded router, and these users are allowed.
- Set `welcome_completed: true` only when onboarding is already complete, and omit pending welcome content from completed imports.

Normal authorization and current onboarding-room membership still apply.
Reconciliation fails if the room's history visibility or membership no longer matches the record, and MindRoom never rewrites the room's name, topic, or history.

## Internal User

The optional `mindroom_user` section configures MindRoom's internal Matrix user account; omit it for hosted or public profiles.

```yaml
mindroom_user:
  username: mindroom_user
  display_name: MindRoomUser
```

| Field | Type | Default | Meaning |
| --- | --- | --- | --- |
| `username` | string | `mindroom_user` | Matrix localpart to request, without `@` or a domain; set it before first startup, because it cannot be changed in place after the account exists |
| `display_name` | string | `MindRoomUser` | Display name, changeable at any time |

Hosted provisioning can return a different actual Matrix ID, which MindRoom then uses.
