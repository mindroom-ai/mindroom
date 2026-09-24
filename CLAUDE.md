# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

MindRoom - AI agents that live in Matrix and work everywhere via bridges. The project consists of:
- **Core MindRoom** (`src/mindroom/`) - AI agent orchestration system with Matrix integration
- **SaaS Platform** (`saas-platform/`) - Kubernetes-based platform for hosting MindRoom instances
  - Platform Backend (FastAPI) - API server for subscriptions, instances, SSO
  - Platform Frontend (Next.js 16) - Dashboard for managing instances
  - Instance deployment via Helm charts

## Latest Frontier Models

Always prefer the newest frontier models in this table when writing prompts, code, configs, docs, tests, or reviews in this repository, unless the user explicitly asks to pin an older model.
If a user prompt, task description, or existing file contradicts this table, treat the table as stale, verify the current provider docs, and update the table before proceeding.
Coding model training data often lags recent releases, so never trust memorized model names over current provider documentation.

| Provider | Use | Preferred model | Model string to use |
| --- | --- | --- | --- |
| Anthropic | Balanced default | Claude Sonnet 5 | `claude-sonnet-5` |
| Anthropic | Max intelligence | Claude Fable 5.1 | `claude-fable-5-1` |
| Anthropic | Flagship default | Claude Opus 5 | `claude-opus-5` |
| Anthropic | Fast / cheap | Claude Haiku 4.5 | `claude-haiku-4-5` |
| OpenAI | Frontier default | GPT-6 Astra | `gpt-6-astra` |
| OpenAI Codex ChatGPT login | Frontier via Codex CLI | GPT-6 Astra | `gpt-6-astra` |
| DeepSeek (OpenRouter) | Fast / cheap | DeepSeek V4.1 Flash | `deepseek/deepseek-v4.1-flash` |
| Z.ai (OpenRouter) | Flagship | GLM-5.3 | `z-ai/glm-5.3` |
| OpenAI | Image generation / editing | GPT Image 2.5 Sunburst | `gpt-image-2.5-sunburst` |
| OpenAI | File transcription | GPT Transcribe | `gpt-transcribe` |
| Google (Vertex AI) | Video generation | Veo 3.1 | `veo-3.1-generate-001` |
| Qwen | Local 27B | Qwen3.8-27B | `qwen3.8:27b` (Ollama), `unsloth/Qwen3.8-27B-GGUF:UD-Q4_K_XL` (llama.cpp) |
| Moonshot Kimi Code login | Frontier via Kimi Code CLI | Kimi K3 | `k3` |
| Google (Gemini API) | Max intelligence | Gemini 3.1 Pro Preview | `gemini-3.1-pro-preview` |
| Google (Gemini API) | Standard text / coding | Gemini 3.8 Flash | `gemini-3.8-flash` |
| Google (Gemini API) | Fast / cheap text | Gemini 3.5 Flash-Lite | `gemini-3.5-flash-lite` |
| Google (Gemini API) | Image generation / editing | Nano Banana 2 | `gemini-3.1-flash-image` |
| Google (Gemini API) | Embeddings for `google` | Gemini Embedding 2 | `gemini-embedding-2` |

