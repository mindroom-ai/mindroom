# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

MindRoom - AI agents that live in Matrix and work everywhere via bridges. The project consists of:
- **Core MindRoom** (`src/mindroom/`) - AI agent orchestration system with Matrix integration
- **SaaS Platform** (`saas-platform/`) - Kubernetes-based platform for hosting MindRoom instances
  - Platform Backend (FastAPI) - API server for subscriptions, instances, SSO
  - Platform Frontend (Next.js 15) - Dashboard for managing instances
  - Instance deployment via Helm charts

## Architecture

### Core MindRoom (`src/mindroom/`)

**MultiAgentOrchestrator** (`orchestrator.py`) is the heart of the system - it boots every configured entity (router, agents, teams), provisions Matrix users, and keeps sync loops alive with hot-reload support when `config.yaml` changes.

**Entity types**:
- `router`: Built-in traffic director that greets rooms and decides which agent should answer
- **Agents**: Single-specialty actors defined under `agents:` in `config.yaml`
- **Teams**: Collaborative bundles of agents that coordinate or parallelize work

**Key modules**:
| Module | Purpose |
|--------|---------|
| `orchestrator.py` | MultiAgentOrchestrator - boots agents, manages sync loops, hot-reload |
| `bot.py` | AgentBot and TeamBot runtime for Matrix event handling, responses, and room behavior |
| `agents.py` | Agent creation and configuration |
| `config/` | Pydantic models for YAML config parsing (root model in `config/main.py`) |
| `routing.py` | Intelligent agent selection when no agent is mentioned |
| `teams.py` | Multi-agent collaboration (coordinate vs collaborate modes) |
| `memory/` | Mem0 memory: agent, room, and team-scoped |
| `knowledge.py` | Knowledge base / RAG file indexing with watcher |
| `skills.py` | Skill integration system (OpenClaw-compatible) |
| `plugins.py` | Plugin loading and tool/skill extension |
| `scheduling.py` | Cron and natural-language task scheduling |
| `tools/` | 100+ tool integrations |
| `tool_dependencies.py` | Auto-install per-tool optional dependencies at runtime |
| `ai.py` | AI model instantiation, caching, and response generation |
| `credentials.py` | Unified credential management (CredentialsManager) |
| `matrix/` | Matrix protocol integration (client, users, rooms, presence, provisioning, message formatting) |
| `matrix/large_messages.py` | Large-message sidecar storage and retrieval for oversized Matrix payloads |
| `matrix/message_content.py` | Canonical Matrix message content building for text, edits, and tool traces |
| `matrix/provisioning.py` | Hosted provisioning client flow used for local pairing and server-side agent registration |
| `commands.py` | Chat command parsing (`!help`, `!schedule`, `!skill`, etc.) |
| `voice_handler.py` | Voice message download, transcription, and command recognition |
| `sandbox_proxy.py` | Container sandbox proxy for isolating shell/python tools |
| `streaming.py` | Response streaming via progressive message edits |
| `agent_prompts.py` | Rich built-in prompts for named agents (code, research, etc.) |
| `image_handler.py` | Image message download, decryption, and AI processing |
| `api/` | FastAPI REST API (dashboard, credentials, OpenAI-compatible endpoint) |
| `custom_tools/` | Built-in custom tool implementations (gmail, calendar, scheduler, etc.) |
| `background_tasks.py` | Background task management for non-blocking operations |
| `tool_events.py` | Tool-event formatting and metadata for Matrix messages |
| `constants.py` | Shared constants, paths, and environment variable defaults |
| `error_handling.py` | User-friendly error message extraction |
| `authorization.py` | Sender and per-agent authorization checks |
| `thread_utils.py` | Thread analysis and agent detection |
| `file_watcher.py` | File change detection for config hot-reload |
| `config_confirmation.py` | Interactive config confirmation workflows |
| `interactive.py` | Interactive Q&A system via Matrix reactions |
| `stop.py` | StopManager for cancelling in-progress responses |
| `room_cleanup.py` | Orphaned bot cleanup from rooms |
| `topic_generator.py` | AI-generated room topics |
| `cli/main.py` | Main CLI entry point (Typer app) |
| `cli/banner.py` | CLI startup banner |
| `cli/config.py` | Config subcommand logic |
| `cli/connect.py` | `mindroom connect` pairing helpers and owner placeholder replacement |
| `cli/doctor.py` | Doctor command implementation |
| `cli/local_stack.py` | Local stack setup command |
| `config_commands.py` | Chat-based config commands (`!config`) |
| `credentials_sync.py` | `.env` to credentials vault sync |
| `logging_config.py` | Structured logging setup |
| `response_tracker.py` | Duplicate response prevention |
| `scheduling_context.py` | Scheduling tool context management |
| `tools_metadata.py` | Tool registry metadata and registration decorators |
| `knowledge_utils.py` | Multi-knowledge-base vector DB utilities |

