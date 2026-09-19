# Web Dashboard

MindRoom includes a web dashboard for configuring agents, teams, rooms, and integrations without editing YAML files.
Editors keep local draft state and write `config.yaml` when you use their save action.

## Accessing the Dashboard

**Standalone Mode:**

```bash
mindroom run
```

The dashboard will be available at `http://localhost:8765`.
When running from a source checkout, MindRoom will build the dashboard assets on first start if Bun is available.

**SaaS Platform:** Access your dashboard at `https://<instance-id>.mindroom.chat`

## Dashboard Tabs

### Dashboard (Overview)

The main dashboard shows system stats and monitoring:

- **Stats cards** - Agents (with status breakdown), rooms, teams, models, and voice status
- **Network graph** - Visual representation of agent-room-team relationships (desktop only)
- **Search and filter** - Filter by agents, rooms, or teams
- **Export Config** - Download configuration as JSON

### Agents

Configure AI agents:

- **Display name** and **Role description**
- **Model** - Select from configured models
- **Memory backend** - Inherit global memory backend or override per agent (`mem0`, `file`, or `none`)
- **Tools** - Organized into configured tools (green badge) and default tools (no config needed)
- **Instructions** - Custom behavior instructions
- **Rooms** - Where the agent operates
- **Learning** - Enable or disable Agno Learning per agent (enabled by default)
- **Learning mode** - Choose `always` (automatic extraction) or `agentic` (tool-driven)

### Teams

Configure multi-agent collaboration:

- **Display name** and **Team purpose**
- **Collaboration mode** - Coordinate (sequential) or Collaborate (parallel)
- **Team model** - Optional model override
- **Team members** and **Team rooms**

### Rooms

Manage Matrix room configuration:

- **Display name** and **Description**
- **Room model** - Optional model override
- **Agents in room** - Select which agents have access

### External Rooms

View and manage rooms that agents have joined but are not in the configuration:

- **Per-agent view** with room names and IDs
- **Bulk selection** and **Leave rooms** functionality
- **Open in Matrix** - Link to view in your Matrix client

### Models & API Keys

Configure AI model providers:

- **Add/edit models** with provider, model ID, host URL, and advanced settings
- **Provider API keys** section for configuring credentials

**Runtime-supported providers:** Anthropic, Bedrock Claude, Azure OpenAI, OpenAI, Codex CLI ChatGPT authentication, Kimi Code, Google Gemini, Vertex AI Claude, Ollama, llama.cpp, Groq, OpenRouter, Cerebras, DeepSeek, Z.ai, and the internal synthetic provider.
The dashboard preserves provider IDs already present in the configuration; not every runtime provider has a dedicated icon or preset in the add-model dropdown.

### Memory

Configure global memory defaults:

- **Backend** - Global default backend (`mem0`, `file`, or `none`)
- **Provider** - Ollama (local), OpenAI, or Sentence Transformers
- **Model** - Provider-specific embedding models
- **Credential service** - Strictly bind an OpenAI-compatible embedder to a dedicated stored key
- **Host URL** - For Ollama provider
- **File backend settings** - Path and file memory tuning options
- **Auto-flush settings** - Background extraction and flush controls for file-backed memory

Per-agent overrides are configured from the **Agents** tab using the **Memory backend** selector.

### Knowledge

Manage file-backed semantic or files-only knowledge bases:

- **Create/edit/delete knowledge bases** with `description`, `mode`, `path`, and refresh-on-access `watch` settings
- **Choose semantic search or files-only access** depending on whether a base should build embeddings
- **Configure Git repository, branch, filtering, credentials service, and sync options**
- **Upload and remove files** for non-Git-backed knowledge bases
- **Reindex or sync** a knowledge base on demand
- **Track index status** (`file_count` and `indexed_count`)
- **Assign agents** to a specific knowledge base from the Agents tab

Git-backed knowledge bases are managed from the dashboard, but file mutations still belong in the repository.

