# MindRoom Agents

This guide explains how MindRoom’s multi-agent stack is wired together, how agents are defined in configuration, and how to extend or operate the system safely. It replaces the older `CLAUDE.md` with up-to-date details drawn straight from the current codebase.

## Architecture at a Glance

- **MultiAgentOrchestrator** (`src/mindroom/bot.py`): boots every configured entity (router, single agents, and teams), provisions Matrix users, and keeps their sync loops alive with automatic restart logic and hot-reload support when `config.yaml` changes.
- **Entity types**:
  - `router`: the built-in traffic director that greets rooms and can decide which agent should answer if no one is explicitly mentioned.
  - **Agents**: single-specialty actors defined under `agents:` in `config.yaml`.
  - **Teams**: collaborative bundles of agents defined under `teams:` that coordinate or parallelize work via Agno’s team runner (`src/mindroom/teams.py`).
- **Matrix-first runtime**: every entity is materialized as a dedicated Matrix account (`src/mindroom/matrix/users.py`), auto-joins the rooms listed in configuration (`src/mindroom/matrix/rooms.py`), and talks through the Matrix bridges you already operate.
- **Persistent state** lives under `mindroom_data/` (overridable via `STORAGE_PATH`):
  - `sessions/` – per-agent Sqlite event history for Agno conversations.
  - `memory/` – Mem0 vector store backing agent and room memories.
  - `tracking/` – response tracking to avoid duplicate replies.
  - `credentials/` – JSON secrets synchronized from `.env` (`src/mindroom/credentials_sync.py`).

### Repo Layout & Supporting Apps

| Path | What lives here | Notes |
| --- | --- | --- |
| `src/mindroom/` | Core agent runtime (Matrix orchestrator, routing, memory, tools) | Python package shipped via `mindroom` CLI |
| `frontend/` | Core MindRoom dashboard (Vite + React) | Connects to agents via Matrix bridges and local backend APIs |
| `saas-platform/platform-backend/` | SaaS control-plane API (FastAPI) | Powers multi-tenant provisioning, billing, and admin surfaces |
| `saas-platform/platform-frontend/` | SaaS portal UI (Next.js) | Customer/admin portal that talks to the SaaS backend and Supabase |
| `saas-platform/supabase/` | Supabase migrations, policies, seeds | Mirrors tables used by the SaaS portal and platform backend |
| `saas-platform/redeploy-*.sh`, `saas-platform/deploy.sh` | K8s deployment helpers | Build/push images and rollout restarts for hosted clusters |
| `cluster/` | Terraform + Helm for hosted deployments | Automates kube clusters, images, ingress, and secrets |
| `local/` | Docker Compose helpers for matrix + instances | Used by `just local-*` recipes for quick local stacks |
| `tools/`, `scripts/` | Utility scripts, avatar generation, automation | Invoked by CI, `just`, or manual ops |

Understanding these layers helps when extending agents: the Matrix runtime is the beating heart, while the SaaS/portal components wrap it in commercial tooling for provisioning and billing.

## Configuration Model

The authoritative configuration is `config.yaml`. The schema is codified in `src/mindroom/config.py` and loaded with `Config.from_yaml`, so every field shown below maps directly to a pydantic model.

```yaml
agents:
  code:
    display_name: CodeAgent
    role: Generate code, manage files, and execute shell commands.
    model: sonnet
    tools: [file, shell]
    instructions:
      - Always read files before modifying them.
    rooms: [lobby, dev, automation]

defaults:
  num_history_runs: 5
  markdown: true

models:
  sonnet:
    provider: anthropic
    id: claude-sonnet-4-latest

router:
  model: ollama

teams:
  super_team:
    display_name: Super Team
    role: A team with all the great agents.
    agents: [code, shell, research, finance]
    mode: collaborate

timezone: America/Los_Angeles
```

Key sections:

- **`agents`** – each key becomes an internal agent name; values map to `AgentConfig` (`src/mindroom/config.py:26`). The `rooms` list accepts aliases (auto-resolved to IDs) and controls auto-join behavior.
- **`defaults`** – baseline behavior used whenever an agent does not override settings such as markdown rendering or history length.
- **`models`** – named providers and identifiers for LLM backends. Model names referenced by agents, teams, the router, and the memory subsystem must exist here.
- **`router`** – selects the model used for routing suggestions (`src/mindroom/routing.py`).
- **`room_models`** – optional per-room overrides if certain spaces should be backed by lighter or faster models.
- **`teams`** – definitions bound to `TeamConfig`; include the target `mode` (`coordinate` vs `collaborate`) and optional `rooms` for team auto-join.
- **`memory` / `voice` / `authorization`** – configure cross-room memory storage, speech handling, and fine-grained access lists.

### Default Agents Snapshot

The repo ships with a curated suite of agents; highlights are below. Refer to `config.yaml` for the full list and to adjust rooms, tools, or models.

| Agent | Display name | Purpose | Tools |
| --- | --- | --- | --- |
| `general` | GeneralAgent | Friendly fallback assistant for everyday questions | _None (language only)_ |
| `code` | CodeAgent | Writes and edits code, executes controlled shell commands | `file`, `shell` |
| `research` | ResearchAgent | Multi-source research with citations | `duckduckgo`, `wikipedia`, `arxiv` |
| `finance` | FinanceAgent | Market data and calculations | `yfinance`, `calculator` |
| `sleepy_paws` | Sleepy Paws 🐾 | Dutch bedtime storyteller with rich instructions | _None_ |
| `agent_builder` | AgentBuilder | Conversational assistant for editing configs through the config manager toolkit | `config_manager` |