**Persistent state** lives under `mindroom_data/` (next to `config.yaml`, overridable via `MINDROOM_STORAGE_PATH`):
- `sessions/` – Per-agent SQLite event history for Agno conversations
- `learning/` – Per-agent Agno Learning preference data
- `chroma/` – ChromaDB storage backing the memory system
- `knowledge_db/` – Knowledge base vector stores for file-backed RAG
- `tracking/` – Response tracking to avoid duplicate replies
- `credentials/` – JSON secrets synchronized from `.env`
- `encryption_keys/` – Matrix E2E encryption keys
- `culture/` – Shared culture state
- `logs/` – Log files
- `matrix_state.yaml` – Matrix sync state

### SaaS Platform (`saas-platform/`)
- **Platform Backend**: Modular FastAPI app with routes in `saas-platform/platform-backend/src/backend/routes/`
- **Platform Frontend**: Next.js 15 with centralized API client in `saas-platform/platform-frontend/src/lib/api.ts`
- **Authentication**: SSO via HttpOnly cookies across subdomains
- **Deployment**: Kubernetes with Helm charts, dual-mode support (platform/standalone)
- **Database**: Supabase with comprehensive RLS policies

### Repo Layout

| Path | Purpose |
|------|---------|
| `src/mindroom/` | Core agent runtime (Matrix orchestrator, routing, memory, tools) |
| `frontend/` | Core MindRoom dashboard (Vite + React) |
| `saas-platform/platform-backend/` | SaaS control-plane API (FastAPI) |
| `saas-platform/platform-frontend/` | SaaS portal UI (Next.js 15) |
| `saas-platform/supabase/` | Supabase migrations, policies, seeds |
| `cluster/` | Terraform + Helm for hosted deployments |
| `local/` | Docker Compose helpers for local dev stacks |

### Ecosystem Repositories

MindRoom also maintains related repositories under `github.com/mindroom-ai`:
- `mindroom-element` - our Element fork for MindRoom message UX: collapsible tool-trace rendering, `!` command autocomplete synced from backend commands, long-text sidecar hydration, AI run metadata tooltip, and MindRoom branding/thread-first defaults. See `README.md` and `FORK_CHANGES.md` in that repo.
- `synapse` - our Synapse fork for MindRoom streaming workloads: optional compact-edit collapsing for superseded `m.replace` events across `/sync`, Sliding Sync, pagination, and context responses, plus `/versions` advertisement via `org.mindroom.compact_edits` and fork-owned Docker/CI flows. See `README.md` and `FORK_CHANGES.md`.
- `mindroom-librechat` - our LibreChat fork that parses MindRoom inline `<tool>` / `<tool-group>` tags into native `ToolCall` cards so tool execution stays server-side; also includes fork Docker CI and MindRoom-specific UX additions. See `README.md` and `.mindroom/` docs (`fork-context.md`, `tool-tag-rendering.md`).
- `mindroom-cinny` - our Cinny fork optimized for MindRoom agent workflows with MindRoom branding/default homeserver config, subpath deployment support (runtime/build base path for `/mindroom`), and thread/auth/sidebar UX refinements. See `README.md` and `FORK_CHANGES.md`.
- `mindroom-stack` - a full Docker Compose reference stack that boots MindRoom backend/frontend, Synapse + Postgres + Redis, and Element together, including first-login and model/API-key setup guidance.