- The dashboard hides upload, dropzone, and per-file delete controls for Git-backed bases.
- `/api/knowledge/bases/{base_id}/files` reflects the manager's filtered file set (for example `include_patterns`/`exclude_patterns`).
- Private HTTPS repo auth can be managed in the **Credentials** tab, then referenced by `knowledge_bases.<id>.git.credentials_service`.
- `POST /api/knowledge/bases/{base_id}/reindex` syncs Git first for Git-backed bases, then rebuilds semantic indexes or publishes files-mode source metadata.
- `POST /api/knowledge/bases/{base_id}/upload` and `DELETE /api/knowledge/bases/{base_id}/files/{path}` reject Git-backed bases with `409`; update the repository and sync or reindex instead.
- Chat/runtime requests use last successfully published indexes and do not wait for indexing or Git sync.

### Credentials

Manage service credentials directly from the dashboard:

- **List configured credential services** from `CredentialsManager`
- **Create/select service names** (for example `github_private` or `model:sonnet`)
- **Edit raw JSON credential payloads** and save via `/api/credentials/{service}`
- **Test credentials existence** using `/api/credentials/{service}/test`
- **Delete credential sets** using `/api/credentials/{service}`
- **Reuse credentials for Git knowledge sync** by setting `knowledge_bases.<id>.git.credentials_service` to the same service name
- `GITHUB_TOKEN` auto-seeds `github_private` (`username: x-access-token`, `token: <GITHUB_TOKEN>`, `_source: env`) unless the service is UI-managed

### Schedules

View and manage scheduled tasks across rooms:

- **List all schedules** with room, status, schedule type, delivery mode, and next run time
- **Edit schedule timing**, description, and visible or silent delivery
- **Cancel schedules** by task ID

### Skills

Manage OpenClaw-compatible skills:

- **List installed skills** with origin and edit status
- **View skill content** (SKILL.md)
- **Create new skills** with name and description
- **Edit user-created skills**
- **Delete user-created skills**

### Voice

Configure voice message handling:

- **Enable/disable** voice message support
- **Speech-to-Text** - OpenAI transcription or a self-hosted OpenAI-compatible service
- **Transcript intelligence** - Model selection for mention normalization and light ASR cleanup

### Integrations

Connect external services to enable agent capabilities:

- **Categories** - Email & Calendar, Communication, Shopping, Entertainment, Social, Development, Research, Smart Home, Information
- **Search and filter** by status (Available, Unconfigured, Configured)
- **OAuth flows** for Google (6 endpoints), Spotify (4 endpoints), Home Assistant (7 endpoints), and more

## Features

### Save Status

The sync status indicator in the header shows:

- **Synced** - All changes saved
- **Syncing...** - Save in progress
- **Sync Error** - Sync failed
- **Disconnected** - Lost connection to backend

If another dashboard tab or a server-side edit changes the configuration, saving an older draft can return HTTP 409.
The dashboard keeps your draft and shows persistent recovery guidance, including in the raw configuration recovery editor.
Copy any changes you want to keep, refresh the page to load the current configuration, then reapply and save your changes.
Further save attempts are rejected locally while the conflict remains; editing the draft or refreshing agent policies does not resolve it.
The dashboard does not automatically reload and retry a full replacement, because that could overwrite another writer's changes.

### Theme and Responsive Design

Toggle between dark and light themes. The dashboard adapts to desktop and mobile devices.

## API Endpoints

The dashboard communicates with the backend API at `/api/`:

### Configuration

| Method | Endpoint | Description |
|--------|----------|-------------|
| POST | `/api/config/load` | Fetch current configuration |
| PUT | `/api/config/save` | Save full configuration |
| GET | `/api/config/raw` | Fetch the raw `config.yaml` source for recovery editing |
| PUT | `/api/config/raw` | Replace the entire raw `config.yaml` source during recovery |
| GET | `/api/config/agents` | List all agents |
| POST | `/api/config/agents` | Create new agent |
| PUT | `/api/config/agents/{id}` | Update agent |
| DELETE | `/api/config/agents/{id}` | Delete agent |
| GET | `/api/config/teams` | List all teams |
| POST | `/api/config/teams` | Create new team |
| PUT | `/api/config/teams/{id}` | Update team |
| DELETE | `/api/config/teams/{id}` | Delete team |
| GET | `/api/config/models` | List model configurations |
| PUT | `/api/config/models/{id}` | Update model configuration |
| GET | `/api/config/room-models` | Get room model overrides |
| PUT | `/api/config/room-models` | Update room model overrides |
| POST | `/api/config/agent-policies` | Get backend-derived agent policies for a draft config |

When `/api/config/load` returns validation errors, the dashboard fetches `/api/config/raw`, opens the recovery editor, and saves a full replacement through `PUT /api/config/raw` before retrying the structured reload.