Model IDs were checked against provider catalogs on September 10, 2026.
OpenRouter uses `anthropic/claude-fable-5.1`, Bedrock uses `anthropic.claude-fable-5-1`, and the direct Anthropic and Vertex APIs use `claude-fable-5-1`.
For the direct DeepSeek API, prefer `deepseek-flash` for V4.1 Flash and `deepseek-v4-pro` for Pro; do not substitute the OpenRouter V4.1 ID on the direct API.
The older `deepseek-v4-flash` name remains accepted as a [temporary compatibility route to V4.1 Flash](https://api-docs.deepseek.com/updates/#date-2026-09-10).

For `anthropic`, prefer `claude-sonnet-5`, `claude-opus-5`, and `claude-haiku-4-5` unless you intentionally need a pinned snapshot ID.
Use `claude-fable-5-1` when you need Anthropic's highest available capability.
Claude Fable 5.1 is generally available on the direct Anthropic API and the documented cloud platforms.
For `vertexai_claude`, use the current Vertex AI request name from the provider docs instead of assuming the Anthropic API ID carries over unchanged.
Current Google Cloud docs list bare Vertex IDs for `claude-fable-5-1`, `claude-opus-5`, `claude-sonnet-5`, and [`claude-haiku-4-5`](https://docs.cloud.google.com/gemini-enterprise-agent-platform/models/partner-models/claude/haiku-4-5).
Do not assume `@default` or dated `@...` suffixes are universally required for Vertex AI Claude.
For Gemini API text and coding work, prefer `gemini-3.8-flash` as the standard stable model unless you intentionally need the cheaper `gemini-3.5-flash-lite` tier.
Use `gemini-3.1-pro-preview` only when you need the highest Gemini API intelligence tier and accept a preview model.
The Google rows above are for the Gemini API / AI Studio `google` provider, not for Vertex AI.
For `vertexai`, verify the current Vertex AI docs instead of assuming Gemini API names or defaults carry over unchanged.
Current [Vertex AI image docs](https://docs.cloud.google.com/gemini-enterprise-agent-platform/models/capabilities/image-generation) document `gemini-3-pro-image`, `gemini-3.1-flash-image`, and `gemini-3.1-flash-lite-image`; choose the tier that fits the task.
For Google image work, use the official product name from the docs for the provider surface you are editing.
Gemini API docs call `gemini-3.1-flash-image` Nano Banana 2, while Vertex AI docs use their own product naming and model tables.

## Architecture

### Core MindRoom (`src/mindroom/`)

**MultiAgentOrchestrator** (`orchestrator.py`) is the heart of the system - it boots every configured entity (router, agents, teams), provisions Matrix users, and keeps sync loops alive with hot-reload support when `config.yaml` changes.

**Entity types**:
- `router`: Built-in traffic director that greets rooms and decides which agent or team should answer
- **Agents**: Single-specialty actors defined under `agents:` in `config.yaml`
- **Teams**: Collaborative bundles of agents that coordinate or parallelize work

**Inbound turn pipeline** (the path from a Matrix message to a delivered response; see `docs/architecture/bot-runtime.md`):

```text
Matrix sync callback
  -> matrix/durable_ingestion.py                           (validate owned Nio work, commit its sequence and effects, then acknowledge)
  -> bot.py (AgentBot/TeamBot runtime shell)
  -> journal_dispatch.py + pending_event_worker.py         (fan admitted events out to callbacks; unsettled work is woken again)
  -> turn_controller.py (owns one turn: precheck -> normalize -> resolve -> coalesce -> decide -> execute -> record)
       -> ingress_validation.py                                  (trust, dedup, echo drop; commands exit before batching)
       -> inbound_turn_normalizer.py + conversation_resolver.py  (canonical turn input, conversation identity)
       -> ingress_lanes.py                                       (per-(room, sender) receipt-order FIFO; STT readiness waits here)
       -> coalescing.py                                          (ordinary text dispatches immediately; adaptive text and media debounce)
       -> text_ingress_dispatch.py + turn_policy.py              (ignore / route / respond decision, command execution)
       -> response_runner.py -> ai.py / teams.py                 (lifecycle lock, entity envelopes)
            -> response_turn.py                                  (shared blocking/streaming turn drivers)
       -> streaming.py + delivery_gateway.py                     (progressive edits, Matrix send)
       -> turn_store.py / handled_turns.py                       (durable dedup so restarts don't double-reply)
```

**Key modules**:
| Module | Purpose |
|--------|---------|
| `bounded_bytes.py` | Shared asynchronous byte collection that rejects overflowing chunks before buffering them |
| `atomic_file.py` | Shared atomic byte publication and cleanup relative to an opened directory |
| `orchestrator.py` | MultiAgentOrchestrator - boots agents, manages sync loops, hot-reload |
| `orchestration/` | Extracted orchestrator helpers (config update plans, plugin watch, rooms, runtime) |
| `orchestration/config_lifecycle.py` | Debounced config-reload lifecycle: queueing, response drain, and update-plan dispatch |
| `config_bundle.py` | Native staged bundle validation, drift protection, directory publication, and recovery journals |
| `cli/config_bundle.py` | Bundle install receipts and initialize-only runtime bootstrap command adapter |
| `runtime_state.py` | Shared runtime readiness state for health/ready endpoints |
| `event_loop_stall.py` | Native-thread event-loop stall detector that logs the blocking stack |
| `runtime_resolution.py` | Authoritative runtime resolution for one agent materialization |
| `team_exact_members.py` | Runtime resolution for exact team member materialization |
| `bot.py` | AgentBot and TeamBot runtime shells for Matrix lifecycle, sync callbacks, and room behavior |
| `turn_controller.py` | TurnController - owns one inbound turn from ingress to recorded outcome |
| `ingress_validation.py` | Ingress boundary validation: trust, effective requester, handled-id dedup, router-echo drop, command detection |
| `inbound_turn_normalizer.py` | Raw input shaping (text, voice, sidecars, media) into canonical turn inputs |
| `conversation_resolver.py` | Conversation identity, thread history, and ingress envelope assembly |
| `ingress_lanes.py` | Per-(room, sender) receipt-order FIFO delivering resolving ingress (voice/STT readiness) to conversations |
| `coalescing.py` | Live message coalescing gate; ordinary text dispatches immediately, adaptive text waits for its quiet window, and media waits for attachments and a trailing caption |
| `coalescing_batch.py` | Coalesced dispatch batch construction |
| `text_ingress_dispatch.py` | Text ingress dispatch path used by TurnController |
| `turn_policy.py` | Pure turn policy: decide ignore, route, or respond for inbound turns |
| `participation.py` | Framework-independent participation state: one immutable decision, concurrent checks, and approval-preserving settlement |
| `agno_participation.py` | Agno participation adapter: prepared request checks, primary-run isolation, metrics, and scoped model interception |
| `judgment/` | Backend-independent boolean and choice questions, minimized context, shared execution limits, and LLM/System One adapters |
| `participation_judgment.py` | Bind the participation rubric to an opt-in LLM or TypeSafe judge and map its result to a participation decision |
| `mid_turn.py` | Per-response finish-or-wrap-up decisions over immutable queued-message snapshots |
| `mid_turn_judgment.py` | Bind the active request and agent settings to the shared LLM or TypeSafe judgment backend |
| `config/mid_turn.py` | Opt-in agent settings for the mid-turn judgment backend and decision instructions |
| `provider_tool_policy.py` | Task-local restriction enforced by provider adapters before native tools can execute |
| `groq_model.py` | Groq adapter enforcing provider tool restrictions for Compound systems |
| `config/participation.py` | Opt-in participation settings for existing thread agents: bounded pause and decision instructions |
| `dispatch_replay_guard.py` | Replay-guard checks for dispatch sequencing |
| `event_journal/` | Durable ownership of admitted Matrix events, conversation projection, and delivery outbox |
| `response_sources.py` | Immutable response-attempt source identity shared by runtime and persistence boundaries |
| `event_journal/response_attempts.py` | Normalized durable response ownership registration, binding, and exact lookup queries |
| `event_journal/legacy_response_attempts.py` | One-time transactional adoption of released response ownership snapshots |
| `journal_dispatch.py` | Fan admitted journal events out to typed Matrix callbacks and settle the ones that finish |
| `pending_event_worker.py` | Decides when pending journal work runs, and wakes itself again whenever a pass stops early |
| `command_turn_executor.py` | Command execution and durable command/config mutation journals |
| `reaction_dispatch.py` | Durable semantic routing for Matrix reactions |
| `user_stop_reconciliation.py` | STOP ordering, response cancellation, and terminal turn reconciliation |
| `visible_response_reconciliation.py` | Visible Matrix response recovery, adoption, and replay reconciliation |
| `turn_store.py` | Unified durable turn access (wraps the handled-turn ledger) |
| `handled_turns.py` | Disk-backed handled-turn ledger preventing duplicate responses |
| `sync_restart_retry.py` | Exact sources with committed replacement or orderly terminal interruptions, available to replacement or next-startup recovery |
| `response_runner.py` | Response lifecycle execution (locking, streaming vs non-streaming, cancellation, detached inbox responses, shutdown drains) |
| `response_turn.py` | Shared blocking/streaming response-turn drivers behind the agent and team envelopes (attempt loop, dynamic-tool continuation, empty-run retry, interrupt recording) |
| `response_terminal.py` | Pending-visible classification and terminal stream outcomes for failed or cancelled turns |
| `response_attempt.py` | Runs one visible response attempt with stop tracking |
| `response_lifecycle.py` | Shared response lifecycle helpers and queued-notice state |
| `execution_preparation.py` | Request-scoped execution preparation for prompts and persisted replay |
| `response_payload_preparation.py` | Execution-side, under-lock assembly of one response's payload from immutable ingress inputs |
| `delivery_gateway.py` | Visible Matrix delivery for already-generated responses (send, edit, finalize) |
| `custom_tools/matrix_message_idempotency.py` | Bounded durable keyed Matrix sends: preparation, receipts, retention, replay, and current authorization checks |
| `personal_room_lifecycle.py` | Personal-room command and membership policy, target-service routing, reconciliation, and separate rejoin/cleanup retention projections |
| `post_response_effects.py` | Shared post-response effects after Matrix delivery |
| `file_access.py` | Agent `file_access` resolution and the shared path authorization every path-taking tool opens files through |
| `orchestration/config_warnings.py` | Startup and reload warnings for risky but allowed config choices (foreign homeserver authorities, unconfined primary-process tools next to worker code tools) |
| `tool_approval.py` | Tool-call approval rule evaluation and public approval API |
| `approval_execution.py` | Agent reconstruction and exact-call execution for persisted native approval continuations |
| `approval_tools.py` | Recorded toolkit restoration and exact owner validation for saved approvals |
| `approval_response.py` | Response-side native approval continuation persistence, card publication, and terminal settlement |
| `approval_manager.py` | Matrix-backed tool approval runtime state |
| `oauth/credential_binding.py` | Canonical OAuth provider and worker-target bindings for browser workflows |
| `oauth/credential_lifecycle.py` | Single transaction owner for scoped OAuth load, refresh, callback publication, invalidation, and reset state |
| `oauth/credential_store.py` | Per-scope SQLite OAuth credential storage, revisions, and reset receipts |
| `oauth/reset.py` | OAuth reset target resolution and requester-bound browser intents |
| `oauth/reset_execution.py` | MCP retirement and durable reset execution |
| `custom_tools/oauth_connections.py` | Requester-bound agent tool for issuing OAuth reset confirmation links |
| `workspaces.py` | Agent workspace scaffolding, template seeding, and context file resolution |
| `worker_browser.py` | Serializes dedicated-worker headless browser calls, retains browser resources, and owns configuration/environment retirement and shutdown cleanup |
| `agents.py` | Agent creation and configuration |
| `config/` | Pydantic models for YAML config parsing (root model in `config/main.py`) |
| `config/personal_rooms.py` | Opt-in personal-room settings and validation for commands, aliases, and message templates |
| `routing.py` | Intelligent responder selection when no agent or team is mentioned |
| `routing_judgment.py` | Opt-in bounded System One responder selection, with explicit no-fit outcomes and existing LLM routing fallback |
| `teams.py` | Multi-agent collaboration (coordinate vs collaborate modes) |
| `agent_policy.py` | Canonical execution-policy derivation from authored agent config |
| `memory/` | Mem0 memory: agent and team-scoped |
| `file_memory_knowledge.py` | Shared resolution for agent file-memory semantic knowledge overlays |
| `memory_scope_ids.py` | Cycle-free canonical agent memory scope identifiers |
| `knowledge/` | Knowledge base / RAG file indexing with watcher |
| `knowledge/file_listing.py` | Which files belong to a knowledge base: include patterns, traversal, symlink-safe inclusion rules |
| `knowledge/collection_lifetime.py` | Compatible publication selection, shared reader locks, and exclusive collection reclamation |
| `knowledge/collections.py` | Chroma collection lifecycle for one knowledge base: naming, opening, probing, deleting, reclaiming |
| `knowledge/git_source.py` | The Git checkout a knowledge base indexes: clone, fetch, force-align, LFS hydration, credential injection |
| `knowledge/refresh_runner.py` | Dispatches one knowledge refresh: subprocess spawn, cancellation cleanup, publish and reconcile decisions |
| `knowledge/refresh_locks.py` | Process-wide refresh serialization (in-loop and cross-process source-root locks) and active-refresh bookkeeping |
| `tool_system/skills.py` | Skill integration system (OpenClaw-compatible) |
| `tool_system/plugins.py` | Plugin loading and tool/skill extension |
| `tool_system/google_workspaces.py` | Workspace-specific Google OAuth provider construction and tool registration |
| `scheduling.py` | Cron and natural-language task scheduling |
| `scheduling_executor.py` | Fire one scheduled task: hook emission, visible or silent Matrix delivery, and failure notices |
| `scheduled_run_records.py` | Agent-workspace JSON receipts for silent scheduled runs |
| `tools/` | 100+ tool integrations |
| `tools/lumalabs.py` | Configurable SDK model binding for both inherited Luma video-generation methods |
| `tool_system/dependencies.py` | Auto-install per-tool optional dependencies at runtime |
| `ai.py` | AI response generation, streaming, and Matrix run metadata |
| `model_loading.py` | Model instantiation and provider-specific loader selection |
| `model_catalog.py` | Allowlisted model metadata, Matrix icon upload/cache, and catalog revision |
| `model_catalog_receiver.py` | Router discovery admission, authenticated responses, and scope/lifetime checks |
| `model_selection.py` | Structured model request/result values and frozen acknowledgement metadata |
| `model_selection_scope.py` | Current joined membership and readable-root eligibility for model selection |
| `ai_runtime.py` | Agent-run input preparation and queued-notice hooks |
| `provider_media_fallback.py` | Provider-boundary inline-media retry and process-local capability learning per model route |
| `model_stream_output.py` | Shared policy for streamed output that makes provider retries unsafe |
| `agent_storage.py` | Agent session and learning SQLite storage helpers |
| `session_storage_preflight.py` | Required session-column checks and retained archives for incompatible owned session stores |
| `agent_descriptions.py` | Shared agent description rendering for delegation and orchestration |
| `credentials.py` | Unified credential management (CredentialsManager) |
| `matrix/` | Matrix protocol integration (client, users, rooms, presence, provisioning, message formatting) |
| `matrix/large_messages.py` | Large-message sidecar storage and retrieval for oversized Matrix payloads |
| `matrix/segmented_messages.py` | Lossless splitting of oversized text responses into ordered rich-text events (`defaults.large_message_strategy: split`) |
| `matrix/durable_ingestion.py` | Owned Nio batch validation, journal admission, and acknowledgement |
| `matrix/sync_continuity.py` | Durable pending join-fence persistence |
| `matrix/journal_ingress.py` | Typed event classification and replay parsing using Nio provenance |
| `matrix/message_content.py` | Canonical Matrix message content building for text, edits, and tool traces |
| `matrix/message_builder.py` | Message content building helpers |
| `matrix/provisioning.py` | Hosted provisioning client flow used for local pairing and server-side agent registration |
| `matrix/image_handler.py` | Image message download, decryption, and AI processing |
| `matrix/media.py` | Shared Matrix media encryption preparation, upload, download, and decryption helpers |
| `matrix/encrypted_file.py` | Dependency-free encrypted-file serialization shared by uploads, desktop, and runtime media |
| `matrix/room_cleanup.py` | Orphaned bot cleanup from rooms |
| `matrix/personal_rooms.py` | Target-agent service for eligible personal-room creation, ownership checks, invitations, and recoverable welcome delivery |
| `matrix/personal_room_store.py` | Durable per-requester room ownership, operator adoption attestations, welcome receipts, and cleanup retention |
| `matrix/event_info.py` | Event metadata parsing |
| `matrix/thread_membership.py` | Canonical Matrix thread identity and transitive relation membership |
| `matrix/identity.py` | Matrix ID parsing and utilities |
| `matrix/mentions.py` | Matrix mention formatting |
| `matrix/member_display_names.py` | Current member display names snapshotted from the synced nio room cache for model-facing `<msg>` tags |
| `matrix/typing.py` | Typing indicator utilities |
| `matrix/avatar.py` | Avatar management |
| `commands/` | Chat command parsing (`!help`, `!schedule`, `!config`, etc.) |
| `commands/config_commands.py` | Chat-based config commands (`!config`) |
| `commands/config_confirmation.py` | Interactive config confirmation workflows |
| `voice_handler.py` | Voice message download, transcription, mention normalization, and ASR cleanup |
| `tool_system/sandbox_proxy.py` | Container sandbox proxy for isolating shell/python tools |
| `shell_output_capture.py` | Bounded shell output spools, completion validation, and atomic output-file publication |
| `shell_execution.py` | Shell command execution core: spawning, output buffering, background handle registry |
| `shell_supervisor.py` | Worker-local shell supervisor process owning background shell handles across sandbox request subprocesses |
| `script_runs/legacy_recovery.py` | Exact pre-v2 recovery digest verification for script-runtime startup migration; the current store owns atomic replacement and unverifiable records remain rejected |
| `streaming.py` | Streaming state machine: placeholder, progressive edits, tool traces, cancellation |
| `prompts.py` | Built-in prompt defaults and prompt override registry |
| `attachments.py` | Attachment persistence, registration, and context-scoped resolution |
| `attachment_ids.py` | Leaf attachment-ID helpers kept free of matrix-client imports |
| `attachment_media.py` | Convert attachment records to Agno media objects |
| `media_inputs.py` | Shared media-input container passed across bot, teams, and AI layers |
| `api/` | FastAPI REST API (dashboard, credentials, OpenAI-compatible endpoint) |
| `api/usage_export.py` | Application-scoped usage-export preparation: one background scan, a bounded cache for daily/request-detail variants, committed-generation validation, and non-blocking shutdown cleanup |
| `custom_tools/` | Built-in custom tool implementations (gmail, calendar, scheduler, etc.) |
| `custom_tools/todo_state.py` | Leaf storage and actionability primitives for native per-thread todo state |
| `custom_tools/todo_poke.py` | Native scanner and background worker that wakes idle agents with actionable assigned todos |
| `thread_export/workspace_sync.py` | Always-on debounced runner that keeps `<workspace>/thread_exports/` current through the live bots' clients and journal principals |
| `background_tasks.py` | Background task management for non-blocking operations |
| `desktop/session.py` | Owns the desktop device's durable NIO session and storage binding |
| `desktop/transport.py` | Polls owned to-device work and acknowledges only after durable command admission |
| `desktop/command_journal.py` | Persists command admission, execution outcomes, and pending responses |
| `desktop/legacy_command_journal.py` | Validates historical JSON v1 receipts for the SQLite journal's one-time import |
| `desktop/bridge.py` | Enforces current local authority and coordinates serial execution and response delivery |
| `desktop/observations.py` | Bounds observation references by requester, agent, session, application, and age |
| `desktop/displays.py` | Maps verified logical display bounds to capture pixel scale |
| `desktop/input.py` | Defines the allowed application-local keyboard and scroll inputs |
| `desktop/macos_input.py` | Sends bounded Quartz pointer input in global logical coordinates |
| `desktop/macos_capture.py` | Captures verified windows and displays through ScreenCaptureKit |
| `desktop/native_config.py` | Validates and persists private native-helper configuration |
| `desktop/native_protocol.py` | Parses and bounds requests on the local NDJSON channel |
| `desktop/native_host.py` | Owns helper setup, runtime lifecycle, local control, and stdio dispatch |
| `desktop/native_entry.py` | Starts the packaged native desktop helper |
| `tool_system/events.py` | Tool-event formatting and metadata for Matrix messages |
| `tool_system/declarations.py` | Leaf tool metadata enums and dataclasses shared by implementations and the runtime catalog |
| `tool_system/registration.py` | Leaf built-in and plugin tool registration surface |
| `tool_system/metadata.py` | Runtime tool lookup, validation, plugin resolution, and instance construction |
| `tool_system/runtime_context.py` | Shared runtime ContextVar for tool calls (including attachment scope) |
| `tool_system/agno_compat_tool_hooks.py` | Private Agno hook-chain adapters installed by `tool_system/tool_hooks.py`; dispatch, approval, and cancellation ownership stay with the tool runtime |
| `agno_compat_*.py` and subsystem-local `agno_compat_*.py` | Agno SDK repairs and private bindings; see `docs/architecture/agno-compatibility.md` for the complete boundary inventory and owners |
| `constants.py` | Shared constants, paths, and environment variable defaults |
| `error_handling.py` | User-friendly error message extraction |
| `authorization.py` | Sender and per-agent authorization checks |
| `access_policy.py` | Resolve membership access config into immutable effective room and responder policies |
| `config/access.py` | Membership access configuration models (responder access, room defaults) |
| `config/legacy_access.py` | One-shot migration from retired access fields to the membership schema; delete with the retired fields |
| `thread_utils.py` | Thread analysis and agent detection |
| `session_ids.py` | Leaf helpers for the canonical persisted room/thread session ID |
| `thread_models.py` | Durable per-thread model overrides backing `!model` and the `thread_model` tool |
| `room_model_overrides.py` | Durable per-room runtime model defaults backing `!room_model` |
| `file_watcher.py` | File change detection for config hot-reload |
| `interactive.py` | Interactive Q&A system via Matrix reactions |
| `stop.py` | StopManager for cancelling in-progress responses |
| `topic_generator.py` | AI-generated room topics |
| `cli/main.py` | Main CLI entry point (Typer app) |
| `cli/banner.py` | CLI startup banner |
| `cli/config.py` | Config subcommand logic |
| `cli/connect.py` | `mindroom connect` pairing helpers and owner placeholder replacement |
| `cli/doctor.py` | Doctor command implementation |
| `cli/local_stack.py` | Local stack setup command |
| `credentials_sync.py` | Shared provider/bootstrap env to credentials sync |
| `logging_config.py` | Structured logging setup |
| `knowledge/utils.py` | Multi-knowledge-base vector DB utilities |

**Persistent state** lives under `mindroom_data/` by default (next to `config.yaml`, overridable via `MINDROOM_STORAGE_PATH`):
- `agents/*/sessions/` and `teams/*/sessions/` – SQLite event history for Agno conversations, optionally rooted at `MINDROOM_SESSION_STORAGE_PATH`
- `agents/*/learning/` – Per-agent Agno Learning data when learning is enabled
- `agents/*/chroma/` – Per-agent Mem0 ChromaDB storage
- `knowledge_db/` – Knowledge base vector stores for file-backed RAG
- `tracking/` – Durable handled-turn ledger plus exact callback obligations and compact terminal tombstones
- `credentials/` – JSON secrets synchronized from `.env`
- `encryption_keys/` – Matrix E2E encryption keys
- `sync_continuity/` – Crash-atomic pending join/decrypt fences
- `logs/` – Log files
- `matrix_state.yaml` – Matrix sync state

These agent paths describe ordinary shared agents; private agents use their resolved private state roots.
`MINDROOM_SESSION_STORAGE_PATH` relocates session storage only, leaving learning and memory at their agent state roots.

### SaaS Platform (`saas-platform/`)
- **Platform Backend**: Modular FastAPI app with routes in `saas-platform/platform-backend/src/backend/routes/`
- **Platform Frontend**: Next.js 16 with centralized API client in `saas-platform/platform-frontend/src/lib/api.ts`
- **Authentication**: SSO via HttpOnly cookies across subdomains
- **Deployment**: Kubernetes with Helm charts, dual-mode support (platform/standalone)
- **Database**: Supabase with comprehensive RLS policies

### Repo Layout

| Path | Purpose |
|------|---------|
| `src/mindroom/` | Core agent runtime (Matrix orchestrator, routing, memory, tools) |
| `frontend/` | Core MindRoom dashboard (Vite + React) |
| `saas-platform/platform-backend/` | SaaS control-plane API (FastAPI) |
| `saas-platform/platform-frontend/` | SaaS portal UI (Next.js 16) |
| `saas-platform/supabase/` | Supabase migrations, policies, seeds |
| `cluster/` | Terraform + Helm for hosted deployments |
| `local/` | Docker Compose helpers for local dev stacks |

### Ecosystem Repositories

MindRoom also maintains related repositories under `github.com/mindroom-ai`:
- `mindroom-element` - our Element fork for MindRoom message UX: collapsible tool-trace rendering, `!` command autocomplete synced from backend commands, long-text sidecar hydration, AI run metadata tooltip, and MindRoom branding/thread-first defaults. See `README.md` and `FORK_CHANGES.md` in that repo.
- `synapse` - our Synapse fork for MindRoom streaming workloads: optional compact-edit collapsing for superseded `m.replace` events across `/sync`, Sliding Sync, pagination, and context responses, plus `/versions` advertisement via `org.mindroom.compact_edits` and fork-owned Docker/CI flows. See `README.md` and `FORK_CHANGES.md`.
- `mindroom-librechat` - our LibreChat fork that parses MindRoom inline `<tool>` / `<tool-group>` tags into native `ToolCall` cards so tool execution stays server-side; also includes fork Docker CI and MindRoom-specific UX additions. See `README.md` and `.mindroom/` docs (`fork-context.md`, `tool-tag-rendering.md`).
- `mindroom-chat` - our AI-native Matrix client, built on Cinny and optimized for MindRoom agent workflows with MindRoom branding/default homeserver config, subpath deployment support (runtime/build base path for `/mindroom`), and thread/auth/sidebar UX refinements. See `README.md` and `FORK_CHANGES.md`.
- `mindroom-stack` - a full Docker Compose reference stack that boots the published MindRoom backend/frontend, a Tuwunel Matrix homeserver, and the MindRoom client together, including first-login and model/API-key setup guidance.

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
    access:
      current_room_members: false
      members_of_rooms: [lobby, dev]
      users: []
    credential_managers: []

defaults:
  tools: [scheduler]
  markdown: true
  enable_streaming: true
  large_message_strategy: sidecar

memory:
  backend: mem0

plugins: []

room_models: {}

bot_accounts: []

models:
  default:
    provider: anthropic
    id: claude-sonnet-5
  sonnet:
    provider: anthropic
    id: claude-sonnet-5

router:
  model: default

teams:
  super_team:
    display_name: Super Team
    role: Collaborative engineering assistant
    agents: [code]
    mode: collaborate

knowledge_bases:
  engineering_docs:
    path: ./knowledge_docs
    watch: true

voice:
  enabled: false
  stt:
    provider: openai
    model: gpt-transcribe

mindroom_user:
  username: mindroom_user
  display_name: MindRoomUser

administrators:
  - __MINDROOM_OWNER_USER_ID_FROM_PAIRING__

room_defaults:
  join_policy: invite
  listed: false
  encrypted: false
  invite_users:
    - __MINDROOM_OWNER_USER_ID_FROM_PAIRING__
  admins:
    - __MINDROOM_OWNER_USER_ID_FROM_PAIRING__

authorization:
  config_command_enabled: false
  aliases: {}

timezone: America/Los_Angeles
```

**Hot reloading**: `config.yaml` changes are watched at runtime. The orchestrator diffs configs, gracefully restarts affected agents, and rejoins rooms without bringing down the stack.

### Memory System

Mem0 memory (`src/mindroom/memory/functions.py`):
- **Agent memory** (`agent_<name>`) – Personal preferences, coding style, tasks
- **Team memory** – Shared context for team collaboration

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
- NEVER put imports in the function, unless it is to avoid circular imports or to keep a heavy import (for example a provider SDK) out of module import time. Prefer an explicit function-level `from x import Y` with `# noqa: PLC0415` over dynamic `import_module` indirection. Imports should be at the top of the file.
- `tests/test_import_graph.py` pins the complete third-party import surface of slim CLI, config, tool-registry, and sandbox entry points, and bans heavy optional provider, storage, and ML/data dependencies from the primary runtime. If it fails on your change, defer the new import to first use; extend the allowlist only when the dependency is genuinely needed at import time.
- Do not use `getattr()` or `hasattr()` to weaken a typed interface or probe for fields that the declared type should guarantee.
- If mocks or tests break, fix them to use proper typed objects or stricter mocks instead of adding dynamic attribute fallbacks in production code.
- **Merge and forget**: Code you touch should be polished enough to never revisit. Fix rough edges in code you're already changing.

### Refactor Policy

- Default to the smallest correct change.
- Never add a production branch, wrapper, or fallback whose only purpose is to preserve an old test expectation.
- Treat `src/mindroom/bot.py` and `src/mindroom/orchestrator.py` as composition roots and lifecycle shells, not feature implementation modules.
- Default to putting new behavior in focused modules or collaborators, then wire it into `bot.py` or `orchestrator.py` through a small public method or dependency.
- A PR that adds substantial code to `bot.py` or `orchestrator.py` must explain why the code truly belongs at that lifecycle boundary and why a focused module would be worse.
- During PR review, flag growth in `bot.py` or `orchestrator.py` as a design smell unless it is limited to routing, lifecycle coordination, dependency wiring, or calls into extracted collaborators.
- Every 3 review rounds, if reviews still show many issues or new major bug classes, stop patching and reconsider the design before another patch round.
- Use larger refactors when they provide clear immediate maintenance ROI, not hypothetical future value.
- A larger refactor is justified only if it:
  - Removes active duplication in current code paths.
  - Creates a clear source of truth without adding unnecessary abstraction layers.
  - Reduces net complexity (simpler call flow, fewer special cases).
  - Is covered by tests in the same PR.

### Migration Policy

Assume there are no active responses when migrations run.
Design migrations around that assumption rather than adding machinery to coordinate with active responses.

### Legacy Compatibility Policy

- Isolate substantive historical schemas and representations in `legacy_<subject>.py` beside their current owner.
  This includes one-time migrations, recurring old-data readers, and diagnostics for retired fields.
- Keep current-format processing, transactions, locking, validation, authorization, and retries with the current storage or lifecycle owner.
  Small field defaults can stay with their model when extraction would only add indirection.
- Each substantive legacy rule must have a nearby source comment whose first line is `# LEGACY_COMPAT: <short description of the legacy format>`.
  Use one marker per documented rule, including small defaults and compatibility notes kept beside current owners, so `rg -n -F 'LEGACY_COMPAT:' --glob '*.py'` lists them across the repository.
  Follow the marker with these fields:
  - `Legacy format`: the old representation and the condition that selects this rule.
  - `Last legacy release`: the last stable MindRoom release whose native writer or typed model emitted that representation, plus the replacement release and format.
  - `Handling`: what the current reader, migration, or rejection does and which guarantees it preserves.
  - `Coverage`: repository-relative regression test paths, preferably exact test node IDs.
- Verify release provenance from history and tags.
  The last release that accepted old data is not the last native writer release.
  State unreleased, unversioned external input, no tagged native model, or schema-based recovery explicitly when no single release cutoff exists.
- Keep the boundary map in `docs/architecture/migrations.md` aligned with changes; source comments own the exact provenance and test references.

### Agno Compatibility Policy

- The goal is to identify Agno weaknesses, contribute focused upstream fixes or extension points, and remove local workarounds as those changes ship.
  Judge an extraction by whether it makes that upstream work easier to understand, test, and retire.
- Isolate substantive Agno monkey patches, copied SDK internals, private-API adapters, and upstream bug workarounds in `agno_compat_<subject>.py` beside their owning module.
  Ordinary public-API usage and MindRoom's orchestration, approval, history, and storage policies remain with their current owners.
  Tiny overrides may stay in a cohesive adapter when extraction would only add indirection, but require the same source comment.
- Each distinct workaround must have a nearby source comment whose first line is `# AGNO_COMPAT: <short description of the upstream weakness>`.
  Use one marker per independently removable workaround, including tiny overrides kept beside their owners, so `rg -n -F 'AGNO_COMPAT:' src/mindroom` lists each gap with a useful summary.
  Follow the marker with these fields:
  - `Reason`: the concrete upstream behavior or missing extension point and its effect on MindRoom.
  - `Upstream issue`: a verified issue URL, or an explicit tracking gap and why the boundary is needed.
  - `Upstream PR`: a verified fix or API proposal URL when one exists; state when none is identified or the linked PR covers only part of the workaround.
  - `Remove when`: the exact upstream behavior that allows removal, including any MindRoom behavior that must remain.
  - `Coverage`: repository-relative regression test paths, preferably exact test node IDs.
- Distinguish upstream bugs from missing public extension points and intentional application policy.
  Never invent a tracking link or treat a related PR as a complete fix.
  Separate removal conditions when one module handles multiple upstream gaps.
- Treat an explicit tracking gap as unfinished upstream work.
  Record the concrete failing behavior or required extension point in the inventory, search for existing tracking before opening a new item, and replace the gap with verified links when available.
- For an upstream contribution, reduce the problem to an Agno-only reproducer and regression test where possible.
  Keep MindRoom-specific policy out of the proposed fix and retain local integration coverage for the behavior MindRoom requires.
- Keep patch installation explicit and idempotent, preserve optional-import boundaries, and retain version guards where private signatures or semantics require them.
- Keep the weakness-to-upstream map and boundary inventory in `docs/architecture/agno-compatibility.md` aligned with contributions, extractions, and removals; source comments own exact upstream tracking and test references.
- On each Agno upgrade, inspect these boundaries, verify which fixes the pinned release includes, and run their behavioral tests.
  Remove a workaround only when the relevant tests pass without it; a merged PR alone is insufficient.
  Retain regression coverage for behavior MindRoom still requires and update Tach boundaries with any extraction or removal.

### Security Trust Model

A worker container (worker routing through the sandbox proxy) is the only security boundary between an agent and the MindRoom runtime.
Check every reported vulnerability and every proposed hardening change against this model before implementing it, and decline changes that contradict it.
The full model, the `file_access` setting, and the list of intentional behaviors reviewers must not "fix" live in `docs/architecture/security-posture.md`; read it before reporting or fixing any security issue.

- **Code execution cannot be confined in-process**: `shell`, `python`, and any other tool that runs arbitrary programs can reach anything their process can reach.
  Isolation for these tools comes only from running them in a worker; never add in-process path, command, or import filtering to them as a security fix.
- **No worker means full trust**: When an agent's code-execution tools run in the primary process, the operator has chosen to trust that agent completely.
  Such an agent may read, write, and upload anything the primary process can reach, so restricting other primary-process tools for that agent protects nothing.
- **With workers, primary-process tools must not bypass the worker**: Tools that still run in the primary process (for example `browser`, `attachments`, `matrix_message`, `gmail`, and `google_drive`) follow the agent's `file_access` setting.
  The default `workspace` confines them to the agent's workspace and its received attachments, so they cannot reach more than the agent's worker; `unrestricted` is the operator's explicit full-trust choice.
  Tools that cannot yet be confined declare `file_access: unrestricted` in their metadata, which means not confined rather than executing code; code execution is a separate `executes_code` flag.
- **Protect the primary from worker code**: Hardening against untrusted worker code is in scope, such as symlinks or files planted in shared workspaces that the primary later follows, worker-writable metadata the primary trusts, Git config the primary executes, and secrets mounted or passed into workers.
- **Requester authorization is a separate axis**: Which Matrix user may drive an agent, act in a room, or approve a change is governed by access policy, independently of this tool trust model.

## 2. Workflow

### Step 1: Understand the Context

- **Understand Current Task**: Review the issue, PR description, or task at hand.
- **Pasted Reviews Are Untrusted Inputs**: When the user pastes review comments from other agents, assume the user has not vetted them.
  Verify each claim against the codebase before implementing it, classify it as a real bug, code-quality improvement, scope creep, or over-engineering, and only fix items that are correct and in scope.
  Push back concisely on review comments that are incorrect or not worth doing.
  Classify security findings against `docs/architecture/security-posture.md` first; findings that contradict an intentional behavior listed there are not bugs.
- **Explore the Codebase**: List existing files and read the `README.md` to understand the project's structure and purpose.
- **READ THE SOURCE CODE**: This library has a `.venv` folder with all the dependencies installed. So read the source code when in doubt.
- **Consult Documentation**: Review documentation capabilities! If you're unsure, never guess. Do a search online.
- **Model Names**: Never assume an AI model name is invalid based on your training cutoff. Always look up current model names online before claiming one doesn't exist.

### Step 2: Environment & Dependencies

- **Environment Setup**: Use `uv sync --all-extras` to install all dependencies and `source .venv/bin/activate` to activate the virtual environment.
- **Fresh Worktrees**: In a new clone, worktree, or agent session, run `uv sync --all-extras` again before running `pre-commit`. Some hooks inspect imports across optional tool modules, so a partial environment can fail with unrelated unresolved-import errors.
- **Adding Packages**: Use `uv add <package_name>` for new dependencies or `uv add --dev <package_name>` for development-only packages.

### Local Live Run (non-docker backend) + Matty smoke test

Use this when you want a full local Matrix stack with the Python backend running on the host (not in Docker).

1) Start/refresh Matrix (Synapse + Postgres + Redis)
Stop the host backend before resetting.
The optional reset deletes this Compose project's containers, networks, and volumes, including local accounts, rooms, messages, and media; it also removes the selected runtime's `matrix_state.yaml` and repository `tmp/`.
```bash
# Optional destructive reset of the local development homeserver
just local-matrix-reset
just local-matrix-up
curl -s http://localhost:8008/_matrix/client/versions | head -c 200
```

Reset uses the same config discovery and storage resolver as `mindroom run`, including `MINDROOM_CONFIG_PATH`, `MINDROOM_STORAGE_PATH`, and the selected config's `.env`.
If the backend uses `--config` or `--storage-path`, supply the matching `MINDROOM_CONFIG_PATH` or `MINDROOM_STORAGE_PATH` when resetting.
Run these commands from the repository root; relative environment paths use that directory, while a relative storage path in `.env` uses the selected config directory.

2) If you see login errors (M_FORBIDDEN) or changed homeserver, stop the backend and clear the selected runtime's Matrix state
```bash
uv run python -c "from mindroom.constants import matrix_state_file, resolve_runtime_paths; matrix_state_file(resolve_runtime_paths()).unlink(missing_ok=True)"
```
This uses the same environment selection as the reset; a hardcoded `mindroom_data/matrix_state.yaml` only covers default storage beside the repository's config.

3) Ensure local OpenAI-compatible server is running on port 9292
```bash
curl -s http://localhost:9292/v1/models | head -c 200
```