Teams such as `super_team` bundle these capabilities and can be summoned with a single mention; their collaboration logic is detailed in [Teams & Collaboration](#teams--collaboration).

### Hot Reloading & Deploy Flow

`config.yaml` changes are watched at runtime via `watch_file` (`src/mindroom/bot.py:2368`). When edits land, the orchestrator diffs the old and new config, gracefully restarts affected agents, applies new presence/status metadata, and rejoins the necessary rooms. This allows iterative tuning without bringing down the entire stack.

## SaaS Platform Context

The SaaS control plane wraps the core agents so you can vend managed rooms for customers:

- **Platform backend** (`saas-platform/platform-backend`, FastAPI): exposes admin routes for provisioning Matrix instances, synchronizes billing via Stripe webhooks, and surfaces metrics for the React Admin dashboards (`README.md` in that folder outlines routes and environment). Kubernetes helpers under `backend/k8s.py` drive per-tenant workloads.
- **Platform frontend** (`saas-platform/platform-frontend`, Next.js): customer and admin portal that consumes the backend, Supabase auth, and Stripe billing APIs (`README.md` there enumerates app router structure and required env vars).
- **Supabase schema** (`saas-platform/supabase`): migrations, policies, and seeds used by both portal and backend for accounts, subscriptions, and audit trails.
- **element-web overlay** (`saas-platform/element-web`): packaged Element client with branded theming to give customers a hosted Matrix web UI.

Local Compose stacks in `saas-platform/docker-compose.yml` spin up backend, frontend, Supabase, and Element together. Use `just local-platform-compose-up` for a full SaaS sandbox and `just docker-build-saas-*` to bake release images.

## Agent Lifecycle

The execution path for a single agent response spans several modules:

1. **Creation & identity** – `create_agent` (`src/mindroom/agents.py:63`) reads the agent’s configuration, instantiates its toolkits via `get_tool_by_name`, attaches identity context (`AGENT_IDENTITY_CONTEXT` in `src/mindroom/agent_prompts.py`), plus current date/time (`get_datetime_context`). Sessions are backed by a per-agent Sqlite store in `mindroom_data/sessions/`.
2. **Prompt assembly** – Agents with a rich prompt (e.g., `code`, `research`, `summary`) receive a handcrafted instruction block; others rely on YAML `role`/`instructions`. The interactive-question helper is appended for UX consistency.
3. **Runtime orchestration** – When a message arrives, `MultiAgentOrchestrator` delegates to either the router or a targeted agent/TEAM bot (`src/mindroom/bot.py`). Each `AgentBot` logs into Matrix, maintains presence, and spins a sync loop with retry logic.
4. **Response streaming & cancellation** – Depending on requester presence (`src/mindroom/matrix/presence.py`), the bot either streams partial replies using edits (`src/mindroom/streaming.py`) or sends a single message. The `StopManager` allows users to cancel generation by reacting with 🛑.
5. **Memory capture** – After responding, the conversation is pushed into agent and room memory stores (`src/mindroom/memory/functions.py`). Memories are retrieved on future turns via `build_memory_enhanced_prompt` to provide context.
6. **Tracking & deduplication** – `ResponseTracker` records served Matrix event IDs so agents skip duplicate work if the same event replays.

## Routing & Mention Handling

- **Mention parsing** – `thread_utils.get_mentioned_agents` and related helpers identify which entities were tagged in a message or thread. Router and teammate participation history is used to avoid spurious replies from agents already involved.
- **Automatic agent suggestions** – When nobody is explicitly addressed, the router runs `suggest_agent_for_message` (`src/mindroom/routing.py`), packaging available agents, their roles, tools, and prior thread context. The router’s model (default `ollama`) predicts the best responder.
- **Room scoping** – Only agents whose `rooms` list includes the current room are considered eligible, keeping private or high-signal rooms clean.
- **Welcome flow** – The router greets rooms with a dynamically generated roster (`_generate_welcome_message` in `src/mindroom/bot.py:112`), summarizing each agent’s role and toolset for quick discovery.

## Memory & Context

MindRoom implements Mem0-inspired dual memory (`src/mindroom/memory/functions.py`):

- **Agent memory** – Stored per agent (namespaced as `agent_<name>`) so personal preferences, coding style, or recurring tasks persist across rooms.
- **Team memory** – Agents also search shared team memories for any team they belong to, allowing coordinated workflows to build shared context.
- **Room memory** – Messages tagged as room-specific context are stored under `room_<id>` so project channels keep local state without leaking to private chats.
- **Embedding & recall config** – Controlled through `memory.embedder` and `memory.llm` in `config.yaml`, typically pointing to self-hosted Ollama endpoints for privacy and cost control.

`build_memory_enhanced_prompt` injects retrieved snippets into the final prompt before generation, ensuring agents stay grounded in prior interactions.

## Tools & Credentials

- **Dynamic registry** – All tools register via decorators in `src/mindroom/tools/__init__.py`, attaching rich metadata (`src/mindroom/tools_metadata.py`) including setup requirements, icons, and dependency hints. `get_tool_by_name` instantiates the toolkit, preloading any saved credentials.
- **Credential management** – Secrets are persisted under `mindroom_data/credentials/` by `CredentialsManager` (`src/mindroom/credentials.py`). `.env` values are auto-synced on startup so the CLI remains the source of truth.
- **Config Manager toolkit** – The `config_manager` tool (`src/mindroom/custom_tools/config_manager.py`) exposes consolidated actions (inspect, create, update, validate agents/teams) and is primarily used by `agent_builder` for conversational configuration edits.
- **Custom tools** – Drop new toolkits under `src/mindroom/custom_tools/` or extend the registry; include metadata so the React dashboard can surface setup instructions.

## Teams & Collaboration

Teams let multiple agents work a request together (`src/mindroom/teams.py`):

- **Modes** – `coordinate` appoints a lead agent to orchestrate others; `collaborate` lets all members respond in parallel with a consensus summary when available.
- **Formatting** – Responses are rendered with explicit headers per agent, nested recursion for sub-teams, and optional consensus blocks for transparency.
- **Model selection** – Teams can override their model; otherwise, they inherit the defaults. `select_model_for_team` respects `team_config.model`, enabling heavy reasoning models only when needed.
- **Availability** – Team bots join any rooms listed under the team configuration and inherit mention and routing behavior like individual agents.

## Scheduling & Background Workflows

- `!schedule`, `!list_schedules`, and `!cancel_schedule` commands are parsed by `CommandParser` (`src/mindroom/commands.py`).
- Natural-language schedule requests are parsed into cron or one-off workflows via `parse_workflow_schedule` (`src/mindroom/scheduling.py`), which uses the default model to interpret timing, target agents, and condition checks.
- Executions run as background asyncio tasks with durable storage in Matrix state events, ensuring restarts reload pending jobs.
- Background tasks use `create_background_task` (`src/mindroom/background_tasks.py`) so long-running operations never block the sync loop.

## Runtime Operations & Monitoring

- **Presence** – `set_presence_status` and `build_agent_status_message` (`src/mindroom/matrix/presence.py`) broadcast which model and role each agent is using.
- **Stop reactions** – Users can cancel streaming replies; `StopManager` (`src/mindroom/stop.py`) removes the stop reaction and cancels the generation coroutine.
- **Config updates** – The orchestrator compares old/new configs (`_identify_entities_to_restart` in `src/mindroom/bot.py`) and only restarts changed entities, minimizing churn.
- **Cleanup** – `room_cleanup.py` and `ensure_all_rooms_exist` keep Matrix rooms aligned with configuration, pruning orphaned bots and ensuring avatars/topics remain accurate.

## Extending MindRoom Agents

1. **Add a new agent**
   - Define it under `agents:` in `config.yaml` or ask `@AgentBuilder` to scaffold it via the config manager tool.
   - Choose the right tools from the registry; custom toolkits can be authored under `src/mindroom/tools/` and registered with metadata for UI support.
   - Tailor instructions or create a dedicated rich prompt in `src/mindroom/agent_prompts.py` if the agent needs a bespoke identity.

2. **Create a team**
   - Add a `teams:` entry specifying `agents`, `mode`, and optional `rooms`. Mention the team handle (e.g., `@mindroom_super_team`) in Matrix to trigger collaborative responses.

3. **Override models per room**
   - Use `room_models:` in `config.yaml` so certain channels automatically route to lighter or faster models when the router or default agent would otherwise choose something heavier.

4. **Wire custom workflows**
   - Implement bespoke scheduling actions or background automations by extending `src/mindroom/scheduling.py`. New workflow types can piggyback on the same cron parsing and Matrix messaging helpers.

5. **Inspect and debug**
   - Check `mindroom_data/sessions/<agent>.db` for Agno conversation traces.
   - Enable `DEBUG` logging via `mindroom run --log-level DEBUG` to surface routing decisions, tool calls, and config reloads.

## Developer Automation (`justfile`)

MindRoom standardizes developer and ops flows through `just` recipes (see root `justfile`):

- **Matrix + core stacks**: `just local-matrix-up` boots a Synapse + Postgres dev stack, while `just local-instances-*` manages per-customer Compose environments via `local/instances/deploy`.
- **SaaS platform**: `just local-platform-compose-up` launches the platform portal/backends; `start-saas-*-dev` kicks off hot-reload dev servers in the respective directories.
- **Testing**: `just test-backend`, `test-front`, `test-saas-backend`, and `test-saas-frontend` wrap uv/pytest and bun test runners to ensure parity with CI.
- **Infrastructure**: `cluster-*` recipes lint/render Helm charts, drive Terraform lifecycle, or stand up Kind-based preview clusters (with Nix shells for repeatability).
- **Docker builds**: handy shortcuts for building dev images for core agents or the SaaS portal (`docker-build-*`).
- **Environment sync**: `just env-saas` prints evaluated SaaS `.env` variables so you can export them into shells or CI jobs.
- **Kubernetes redeploy shortcuts**: run `just redeploy-mindroom-backend` / `just redeploy-mindroom-frontend` to build, push, and restart every customer deployment, or `just deploy target=platform-backend` (etc.) for one-off rollouts using the underlying scripts in `saas-platform/`.

These commands are the canonical way to bootstrap local dev, run focused suites, or deploy to playground clusters without memorizing long scripts.

## Quick Reference

- Run the stack: `uv run mindroom run --storage-path mindroom_data` (or use `run-backend.sh` / `run-frontend.sh` / `just` recipes).
- Update credentials: edit `.env` and restart; the sync step will mirror keys to the credentials vault.
- Discover commands: `!help` from any bridged room lists scheduling, widget, and configuration commands alongside usage tips.
- Keep documentation current: whenever `config.yaml`, tools, or prompts change, refresh this document to reflect new capabilities.

MindRoom’s agent subsystem is intentionally composable—add new tools, teams, or workflows as your organization’s playbook solidifies, and the orchestrator will make sure every Matrix room sees the right specialists at the right time.
