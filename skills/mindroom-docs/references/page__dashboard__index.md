# Web Dashboard

The web dashboard configures agents, teams, rooms, models, credentials, and integrations without editing YAML by hand.
Each editor keeps a local draft and writes `config.yaml` when you save.
This page covers opening the dashboard, what each tab does, saving and conflict errors, and the dashboard REST API.

## Accessing the Dashboard

**Standalone:** run `mindroom run` and open `http://localhost:8765`.
When running from a source checkout, MindRoom builds the dashboard assets on first start if Bun is available.
When `MINDROOM_API_KEY` is set, the dashboard first asks for that key, and the login page shows which `.env` file holds it.
See [Authorization](https://docs.mindroom.chat/authorization/#dashboard-configuration) for who can open the dashboard and [Environment Variables](https://docs.mindroom.chat/configuration/#dashboard-and-api) for the related settings.

**SaaS platform:** open `https://<instance-id>.mindroom.chat`.

## Connect Your AI Provider

When the router, an agent, or a team uses a model whose provider has no API key, every dashboard page shows a **Connect your AI provider** banner.
Keys in `.env`, keys saved per model on the **Models** tab, and provider keys saved under their environment variable name (for example `OPENROUTER_API_KEY`) all count as set.
Select **Connect provider**, choose OpenRouter, Anthropic, or OpenAI, and paste a key.
MindRoom checks the key with the provider without using credits and saves it as the `openrouter`, `anthropic`, or `openai` credential only if the provider accepts it.
OpenRouter is recommended for the hosted default setup, where one key covers chat, memory, and voice.
If you connect Anthropic or OpenAI while your models still use OpenRouter, the dialog lists the affected models so you can switch them on the **Models** tab.
When an agent cannot reply because no provider key is set, it answers in chat with a pointer to this setup step, including the dashboard address when `MINDROOM_PUBLIC_URL` is set.

## Dashboard Tabs

### Dashboard (Home)

The home tab shows today's activity, upcoming schedules, and shortcuts to room and agent editors.
Select **Browse workspace** to search and filter agents, rooms, and teams, inspect an item and open its editor, or **Export summary**.
The exported JSON summary lists counts, agents, rooms, teams, and model configurations; it is not a complete `config.yaml` backup.

### Agents

- **Display name**, **Role description**, **Model**, **Instructions**, and **Rooms**
- **Memory backend** - inherit the global backend or override it per agent (`mem0`, `file`, or `none`)
- **Tools** - configured tools (green badge) and tools that need no setup
- **Learning** and **Learning mode** (`always` or `agentic`); see [Agent Configuration](https://docs.mindroom.chat/configuration/agents/)
- **Lazy tool loading** - open a checked tool's settings and enable **Load lazily** (`defer`) or **Load at session start** (`initial`, which requires `defer`); presets and control-plane tools such as `dynamic_tools` do not offer it (see [Dynamic Tools](https://docs.mindroom.chat/tools/dynamic-tools/))
- **More settings** - every other agent field, such as `participation`, `mid_turn`, `accept_invites`, `access`, `memory_search`, `thread_exports`, and `room_thread_modes`

### Teams

- **Display name** and **Team purpose**
- **Collaboration mode** - Coordinate or Collaborate (see [Teams](https://docs.mindroom.chat/configuration/teams/))
- **Team model** - optional model override
- **Team members** and **Team rooms**

### Rooms

- **Room Admins** - the panel at the top of the tab edits `room_defaults.admins`, the default Matrix room-admin list; add or remove user IDs and click **Save**
- **Display name** and **Description**
- **Room model** - optional model override
- **Agents in room**
- **More settings** - per-room `join_policy`, `listed`, `encrypted`, `invite_users`, and `admins` overrides (see [Rooms](https://docs.mindroom.chat/rooms/))

### Schedules

Lists scheduled tasks across rooms with room, status, schedule type, delivery mode, and next run time.
You can edit a task's timing, description, and visible or silent delivery, or cancel it.
See [Scheduling](https://docs.mindroom.chat/scheduling/).

### External Rooms

The **External** tab lists, per agent, rooms the agent has joined that are not in the configuration.
Select rooms and use **Leave rooms** to remove the agents, or open a room in your Matrix client.

### Models

Add and edit models with provider, model ID, host URL, and advanced settings, and set provider API keys.
A row's **Save** only updates the draft; click **Save All Changes** to write model changes to `config.yaml`.
The add-model dropdown does not have a preset for every supported provider, but the dashboard keeps any provider already in the configuration.
A model's **More settings** section appears below the table while its row is being edited, and **Cancel** on the row also reverts More settings changes made since the last save.
See [Models](https://docs.mindroom.chat/configuration/models/) for providers and fields.

### Usage

See [Usage Tracking](https://docs.mindroom.chat/usage/#dashboard-usage-tab).

### Memory

Sets the global memory backend, embedder, file backend, and auto-flush settings; per-agent backend overrides are on the **Agents** tab.
See [Memory](https://docs.mindroom.chat/memory/#ui-configuration) for the fields.

### Knowledge

- Create, edit, and delete knowledge bases, including `description`, `mode` (semantic search or files-only), `path`, `watch`, and Git repository, branch, filtering, credentials service, and sync options
- Upload and remove files for non-Git bases
- Reindex or sync a base on demand
- See index status (`file_count` and `indexed_count`)

Git-backed bases hide upload and delete controls; change files in the repository, then sync.
The file list shows only files that pass the base's `include_patterns` and `exclude_patterns`.
See [Knowledge Bases](https://docs.mindroom.chat/knowledge/) for configuration and sync behavior; assign bases to agents on the **Agents** tab.

### Credentials

- List, create, edit (as raw JSON), test, and delete credential services, such as `github_private` or `model:sonnet`
- Reference a stored service from config, for example `knowledge_bases.<id>.git.credentials_service`

A new service named after a provider key environment variable, such as `ANTHROPIC_API_KEY`, is saved as the canonical provider service (`anthropic`) that the Models tab and runtime read.
An existing service with an environment-variable name keeps receiving writes, because config may reference it by that exact name.
See [Credential Storage](https://docs.mindroom.chat/oauth-framework/#credential-storage) for credentials imported from `.env`, such as `GITHUB_TOKEN`.

### Voice

Enables voice messages and configures speech-to-text (OpenAI or a self-hosted OpenAI-compatible service), the transcript-cleanup model, and [voice calls](https://docs.mindroom.chat/voice-calls/#calls-configuration) with named call profiles and per-agent assignments.
See [Voice Messages](https://docs.mindroom.chat/voice/).

### Tools

The **Tools** tab connects external services that tools need, including OAuth flows such as Google, Spotify, and Home Assistant.
Search tools, filter by category or status (**Available**, **Unconfigured**, **Configured**), and choose **Shared deployment credentials** or an agent with a worker scope to configure that scope (see [Dashboard credentials and worker scopes](https://docs.mindroom.chat/deployment/sandbox-proxy/#dashboard-credentials-and-worker-scopes)).

### Skills

Lists installed skills with origin and edit status, shows `SKILL.md` content, and creates, edits, and deletes user skills.
See [Skills](https://docs.mindroom.chat/skills/#installing-and-managing-skills).

### Settings

The **Settings** tab edits every top-level configuration section without a dedicated tab, grouped into Responses, History and context, Tools and workers, Router, Personal rooms, Room defaults, Tool approval, Prompts, MCP servers, Plugins, Access and identity, Matrix and runtime, and Diagnostics.
Any option no tab claims appears under **Other**.
**Tools and workers** also edits per-tool overrides for the default tools every agent inherits.

## Editing and Saving

### More Settings and Schema Forms

The Agents, Teams, Rooms, Memory, Knowledge, Voice, and Models tabs show a collapsible **More settings** section with the fields their main editor does not cover.
These sections and the Settings tab are generated from the running backend's configuration schema.
Each field shows its description and default, references such as model or agent names are pickers, and credential fields are masked.
Resetting a field removes it from `config.yaml` so the default applies again, and optional blocks are added or removed with their **Configure** checkbox.
Validation errors from a save appear beside the affected field, and Settings sections with errors are marked **Needs attention**.

### Save Status

The header shows **Synced**, **Syncing...**, **Sync Error**, or **Disconnected** (lost connection to the backend).
The dashboard has dark and light themes and works on mobile.

### Save Conflicts

If another dashboard tab or a server-side edit changed the configuration, saving shows `Configuration changed elsewhere. Your draft has not been saved. Copy any changes you want to keep, then refresh this page and reapply them.`
Saving stays blocked until you refresh, even if you edit the draft.

### Invalid Configuration

If `config.yaml` fails validation when the dashboard loads, it opens a raw YAML recovery editor so you can fix and save the whole file.

### Configurations Split With `!include`

When the configuration uses [include tags](https://docs.mindroom.chat/configuration/#splitting-the-configuration-into-multiple-files), the dashboard shows a banner and structured saves fail, so edit the included source files directly.
The raw recovery editor still edits the top-level file's text.
Through the API, structured saves return HTTP 409 with `{"code": "config_composed_from_includes", "message": "configuration uses !include; edit the source files instead"}`, unlike the plain-text 409 for a stale write, which can be retried.
If the source files cannot be read well enough to tell whether they use `!include`, structured saves return HTTP 422; repair the files or edit the raw YAML.
`POST /api/config/load` reports include use in the `x-mindroom-config-uses-includes` response header, and `GET /api/config/raw` in its `uses_includes` field.

## API Endpoints

The dashboard uses the backend REST API under `/api/`, with the same authentication as the dashboard.

### Configuration API

| Method | Endpoint | Description |
|--------|----------|-------------|
| POST | `/api/config/load` | Fetch current configuration |
| PUT | `/api/config/save` | Save full configuration |
| GET | `/api/config/raw` | Fetch raw `config.yaml` text |
| PUT | `/api/config/raw` | Replace raw `config.yaml` text |
| GET | `/api/config/schema` | Configuration JSON schema used by Settings and More settings |
| GET | `/api/config/agents` | List agents |
| POST | `/api/config/agents` | Create agent |
| PUT | `/api/config/agents/{id}` | Update agent |
| DELETE | `/api/config/agents/{id}` | Delete agent |
| GET | `/api/config/teams` | List teams |
| POST | `/api/config/teams` | Create team |
| PUT | `/api/config/teams/{id}` | Update team |
| DELETE | `/api/config/teams/{id}` | Delete team |
| GET | `/api/config/models` | List model configurations |
| PUT | `/api/config/models/{id}` | Update model configuration |
| GET | `/api/config/room-models` | Get room model overrides |
| PUT | `/api/config/room-models` | Update room model overrides |
| POST | `/api/config/agent-policies` | Derived agent policies for a draft config |

### Credentials API

| Method | Endpoint | Description |
|--------|----------|-------------|
| GET | `/api/credentials/list` | List services with credentials |
| GET | `/api/credentials/{service}/status` | Get credential status |
| GET | `/api/credentials/{service}` | Get credentials for editing |
| POST | `/api/credentials/{service}` | Set credentials |
| POST | `/api/credentials/{service}/api-key` | Set API key |
| GET | `/api/credentials/{service}/api-key` | Get masked API key |
| POST | `/api/credentials/{service}/test` | Check stored credentials exist |
| DELETE | `/api/credentials/{service}` | Delete credentials |
| POST | `/api/credentials/{service}/copy-from/{source_service}` | Copy credentials from another service |

Add `agent_name` to target an agent's credentials in its saved worker scope.
The optional `execution_scope` requires `agent_name` and must match that agent's saved scope; save the configuration first after changing the scope.
Agent-scoped requests return HTTP 403 unless the requester may manage that agent's credentials; see [Platform and credential authority](https://docs.mindroom.chat/authorization/#platform-and-credential-authority).

### Knowledge API

| Method | Endpoint | Description |
|--------|----------|-------------|
| GET | `/api/knowledge/bases` | List knowledge bases |
| GET | `/api/knowledge/bases/{base_id}/files` | List files in a base |
| POST | `/api/knowledge/bases/{base_id}/upload` | Upload files to a non-Git base |
| DELETE | `/api/knowledge/bases/{base_id}/files/{path}` | Delete a file from a non-Git base |
| GET | `/api/knowledge/bases/{base_id}/status` | Get indexing status |
| POST | `/api/knowledge/bases/{base_id}/reindex` | Reindex a base, or sync a Git-backed base |

Upload and delete return HTTP 409 for Git-backed bases (see [Sync Behavior](https://docs.mindroom.chat/knowledge/#sync-behavior)).

### Skills API

| Method | Endpoint | Description |
|--------|----------|-------------|
| GET | `/api/skills` | List installed skills |
| GET | `/api/skills/{skill_name}` | Get skill content, origin, and edit status |
| POST | `/api/skills` | Create a user skill |
| PUT | `/api/skills/{skill_name}` | Update a user skill |
| DELETE | `/api/skills/{skill_name}` | Delete a user skill |

### Schedules API

| Method | Endpoint | Description |
|--------|----------|-------------|
| GET | `/api/schedules` | List scheduled tasks, optionally filtered by room, including each task's `silent` flag |
| PUT | `/api/schedules/{task_id}` | Edit a scheduled task, including `silent` |
| DELETE | `/api/schedules/{task_id}` | Cancel a scheduled task |

### Workers API

| Method | Endpoint | Description |
|--------|----------|-------------|
| GET | `/api/workers` | List sandbox workers |
| POST | `/api/workers/cleanup` | Clean up idle sandbox workers |

### Tools and Matrix API

| Method | Endpoint | Description |
|--------|----------|-------------|
| GET | `/api/tools` | List available tools |
| GET | `/api/rooms` | List configured rooms |
| GET | `/api/matrix/agents/rooms` | All agents' room memberships |
| GET | `/api/matrix/agents/{id}/rooms` | One agent's rooms |
| POST | `/api/matrix/rooms/leave` | Leave one room |
| POST | `/api/matrix/rooms/leave-bulk` | Leave multiple rooms |

For usage reports and health endpoints, see [Usage Report API](https://docs.mindroom.chat/usage/#usage-report-api) and [Health & Readiness](https://docs.mindroom.chat/deployment/operational-log-events/#health-readiness).