When the loaded configuration is composed from multiple files via `!include`, structured save endpoints reject writes with HTTP 409 and the machine-readable error code `config_composed_from_includes`, distinguishing this permanent rejection from a retryable stale-write conflict.
`POST /api/config/load` reports the includes state in the `x-mindroom-config-uses-includes` response header, `GET /api/config/raw` includes a `uses_includes` field in its response body, and the dashboard shows a banner explaining that changes must be made by editing the include source files directly.
The raw recovery editor keeps working on the top-level file's literal text; see the configuration guide's section on splitting the configuration into multiple files for the full semantics.

### Credentials

| Method | Endpoint | Description |
|--------|----------|-------------|
| GET | `/api/credentials/list` | List services with credentials |
| GET | `/api/credentials/{service}/status` | Get credential status |
| GET | `/api/credentials/{service}` | Get credentials for editing |
| POST | `/api/credentials/{service}` | Set credentials |
| POST | `/api/credentials/{service}/api-key` | Set API key |
| GET | `/api/credentials/{service}/api-key` | Get masked API key |
| POST | `/api/credentials/{service}/test` | Test credentials validity |
| DELETE | `/api/credentials/{service}` | Delete credentials |
| POST | `/api/credentials/{service}/copy-from/{source_service}` | Copy credentials from another service |

Credentials support scoping via query parameters:

- `agent_name` — scope credentials to a specific agent
- `execution_scope` — scope credentials to a specific worker scope (e.g., `shared`, `unscoped`)

Agent-scoped credential routes for shared agents require the authenticated dashboard requester to be a platform `administrator` or a concrete user in `agents.<name>.credential_managers`.
Requester-private agents allow authenticated requesters to manage OAuth connections in their own isolated scope.
Deployment-global OAuth client configuration requires platform-administrator authority, with or without `agent_name`.
Responder access and room membership never grant credential-management access.
Unauthorized agent-scoped requests return HTTP 403.
Trusted upstream deployments should provide a Matrix requester identity through the configured Matrix user ID header or email-to-Matrix template.
Standalone deployments should set `MINDROOM_OWNER_USER_ID` so API-key dashboard requests manage credentials as the owner Matrix user.

### Knowledge

| Method | Endpoint | Description |
|--------|----------|-------------|
| GET | `/api/knowledge/bases` | List configured knowledge bases |
| GET | `/api/knowledge/bases/{base_id}/files` | List files in a knowledge base |
| POST | `/api/knowledge/bases/{base_id}/upload` | Upload one or more files for a non-Git-backed base |
| DELETE | `/api/knowledge/bases/{base_id}/files/{path}` | Delete a file from disk for a non-Git-backed base and schedule refresh |
| GET | `/api/knowledge/bases/{base_id}/status` | Get indexing status |
| POST | `/api/knowledge/bases/{base_id}/reindex` | Rebuild the index for a base |

### Skills

| Method | Endpoint | Description |
|--------|----------|-------------|
| GET | `/api/skills` | List all installed skills |
| GET | `/api/skills/{skill_name}` | Get skill detail (content, origin, edit status) |
| POST | `/api/skills` | Create a new user skill |
| PUT | `/api/skills/{skill_name}` | Update a user skill's content |
| DELETE | `/api/skills/{skill_name}` | Delete a user skill |

### Schedules

| Method | Endpoint | Description |
|--------|----------|-------------|
| GET | `/api/schedules` | List scheduled tasks, including each task's `silent` delivery flag (filterable by room) |
| PUT | `/api/schedules/{task_id}` | Edit a scheduled task, including the optional `silent` delivery flag |
| DELETE | `/api/schedules/{task_id}` | Cancel a scheduled task |

### Workers

| Method | Endpoint | Description |
|--------|----------|-------------|
| GET | `/api/workers` | List active sandbox workers |
| POST | `/api/workers/cleanup` | Clean up idle sandbox workers |

### Token Usage