4) Configure `config.yaml` to use the local OpenAI-compatible server
- Set relevant models to `provider: openai`
- Use a model ID that exists on the local server (e.g., `gpt-oss-low:20b`)
- Add `extra_kwargs.base_url: http://localhost:9292/v1` for those models
- For memory, prefer `provider: openai` and an embedding model that exists (e.g., `embeddinggemma:300m`)

5) Run the backend with explicit env overrides (use Python 3.13; production Dockerfile also uses 3.13)
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
uv run --python 3.13 matty send "Lobby" "Hello @general please reply with pong."

MATRIX_HOMESERVER=http://localhost:8008 MATRIX_SSL_VERIFY=false \
uv run --python 3.13 matty threads "Lobby"

MATRIX_HOMESERVER=http://localhost:8008 MATRIX_SSL_VERIFY=false \
uv run --python 3.13 matty thread "Lobby" t1
```

### Hosted Matrix Run (`uvx`) + Pairing

Use this when Matrix + chat UI are hosted and only the MindRoom backend runs locally.

1) Initialize local config with hosted defaults
```bash
uvx mindroom config init --matrix-server mindroom.chat
```

2) Add at least one model provider key in `~/.mindroom/.env` (for example `ANTHROPIC_API_KEY` or `OPENAI_API_KEY`)

3) Generate a pair code in `https://chat.mindroom.chat` (`Settings -> Local MindRoom`) and pair locally
```bash
uvx mindroom connect --pair-code ABCD-EFGH
```