In this dev environment, many of these repositories are cloned in the parent directory (`../`) and can be inspected directly.

### Configuration Model

The authoritative config is `config.yaml`, loaded via Pydantic models in `src/mindroom/config/` (root model in `src/mindroom/config/main.py`):

```yaml
agents:
  code:
    display_name: CodeAgent
    role: Generate code, manage files, execute shell commands
    model: sonnet
    tools: [file, shell]
    instructions:
      - Always read files before modifying them.
    rooms: [lobby, dev]
    knowledge_bases: [engineering_docs]

defaults:
  tools: [scheduler]
  markdown: true
  enable_streaming: true

models:
  default:
    provider: anthropic
    id: claude-sonnet-4-5-latest
  sonnet:
    provider: anthropic
    id: claude-sonnet-4-5-latest

router:
  model: default

teams:
  super_team:
    display_name: Super Team
    role: Collaborative engineering assistant
    agents: [code]
    mode: collaborate

cultures:
  engineering:
    description: Follow clean code principles and write tests
    agents: [code]
    mode: automatic

knowledge_bases:
  engineering_docs:
    path: ./knowledge_docs
    watch: true

voice:
  enabled: false
  stt:
    provider: openai
    model: whisper-1

mindroom_user:
  username: mindroom_user
  display_name: MindRoomUser

matrix_room_access:
  mode: single_user_private
  multi_user_join_rule: public
  publish_to_room_directory: false
  invite_only_rooms: []
  reconcile_existing_rooms: false

authorization:
  default_room_access: false
  global_users:
    - __MINDROOM_OWNER_USER_ID_FROM_PAIRING__
  agent_reply_permissions:
    "*":
      - __MINDROOM_OWNER_USER_ID_FROM_PAIRING__

timezone: America/Los_Angeles
```

**Hot reloading**: `config.yaml` changes are watched at runtime. The orchestrator diffs configs, gracefully restarts affected agents, and rejoins rooms without bringing down the stack.

### Memory System

Mem0 memory (`src/mindroom/memory/functions.py`):
- **Agent memory** (`agent_<name>`) – Personal preferences, coding style, tasks
- **Team memory** – Shared context for team collaboration
- **Room memory** (`room_<id>`) – Project-specific knowledge

### Teams & Collaboration

Teams (`src/mindroom/teams.py`) let multiple agents work together:
- **coordinate**: Lead agent orchestrates others
- **collaborate**: All members respond in parallel with consensus summary

## 1. Core Philosophy

- **Embrace Change, Avoid Backward Compatibility**: This project has no end-users yet. Prioritize innovation and improvement over maintaining backward compatibility.
- **Simplicity is Key**: Implement the simplest possible solution. Avoid over-engineering or generalizing features prematurely.
- **Focus on the Task**: Implement only the requested feature, without adding extras.
- **Functional Over Classes**: Prefer a functional programming style for Python over complex class hierarchies.
- **Keep it DRY**: Don't Repeat Yourself. Reuse code wherever possible.
- **Be Ruthless with Code Removal**: Aggressively remove any unused code, including functions, imports, and variables.
- **Prefer dataclasses**: Use `dataclasses` that can be typed over dictionaries for better type safety and clarity.
- **Documentation Line Style**: In Markdown docs, write one sentence per line, and never split a single sentence across multiple lines.
- Do not wrap things in try-excepts unless it's necessary. Avoid wrapping things that should not fail.
- NEVER put imports in the function, unless it is to avoid circular imports. Imports should be at the top of the file.

### Refactor Policy

- Default to the smallest correct change.
- Use larger refactors when they provide clear immediate maintenance ROI, not hypothetical future value.
- A larger refactor is justified only if it:
  - Removes active duplication in current code paths.
  - Creates a clear source of truth without adding unnecessary abstraction layers.
  - Reduces net complexity (simpler call flow, fewer special cases).
  - Is covered by tests in the same PR.

## 2. Workflow