`GET /api/usage` returns organization-wide retained usage under the same standard dashboard authentication as other administrator APIs.
`GET /api/usage/export` exposes the same report to a collector through the dedicated signed service assertion described in [Trusted Upstream Authentication](https://docs.mindroom.chat/deployment/trusted-upstream-auth/#usage-export-service).
The two routes share report preparation and caching while authenticating every request through their own policy.
When a report needs preparation, either route returns `202` with `{"status":"pending"}` and `Retry-After: 5`; after preparation succeeds, an authenticated poll returns the completed report.
Every report-state response uses `Cache-Control: no-store`.

The JSON includes overall `totals`, an entity `breakdown`, a `model_breakdown`, and `user_breakdown`.
Each user has a canonical `user_id`, token `totals`, `run_count`, and their own `model_breakdown`.
Counters include input, output, total, cache read/write, reasoning, and audio tokens.
Models include their provider.
Stored per-model details split runs that use several models; older runs fall back to their recorded model.
Malformed or inconsistent model details retain the run's tokens under `unknown` and mark model coverage as incomplete.
Requester aliases are combined; `user_id: null` holds unattributed usage.

Use `GET /api/usage?include_daily=true` or `GET /api/usage/export?include_daily=true` to also return `daily_breakdown` and `daily_coverage`.
Each daily row includes a UTC `date`, combined token `totals`, `run_count`, and a `model_breakdown` with input, output, total, cache-read, cache-write, reasoning, and audio counters.
With `include_daily=true`, each entry in `user_breakdown` also includes its own `daily_breakdown` with that same row structure.
User aliases are combined before daily grouping, and the `user_id: null` entry includes daily unattributed usage.
Daily rows are sorted oldest first and use retained run creation timestamps.
Missing or invalid timestamps exclude the run from daily rows and mark daily coverage as incomplete.
Users with only undated retained runs have an empty `daily_breakdown`; their all-time totals still include those runs.
The report-level `daily_coverage` applies to both the overall and per-user daily breakdowns.
Omitting `include_daily` or setting it to `false` leaves out the daily fields.
The API and agent tools share storage reading, aggregation, and serialization.

User and model breakdowns cover stored top-level usage snapshots.
Each run save records its content-free usage in the same database transaction; later saves replace that run's snapshot.
Compaction, edits, and regeneration keep usage already incurred; a regenerated reply with a new run ID contributes separately.
Explicit whole-session erasure removes its usage too.
Startup imports available old run rows and session blobs once, without reconstructing missing history from logs or inventing dates or requester identity.
Historical conversion lives in `legacy_usage_storage.py`; reporting reads the current usage table only.
Breakdowns can still differ from session totals because history lost before migration and nested team-member usage may lack detailed attribution.
Deleted sessions are unavailable.
The `coverage`, `model_coverage`, and `user_coverage` fields describe missing sources and these limits.
`scanned_sources` counts discovered database candidates, including absent configured databases.
`unavailable_sources` also includes discovery failures and sources with incomplete metrics or attribution, so it is not a missing-token percentage or necessarily a subset of `scanned_sources`.
Verified historical aliases are ignored because their canonical directories are scanned separately; unverified symlinks remain coverage warnings.
This is a retained-usage report, not a billing ledger.
Responses contain no conversation content and use `Cache-Control: no-store`.

The organization-wide response also contains `private_agent_breakdown`, with one row per canonical `user_id` and `agent_name`.
Rows separate stored session `totals` and `session_count` from `retained_run_totals`, `run_count`, and `model_breakdown`.
They include `daily_breakdown` when `include_daily=true`, with the same input, output, cache-read, cache-write, reasoning, and audio counters.
Session totals use a validated private-instance owner or the recorded session requester; retained runs preserve their recorded requester, falling back to the validated owner when missing.
Unknown ownership remains `user_id: null`, and `private_agent_coverage` reports unavailable attribution or metrics.
History compacted before usage migration can contribute to session totals without recoverable model or daily detail.

For an ownership-based view, combine private-agent session usage attributed to owners with retained usage outside private-agent instances attributed to requesters.
Group private-agent rows by `user_id` and replace their retained-run contribution to `user_breakdown` with their session totals.
For each token counter and user, calculate `user_breakdown.totals - private_agent_breakdown.retained_run_totals + private_agent_breakdown.totals`, summing private-agent rows first and treating missing rows as zero.
Calculate over the union of users in both breakdowns; users with no private-agent row retain their full `user_breakdown.totals`.
Use values from the same response and retain `user_id: null` as unattributed usage.
For example, 60 retained tokens containing 20 private-agent tokens, plus a private-agent session total of 50, gives 90 tokens: `60 - 20 + 50`.
This includes private history whose detail was lost before usage migration without counting its stored usage twice.
It also replaces recorded-requester attribution for private runs with session ownership: if Bob requested 20 retained tokens from Alice's private instance with 100 session tokens, this view assigns those 100 tokens to Alice and none to Bob.
Keep `user_breakdown` unchanged when reporting who made the retained requests; the combined view answers a different ownership question.
Usage outside private-agent instances still relies on retained requester-attributed runs; a shared conversation's recorded requester is not the owner of every run.
This combined view does not recover historical dates or per-model splits for the additional private session totals, and incomplete coverage still applies.

Consumers should tolerate additional response fields and preserve the distinction between session totals and retained-run breakdowns.
Coverage corrections can change counter values without changing the response shape.
Treat each export as a snapshot, rather than adding successive all-time totals together.

#### Personal Private-Agent Usage

`GET /api/usage/me/private-agents?include_daily=true` returns the authenticated requester's usage across their configured private agents.
It shares the collector used by `get_my_private_usage()` and returns the same private rows, without `user_id` or an organization-wide `user_breakdown`.
Known requester aliases share one history; other users' databases and shared-agent databases are excluded.
The optional `include_daily` parameter defaults to `false`.

This endpoint requires trusted upstream authentication with signed JWTs and a verified Matrix identity, using the same identity checks as the personal Connections API.
An instance API key alone cannot select a personal user.
The requester comes only from authenticated identity; user, agent, and storage-path query overrides are rejected.
Ordinary authenticated users can access this personal endpoint, but their signed identity does not grant administrator dashboard access or access to the service export.

### Health & Readiness

| Method | Endpoint | Description |
|--------|----------|-------------|
| GET | `/api/health` | Liveness endpoint that returns `503` with `{"status": "unhealthy", "stale_sync_entities": [...]}` for stale Matrix sync after startup is ready, while `/api/ready` owns startup state before readiness. |
| GET | `/api/ready` | Returns `{"status": "ready"}` when the orchestrator has finished startup. Returns `503` with `{"status": "<phase>", "detail": "..."}` otherwise |

MindRoom tracks runtime phases internally:

| Phase | Meaning |
|-------|---------|
| `idle` | Process not started |
| `starting` | Startup in progress (detail message available) |
| `ready` | Orchestrator booted, serving requests |
| `failed` | Startup or runtime failure (detail message available) |

When the semantic-search embedder is failing, the health payload additionally carries `"embedder": {"status": "failing", "detail": ...}` with the classified cause; this block is diagnostic only and never flips liveness.
Use `/api/health` for liveness probes and `/api/ready` for readiness probes in container orchestrators.
Ordinary Matrix transport silence still reaches the watchdog after 120 seconds and makes `/api/health` return `503` after 180 seconds without a successful sync.
While Nio commits durable ingestion progress, the watchdog and `/api/health` consume the same monotonic progress snapshot and defer for up to `MINDROOM_MATRIX_INGESTION_GRACE_SECONDS` (default 600).
Both stop deferring when that grace expires or progress stops advancing for their respective silence timeout.
Successful sync completion refreshes both liveness clocks and clears the ingestion grace window.
After 90 seconds without sync or durable ingestion progress, `matrix_sync_stall_diagnostics` logs bounded await chains for that agent's sync, ingestion runner, ingestion pump, and delivery recovery tasks, including during first-sync and restarted-sync startup grace.
Reports repeat at most every 90 seconds and include sync age, time without progress, and ingestion generation without changing watchdog or readiness behavior.
Snapshots contain at most four tasks and 32 code locations per task, with strings capped at 240 characters; they exclude locals, message content, and source text, and stop at opaque Future or Task await boundaries.
Configure liveness probe `failureThreshold` to allow sufficient time for watchdog self-healing.

### Tools & Matrix

| Method | Endpoint | Description |
|--------|----------|-------------|
| GET | `/api/tools` | List available tools |
| GET | `/api/rooms` | List configured rooms |
| GET | `/api/matrix/agents/rooms` | Get all agents' room memberships |
| GET | `/api/matrix/agents/{id}/rooms` | Get specific agent's rooms |
| POST | `/api/matrix/rooms/leave` | Leave a single room |
| POST | `/api/matrix/rooms/leave-bulk` | Leave multiple rooms |