4) Start MindRoom
```bash
uvx mindroom run
```

`mindroom connect` writes `MINDROOM_LOCAL_CLIENT_ID` and `MINDROOM_LOCAL_CLIENT_SECRET` to `~/.mindroom/.env` by default (unless `--no-persist-env` is used) and auto-replaces owner placeholder tokens in `config.yaml` and every file it pulls in via `!include` when `owner_user_id` is returned.

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
Run these commands from the repository root.
For staging, copy the example values and fill in the Supabase, Stripe, and provisioner credentials before running Helm.
This chart-managed Secret workflow also stores credentials in Helm release history; restrict access to the release Secrets as well as the populated values file.
See [Platform Deployment](docs/deployment/kubernetes.md#platform-deployment) for credential retention and the existing external-Secret option.
The `domain` value selects ingress hosts; the namespace alone does not select staging domains.
For a fresh staging install, store Helm release records in `staging`; the chart creates application resources in `mindroom-staging`, matching the Terraform namespace layout.
For an existing release, retain its original release name and namespace.

```bash
# Set kubeconfig path
export KUBECONFIG=./cluster/terraform/terraform-k8s/mindroom-k8s_kubeconfig.yaml

# Prepare staging values (keep the populated file private)
cp cluster/k8s/platform/values-staging.example.yaml cluster/k8s/platform/values-staging.yaml
# Fill in credentials before deploying
helm upgrade --install platform ./cluster/k8s/platform -f cluster/k8s/platform/values-staging.yaml --namespace staging --create-namespace

# Create customer instances through the portal or authenticated POST /my/instances/provision.
# See docs/deployment/kubernetes.md for the customer and operator API flows.
# The CLI provision <id> command sends fixed test metadata; use only with existing test fixtures.

# The provisioner:
# - Creates new database records or updates an existing instance
# - Manages secrets securely
# - Deploys via Helm with proper values
# - Tracks status

# Manual Helm deployment (debugging only, not for production):
# helm upgrade --install instance-1 ./cluster/k8s/instance \
#   --namespace mindroom-instances \
#   -f values-with-secrets.yaml  # Never commit this file!

# Quick redeploy of MindRoom backend (updates all instances)
./saas-platform/redeploy-mindroom.sh

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
- **Preserve Review History**: Do not amend commits or force-push PR branches unless the user explicitly asks for it. Prefer follow-up commits so PR history stays inspectable.
- **Targeted Adds**: **NEVER** use `git add .`. Always add files individually (`git add <filename>`) to prevent committing unrelated changes.

### Step 4: Testing & Quality

- **Test Before Committing**: **NEVER** claim a task is complete without running `pytest` to ensure all tests pass.
- **Test at the Owning Seam**: New behavior tests target the collaborator seam that owns the behavior (`ResponseRunner`, `DeliveryGateway`, `TurnPolicy`, `TurnController`, `CoalescingGate`, `IngressValidator`, `EditRegenerator`), not the `AgentBot`/`TeamBot` facade.
  Fake collaborators through the conftest seam installers (`install_generate_response_mock`, `install_send_response_mock`, `replace_edit_regenerator_deps`) or the `*Deps` dataclasses, never by assigning mocks onto bot attributes.
  A test needing more than 3 patches is a smell that it is testing through the wrong seam.
  Genuinely end-to-end tests belong in the slim integration files (`test_multi_agent_bot.py`, `test_threading_error.py`), which stay small by design.
- **Run Pre-commit Hooks**: After `uv sync --all-extras`, run `uv run pre-commit run --all-files` before committing to enforce code style and quality.
- **Update Tach Boundaries in the Same PR**: If your PR changes a Tach-governed boundary, update `tach.toml` in the same PR, follow the guidance in the comment at the top of that file, and run `uv run tach check --dependencies --interfaces`.
- **Handle Linter Issues**:
  - **False Positives**: The linter may incorrectly flag issues in `pyproject.toml`; these can be ignored.
  - **Test-Related Errors**: If a pre-commit fix breaks a test (e.g., by removing an unused but necessary fixture), suppress the warning with a `# noqa: <error_code>` comment.

### Step 5: Refactoring

- **Be Proactive**: Continuously look for opportunities to refactor and improve the codebase for better organization and readability.
- **Incremental Changes**: Refactor in small, testable steps. Run tests after each change and commit on success.

### Step 6: Viewing the Widget

- **Taking Screenshots**: With the bundled dashboard running at `http://localhost:8765`, use `uv run python frontend/take_screenshot.py` from the project root.
- **Manual Screenshot**: From the frontend directory, run `bun run dev` to start the development server, then run `DEMO_URL=http://localhost:3003 bun run screenshot` in another terminal.
  Replace `3003` with the configured `FRONTEND_PORT` when using a different development port.
- **Screenshot Location**: Screenshots are saved to `frontend/screenshots/` with timestamps.
- **Use Cases**: This is helpful for visual verification, documentation, and sharing the dashboard appearance.

### Developer Automation (`justfile`)

Common `just` recipes for development:
```bash
# Local stacks
just local-matrix-up              # Boot Synapse + Postgres dev stack
just local-platform-compose-up    # Full SaaS sandbox

# Testing (IMPORTANT: enter the Node.js 24 `nix-shell shell.nix` first on NixOS hosts)
# Recipe regression tests require just; shell.nix and CI install it.
# If `uv run pytest` fails with 'module mindroom has no attribute bot',
# use the repo dev shell so `libstdc++.so.6` is available:
nix-shell shell.nix
# Then run commands normally inside the shell, for example:
uv run pytest tests/<file>.py -x -n 0 --no-cov -v
# If `<nixpkgs>` is unresolved, use:
nix-shell -I nixpkgs=/nix/var/nix/profiles/per-user/root/channels/nixos shell.nix
# Or if the above works, you can also use:
just test-backend                 # Run pytest for core (may need nix-shell wrapper)
just test-saas-backend            # Run pytest for SaaS backend

# Deployment
just cluster-helm-template        # Render platform chart manifests
just cluster-helm-lint            # Lint platform chart
```

## 3. Critical "Don'ts"

- **DO NOT** manually edit the CLI help messages in `README.md`. They are auto-generated.
- **NEVER** use `git add .`.
- **NEVER** claim a task is done without passing all `pytest` tests.
- **NEVER** run `sleep <guessed duration>` in a Bash tool call to wait for something.
  This rule is about how you drive your own terminal, not about MindRoom's runtime code: `asyncio.sleep` in `src/` is unaffected.
  A guessed duration is always wrong: too short and you read a half-written result, too long and you burn wall-clock doing nothing.
  Poll the real condition instead (process exit, a line in the output file, an HTTP health check, a file appearing), or start the command in the background and react to its completion notification.

## 4. Interacting with MindRoom Agents via Matty CLI

### Overview
Matty is a Matrix CLI client that allows you to interact with MindRoom AI agents. Use it to send messages and observe agent responses during development and testing.

### Prerequisites
```bash
# Matty is optional and is not installed by `uv sync --all-extras`
# Activate an environment where Matty is installed
source .venv/bin/activate
# If Matty is unavailable, use the authenticated raw Matrix client API
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
matty send "room_name" "Hello @assistant!"

# Multiple agent mentions
matty send "room_name" "@research @analyst analyze this topic"
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
matty thread-reply "room_name" t1 "@assistant continue"
```

### Typical Agent Testing Workflow
```bash
# 1. Find the test room
matty rooms

# 2. Send a message mentioning agents
matty send "test_room" "@assistant What can you do?"

# 3. Check for agent response (agents respond in threads)
matty threads "test_room"
matty thread "test_room" t1  # View the thread where agent responded

# 4. Continue conversation in thread
matty thread-reply "test_room" t1 "@research find information about X"
```

### Important Notes
- **Agents respond in threads**: Always check threads after sending messages
- **Use @mentions**: Tag agents with @ to get their attention
- **Message handles**: Use m1, m2, m3 to reference messages
- **Thread IDs**: Use t1, t2, t3 to reference threads (persistent across sessions)
- **Output formats**: Add `--format json` for machine-readable output
- **Streaming responses**: Agents stream responses by editing messages, which may take 10+ seconds to complete.
  Inspect the latest `io.mindroom.stream_status` in a client or event view that exposes it: `pending` and `streaming` indicate progress, and `completed` confirms successful completion.
  Record `cancelled` or `error` as terminal outcomes; body ellipses are not a completion signal.

## 5. Quick Reference

```bash
# Preflight check before running
mindroom doctor

# Run the stack
uv run mindroom run --storage-path mindroom_data

# Pair local install with hosted provisioning
mindroom connect --pair-code ABCD-EFGH

# Bootstrap local Synapse + MindRoom Chat (Docker)
mindroom local-stack-setup --synapse-dir /path/to/mindroom-stack/local/matrix

# Update credentials
# Edit .env for provider/bootstrap keys and restart; configure tool credentials through the dashboard or persisted tool config

# Discover commands
# Send !help from any bridged room

# Debug logging
mindroom run --log-level DEBUG  # Surface routing decisions, tool calls, config reloads
```

Inspect agent traces under `<session-storage-root>/agents/<agent>/sessions/<agent>.db`, where the session storage root is `MINDROOM_SESSION_STORAGE_PATH` when set and `MINDROOM_STORAGE_PATH` otherwise.

## 6. Releases

Pushes to `main` run `.github/workflows/calver-auto-release.yml`, except pushes that only change `macos/appcast.xml`.
The workflow serializes CalVer release creation and dispatches publishers only after confirming that the release tag points to the run's commit.
It dispatches these workflows from `main`, passing the release tag as `release_ref`:

- `build-mindroom.yml`: MindRoom container images.
- `build-platform.yml`: Platform container images.
- `publish-helm-charts.yml`: Helm charts.
- `release.yml`: Python package and macOS desktop artifacts.

Check the release workflow and publisher runs before retrying a failed publication.
To retry one publisher for an existing intended release tag, use its workflow filename and pass that same tag as `release_ref`:

```bash
gh workflow run build-mindroom.yml --ref main --field release_ref='<existing-release-tag>'
```

Replace the example workflow with the publisher that needs recovery and the placeholder with the existing release tag.

# Important Instruction Reminders
Do what has been asked; nothing more, nothing less.
NEVER create files unless they're absolutely necessary for achieving your goal.
ALWAYS prefer editing an existing file to creating a new one.