### Step 1: Understand the Context

- **Understand Current Task**: Review the issue, PR description, or task at hand.
- **Explore the Codebase**: List existing files and read the `README.md` to understand the project's structure and purpose.
- **READ THE SOURCE CODE**: This library has a `.venv` folder with all the dependencies installed. So read the source code when in doubt.
- **Consult Documentation**: Review documentation capabilities! If you're unsure, never guess. Do a search online.
- **Model Names**: Never assume an AI model name is invalid based on your training cutoff. Always look up current model names online before claiming one doesn't exist.

### Step 2: Environment & Dependencies

- **Environment Setup**: Use `uv sync --all-extras` to install all dependencies and `source .venv/bin/activate` to activate the virtual environment.
- **Adding Packages**: Use `uv add <package_name>` for new dependencies or `uv add --dev <package_name>` for development-only packages.

### Local Live Run (non-docker backend) + Matty smoke test

Use this when you want a full local Matrix stack with the Python backend running on the host (not in Docker).

1) Start/refresh Matrix (Synapse + Postgres + Redis)
```bash
# Optional if switching between remote and local homeservers
just local-matrix-reset
just local-matrix-up
curl -s http://localhost:8008/_matrix/client/versions | head -c 200
```

2) If you see login errors (M_FORBIDDEN) or changed homeserver, clear local Matrix state
```bash
rm -f mindroom_data/matrix_state.yaml
```

3) Ensure local OpenAI-compatible server is running on port 9292
```bash
curl -s http://localhost:9292/v1/models | head -c 200
```

4) Configure `config.yaml` to use the local OpenAI-compatible server
- Set relevant models to `provider: openai`
- Use a model ID that exists on the local server (e.g., `gpt-oss-low:20b`)
- Add `extra_kwargs.base_url: http://localhost:9292/v1` for those models
- For memory, prefer `provider: openai` and an embedding model that exists (e.g., `embeddinggemma:300m`)

5) Run the backend with explicit env overrides (use Python 3.13; production Dockerfile uses 3.12)
```bash
MATRIX_HOMESERVER=http://localhost:8008 \
MATRIX_SSL_VERIFY=false \
OPENAI_BASE_URL=http://localhost:9292/v1 \
OPENAI_API_KEY=sk-test \
UV_PYTHON=3.13 \
uv run mindroom run
```

6) Wait for API health, then for rooms to appear (room creation uses AI topics)
```bash
curl -s http://localhost:8765/api/health
MATRIX_HOMESERVER=http://localhost:8008 MATRIX_SSL_VERIFY=false \
uv run --python 3.13 matty rooms
```

7) Matty smoke test (agent reply in a thread)
```bash
MATRIX_HOMESERVER=http://localhost:8008 MATRIX_SSL_VERIFY=false \
uv run --python 3.13 matty send "Lobby" "Hello @mindroom_general:localhost please reply with pong."

MATRIX_HOMESERVER=http://localhost:8008 MATRIX_SSL_VERIFY=false \
uv run --python 3.13 matty threads "Lobby"

MATRIX_HOMESERVER=http://localhost:8008 MATRIX_SSL_VERIFY=false \
uv run --python 3.13 matty thread "Lobby" t1
```

### Hosted Matrix Run (`uvx`) + Pairing

Use this when Matrix + chat UI are hosted and only the MindRoom backend runs locally.

1) Initialize local config with hosted defaults
```bash
mkdir -p ~/mindroom-local
cd ~/mindroom-local
uvx mindroom config init --profile public
```

2) Add at least one model provider key in `.env` (for example `ANTHROPIC_API_KEY` or `OPENAI_API_KEY`)

3) Generate a pair code in `https://chat.mindroom.chat` (`Settings -> Local MindRoom`) and pair locally
```bash
uvx mindroom connect --pair-code ABCD-EFGH
```

4) Start MindRoom
```bash
uvx mindroom run
```

`mindroom connect` writes `MINDROOM_LOCAL_CLIENT_ID` and `MINDROOM_LOCAL_CLIENT_SECRET` to `.env` (unless `--no-persist-env` is used) and auto-replaces owner placeholder tokens in `config.yaml` when `owner_user_id` is returned.

### SaaS Platform Commands

#### Development
```bash
# Platform Backend
cd saas-platform/platform-backend
uv run uvicorn main:app --reload --host 0.0.0.0 --port 8000

# Platform Frontend
cd saas-platform/platform-frontend
bun install && bun run dev
```

#### Deployment
```bash
# Set kubeconfig path
export KUBECONFIG=./cluster/terraform/terraform-k8s/mindroom-k8s_kubeconfig.yaml

# Deploy platform
helm upgrade --install platform ./cluster/k8s/platform -f cluster/k8s/platform/values.yaml --namespace mindroom-staging

# Deploy instance - ALWAYS use the provisioner API:
./cluster/scripts/mindroom-cli.sh provision 1

# The provisioner handles everything:
# - Creates database records
# - Manages secrets securely
# - Deploys via Helm with proper values
# - Tracks status

# Manual Helm deployment (debugging only, not for production):
# helm upgrade --install instance-1 ./cluster/k8s/instance \
#   --namespace mindroom-instances \
#   -f values-with-secrets.yaml  # Never commit this file!

# Quick redeploy of MindRoom backend (updates all instances)
./saas-platform/redeploy-mindroom-backend.sh

# Deploy platform frontend or backend
./saas-platform/deploy.sh platform-frontend  # Build, push, and deploy frontend
./saas-platform/deploy.sh platform-backend   # Build, push, and deploy backend

# Use the CLI helper for common operations
./cluster/scripts/mindroom-cli.sh status
./cluster/scripts/mindroom-cli.sh list
./cluster/scripts/mindroom-cli.sh logs 1
```

### Step 3: Development & Git

- **Check for Changes**: Before starting, review the latest changes from the main branch with `git diff origin/main | cat`. Make sure to use `--no-pager`, or pipe the output to `cat`.
- **Commit Frequently**: Make small, frequent commits.
- **Atomic Commits**: Ensure each commit corresponds to a tested, working state.
- **Targeted Adds**: **NEVER** use `git add .`. Always add files individually (`git add <filename>`) to prevent committing unrelated changes.

### Step 4: Testing & Quality

- **Test Before Committing**: **NEVER** claim a task is complete without running `pytest` to ensure all tests pass.
- **Run Pre-commit Hooks**: Always run `pre-commit run --all-files` before committing to enforce code style and quality.
- **Handle Linter Issues**:
  - **False Positives**: The linter may incorrectly flag issues in `pyproject.toml`; these can be ignored.
  - **Test-Related Errors**: If a pre-commit fix breaks a test (e.g., by removing an unused but necessary fixture), suppress the warning with a `# noqa: <error_code>` comment.

### Step 5: Refactoring

- **Be Proactive**: Continuously look for opportunities to refactor and improve the codebase for better organization and readability.
- **Incremental Changes**: Refactor in small, testable steps. Run tests after each change and commit on success.

### Step 6: Viewing the Widget

- **Taking Screenshots**: To view the widget without Jupyter, use `python frontend/take_screenshot.py` from the project root.
- **Manual Screenshot**: From the frontend directory, run `bun run dev` to start the development server, then run `bun run screenshot` in another terminal.
- **Screenshot Location**: Screenshots are saved to `frontend/screenshots/` with timestamps.
- **Use Cases**: This is helpful for visual verification, documentation, and sharing the widget appearance.

### Developer Automation (`justfile`)

Common `just` recipes for development:
```bash
# Local stacks
just local-matrix-up              # Boot Synapse + Postgres dev stack
just local-platform-compose-up    # Full SaaS sandbox

# Testing
just test-backend                 # Run pytest for core
just test-saas-backend            # Run pytest for SaaS backend

# Deployment
just cluster-helm-template        # Render platform chart manifests
just cluster-helm-lint            # Lint platform chart
```

## 3. Critical "Don'ts"

- **DO NOT** manually edit the CLI help messages in `README.md`. They are auto-generated.
- **NEVER** use `git add .`.
- **NEVER** claim a task is done without passing all `pytest` tests.

## 4. Interacting with MindRoom Agents via Matty CLI

### Overview
Matty is a Matrix CLI client that allows you to interact with MindRoom AI agents. Use it to send messages and observe agent responses during development and testing.

### Prerequisites
```bash
# Matty is installed as a project dependency
# Activate the virtual environment
source .venv/bin/activate
# Now you can use matty directly
```

### Configuration
The Matrix credentials are already configured in the project's `.env` file. Matty will automatically use these credentials.

### Essential Commands for Agent Interaction

#### 1. List Rooms
```bash
matty rooms  # or: matty r
```

#### 2. View Messages (See Agent Responses)
```bash
matty messages "room_name" --limit 20  # or: matty m "room_name" -l 20
```

#### 3. Send Messages to Agents
```bash
# Direct message
matty send "room_name" "Hello @mindroom_assistant!"

# Multiple agent mentions
matty send "room_name" "@mindroom_research @mindroom_analyst analyze this topic"
```

#### 4. Work with Threads (Agents respond in threads)
```bash
# List threads in a room
matty threads "room_name"

# View thread messages (where agents typically respond)
matty thread "room_name" t1  # View thread with ID t1

# Start a thread (agents will respond here)
matty thread-start "room_name" m2 "Starting discussion with agents"

# Reply in thread
matty thread-reply "room_name" t1 "@mindroom_assistant continue"
```

### Typical Agent Testing Workflow
```bash
# 1. Find the test room
matty rooms

# 2. Send a message mentioning agents
matty send "test_room" "@mindroom_assistant What can you do?"

# 3. Check for agent response (agents respond in threads)
matty threads "test_room"
matty thread "test_room" t1  # View the thread where agent responded

# 4. Continue conversation in thread
matty thread-reply "test_room" t1 "@mindroom_research find information about X"
```

### Important Notes
- **Agents respond in threads**: Always check threads after sending messages
- **Use @mentions**: Tag agents with @ to get their attention
- **Message handles**: Use m1, m2, m3 to reference messages
- **Thread IDs**: Use t1, t2, t3 to reference threads (persistent across sessions)
- **Output formats**: Add `--format json` for machine-readable output
- **Streaming responses**: If you see "⋯" in agent messages, they're still typing. Agents stream responses by editing messages, which may take 10+ seconds to complete. Re-check the thread after waiting.

## 5. Quick Reference

```bash
# Preflight check before running
mindroom doctor

# Run the stack
uv run mindroom run --storage-path mindroom_data

# Pair local install with hosted provisioning
mindroom connect --pair-code ABCD-EFGH

# Bootstrap local Synapse + MindRoom Cinny (Docker)
mindroom local-stack-setup --synapse-dir /path/to/mindroom-stack/local/matrix

# Update credentials
# Edit .env and restart; sync step mirrors keys to credentials vault

# Discover commands
# Send !help from any bridged room

# Debug logging
mindroom run --log-level DEBUG  # Surface routing decisions, tool calls, config reloads
```

Inspect agent traces: `mindroom_data/sessions/<agent>.db`

## 6. Releases

Use `gh release create` to create releases. The tag is created automatically.

```bash
# IMPORTANT: Ensure you're on latest origin/main before releasing!
git fetch origin
git checkout origin/main

# Check current version
git tag --sort=-v:refname | head -1

# Create release (minor version bump: v0.2.2 -> v0.3.0)
gh release create v0.3.0 --title "v0.3.0" --notes "release notes here"
```

Versioning:
- **Patch** (v0.2.2 -> v0.2.3): Bug fixes
- **Minor** (v0.2.3 -> v0.3.0): New features, non-breaking changes

Write release notes manually describing what changed. Group by features and bug fixes.

# Important Instruction Reminders
Do what has been asked; nothing more, nothing less.
NEVER create files unless they're absolutely necessary for achieving your goal.
ALWAYS prefer editing an existing file to creating a new one.
NEVER proactively create documentation files (*.md) or README files. Only create documentation files if explicitly requested by the User.
