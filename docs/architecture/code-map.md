# Code Map

Where things live in `src/mindroom/`, for locating code.
When you add, rename, or remove a key module, update its row in the same PR.

## Inbound Turn Pipeline

The path from a Matrix message to a delivered response; [Bot Runtime](bot-runtime.md) describes it in detail.

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

## Modules

| Module | Purpose |
|--------|---------|
| `bounded_bytes.py` | Shared asynchronous and synchronous byte collection that rejects overflowing chunks before buffering them |
| `atomic_file.py` | Shared atomic byte publication and cleanup relative to an opened directory |
| `orchestrator.py` | MultiAgentOrchestrator - boots agents, manages sync loops, hot-reload |
| `orchestration/` | Extracted orchestrator helpers (config update plans, plugin watch, rooms, runtime) |
| `orchestration/config_lifecycle.py` | Debounced config-reload lifecycle: queueing, response drain, and update-plan dispatch |
| `config_bundle.py` | Native staged bundle validation, drift protection, directory publication, and recovery journals |
| `cli/config_bundle.py` | Bundle install, change-classification, and runtime-confirmed apply/rollback command adapters, plus initialize-only runtime bootstrap |
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
| `config/skill_learning.py` | Opt-in agent settings for skill reviews: interval, review model, notices, and archival |
| `dispatch_replay_guard.py` | Replay-guard checks for dispatch sequencing |
| `event_journal/` | Durable ownership of admitted Matrix events, conversation projection, and delivery outbox |
| `response_sources.py` | Immutable response-attempt source identity shared by runtime and persistence boundaries |
| `event_journal/response_attempts.py` | Normalized durable response ownership registration, binding, and exact lookup queries |
| `event_journal/legacy_response_attempts.py` | One-time transactional adoption of released response ownership snapshots |
| `event_journal/scheduled_approvals.py` | Stored scheduled tool calls and their one-shot approvals: binding, fire-time arming, withdrawal, claim with its receipt, and outcome |
| `journal_dispatch.py` | Fan admitted journal events out to typed Matrix callbacks and settle the ones that finish |
| `pending_event_worker.py` | Decides when pending journal work runs, and wakes itself again whenever a pass stops early |
| `command_turn_executor.py` | Command execution and durable command/config mutation journals |
| `reaction_dispatch.py` | Durable semantic routing for Matrix reactions |
| `user_stop_reconciliation.py` | STOP ordering, response cancellation, and terminal turn reconciliation |
| `visible_response_reconciliation.py` | Visible Matrix response recovery, adoption, and replay reconciliation |
| `turn_store.py` | Unified durable turn access (wraps the handled-turn ledger) |
| `handled_turns.py` | Disk-backed handled-turn ledger preventing duplicate responses |
| `sync_restart_retry.py` | Whether an edit regeneration of an already committed revision may run again, decided from persisted history |
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
| `minimal_agent.py` | Same live Agent with one provider-facing Bash tool and hidden canonical tool preparation |
| `cli_shell_agent.py` | Standard agents whose native shell commands call their other tools through `mindroom-agent` |
| `agent_cli/` | Response-owned CLI grants, call admission, shell access, discovery, and result projection |
| `agent_modes.py` | Conversation-scoped standard/minimal selection persistence |
| `minimal_mode_preflight.py` | Minimal-mode eligibility for `!mode` and minimal subagents, reported as one actionable checklist |
| `commands/mode_commands.py` | Authorized agent mode selection with canonical session scope and deployment preflight |
| `api/agent_cli.py` | Authenticated transport for response-owned CLI operations and live call receipts |
| `cli_approval_recovery.py` | Exact saved CLI approval execution through rebuilt canonical bindings and ordinary interrupted-response recovery |
| `cli_approval_waits.py` | Response-owned CLI approval waits, exact journal claims, and terminal cleanup |
| `tool_system/agent_tool_calls.py` | Prepared live-Agent catalog and serialized native execution of qualified tools |
| `tool_system/tool_access.py` | Shared qualified tool identities, discovery, schemas, and local argument validation |
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
| `tool_system/skill_usage.py` | Workspace skill usage records the skill learner's archival reads as an inactivity clock |
| `tool_system/plugins.py` | Plugin loading and tool/skill extension |
| `tool_system/google_workspaces.py` | Workspace-specific Google OAuth provider construction and tool registration |
| `tool_system/atlassian_connections.py` | Additional Atlassian Cloud connection providers and prefixed tool registration |
| `scheduling.py` | Cron and natural-language task scheduling |
| `scheduled_tool_calls.py` | Resolving a scheduled call on the agent's own live tools and running it once by task ID with its approval |
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
| `skill_learning/capture.py` | The final model request of a counting response, kept for the review to fork |
| `skill_learning/runner.py` | In-memory reply counts per conversation; starts a review when a count reaches the interval, stops it when a new response starts, and posts change notices |
| `skill_learning/reviewer.py` | One bounded skill review: a fork of the response's final request with its tools unchanged, or a redacted digest replay when the request cannot be forked or another review model is set |
| `skill_learning/tools.py` | Skill tools shared by chat and the review: ownership, read-before-write, and landed-change tracking |
| `skill_learning/transcript.py` | Reply counting and the digest a replayed review reads: older turns shortened plus the newest messages verbatim |
| `skill_learning/library.py` | Confined workspace skill writes, ownership provenance, history snapshots, and archival |
| `automations/runner.py` | Built-in automation schedule loop: cron timing, the visible hook-dispatched prompt, and the verify step after its run |
| `automations/prompt_curation.py` | The `prompt_curation` automation: size check over always-loaded files, the bounded prompt, and the verify that asks for a re-check |
| `config/automations.py` | Built-in automation settings and validation |
| `custom_tools/skill_manage.py` | Chat-time `skill_manage`, like Hermes' foreground tool, for agents that list it or learn skills |
| `session_storage_preflight.py` | Required session-column checks and retained archives for incompatible owned session stores |
| `agent_descriptions.py` | Shared agent description rendering for delegation and orchestration |
| `credentials.py` | Unified credential management (CredentialsManager) |
| `matrix/` | Matrix protocol integration (client, users, rooms, presence, provisioning, message formatting) |
| `matrix/large_messages.py` | Large-message sidecar storage and retrieval for oversized Matrix payloads |
| `matrix/segmented_messages.py` | Lossless splitting of oversized text responses into ordered rich-text events (`defaults.large_message_strategy: split`) |
| `matrix/durable_ingestion.py` | Owned Nio batch validation, journal admission, and acknowledgement |
| `matrix/sync_continuity.py` | Durable pending join-fence persistence |
| `matrix/journal_ingress.py` | Typed event classification and replay parsing using Nio provenance |
| `matrix/message_content.py` | Canonical extraction and sidecar resolution for received Matrix messages and edits |
| `matrix/message_builder.py` | Message content building helpers |
| `matrix/provisioning.py` | Hosted provisioning client flow used for local pairing and server-side agent registration |
| `matrix/provisioning_heartbeat.py` | Best-effort startup and periodic "last seen" heartbeat from paired installs to the hosted provisioning service |
| `matrix/provisioning_env.py` | Slim environment readers deciding whether a hosted install registers by token, shared secret, or pairing, plus the paired-install client-credential headers (no Matrix/HTTP imports) |
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
| `api/sandbox_request_cancellation.py` | Stops an in-flight sandbox runner request when the primary stops waiting for it |
| `shell_output_capture.py` | Bounded shell output spools, completion validation, and atomic output-file publication |
| `shell_execution.py` | Shell command execution core: spawning, output buffering, background handle registry |
| `shell_supervisor.py` | Worker-local shell supervisor process owning background shell handles across sandbox request subprocesses |
| `streaming.py` | Streaming state machine: placeholder, progressive edits, tool traces, cancellation |
| `prompts.py` | Built-in prompt defaults and prompt override registry |
| `attachments.py` | Attachment persistence, registration, and context-scoped resolution |
| `attachment_ids.py` | Leaf attachment-ID helpers kept free of matrix-client imports |
| `attachment_media.py` | Convert attachment records to Agno media objects |
| `media_inputs.py` | Shared media-input container passed across bot, teams, and AI layers |
| `api/` | FastAPI REST API (dashboard, credentials, OpenAI-compatible endpoint) |
| `api/open_access.py` | Host allow-list and browser-origin guard for requests served without a credential (open dashboard auth, unauthenticated `/v1`) |
| `api/request_body_limit.py` | Pure ASGI middleware answering 413 for dashboard API request bodies over 16 MiB, except knowledge uploads |
| `api/usage_export.py` | Application-scoped usage-export preparation: one background scan, a bounded cache for daily/request-detail variants, committed-generation validation, and non-blocking shutdown cleanup |
| `custom_tools/` | Built-in custom tool implementations (gmail, calendar, scheduler, etc.) |
| `custom_tools/todo_state.py` | Leaf storage and actionability primitives for native per-thread todo state |
| `custom_tools/todo_poke.py` | Native scanner and background worker that wakes idle agents with actionable assigned todos |
| `custom_tools/todo_template_render.py` | Full-Jinja rendering of workspace todo templates in a short-lived, memory- and CPU-limited child process |
| `custom_tools/calculator.py` | Agno calculator with bounded `factorial()` and `is_prime()` arguments |
| `custom_tools/sleep.py` | Agno sleep toolkit with pauses capped at 300 seconds |
| `thread_export/workspace_sync.py` | Always-on debounced runner that keeps `<workspace>/thread_exports/` current through the live bots' clients and journal principals |
| `background_tasks.py` | Background task management for non-blocking operations |
| `desktop/session.py` | Owns the desktop device's durable NIO session and storage binding |
| `desktop/transport.py` | Polls owned to-device work and acknowledges only after durable command admission |
| `desktop/command_journal.py` | Persists command admission, execution outcomes, and pending responses |
| `desktop/legacy_command_journal.py` | Validates historical JSON v1 receipts for the SQLite journal's one-time import |
| `desktop/bridge.py` | Enforces current local authority, routes app input, folder, and shell actions to their owners, runs browser actions, app launch, and app observation, and coordinates serial execution and response delivery |
| `desktop/command_parameters.py` | Parses the typed, length-bounded parameters of desktop commands |
| `desktop/reply_fitting.py` | Builds desktop success replies and fits trimmed replies within one encrypted to-device message |
| `desktop/file_actions.py` | Runs desktop folder listings and reads and fits them into one reply |
| `desktop/gui_actions.py` | Runs desktop app input actions (semantic element actions, pointer, text, scroll, and key chords) through the local GUI provider |
| `desktop/shell_actions.py` | Runs desktop shell actions, reports local and caller-scoped shell status, and delivers output inline or as an encrypted attachment |
| `desktop/bridge_components.py` | Builds the local capability providers and bridge for one Desktop run, shared by the app helper and the terminal |
| `desktop/filesystem.py` | Bounded, descriptor-confined reads from explicitly selected local folders |
| `desktop/shell.py` | Runs locally approved desktop shell commands through MindRoom's shell engine |
| `desktop/login_environment.py` | Captures the account's login-shell environment once for locally approved desktop commands |
| `desktop/shell_prompt.py` | Local terminal approval for shell commands requested through a terminal-owned Desktop bridge |
| `desktop/observations.py` | Bounds observation references by requester, agent, session, application, and age |
| `desktop/displays.py` | Maps verified logical display bounds to capture pixel scale |
| `desktop/input.py` | Defines the allowed application-local keyboard and scroll inputs |
| `desktop/macos_input.py` | Sends bounded Quartz pointer input in global logical coordinates |
| `desktop/macos_capture.py` | Captures verified windows and displays through ScreenCaptureKit |
| `desktop/native_config.py` | Validates and persists private native-helper configuration |
| `desktop/native_protocol.py` | Parses and bounds requests on the local NDJSON channel |
| `desktop/native_host.py` | Owns helper setup, runtime lifecycle, local control, and stdio dispatch |
| `desktop/local_dashboard.py` | Validates the loopback dashboard URL and provides its credential through the private native-host pipe |
| `desktop/startup_errors.py` | Translates desktop startup failures into actionable protocol errors and recovery advice |
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
| `debug_report.py` | Read-only collection of what the backend stored about one reported conversation: event journal, Agno runs, tool-call and LLM request logs, and log lines |
| `cli/main.py` | Main CLI entry point (Typer app) |
| `cli/banner.py` | CLI startup banner |
| `cli/config.py` | Config subcommand logic |
| `cli/connect.py` | `mindroom connect` pairing helpers and owner placeholder replacement |
| `cli/debug_report.py` | `mindroom debug-report` command: bug report parsing, config-only storage location, and JSON output |
| `cli/doctor.py` | Doctor command implementation |
| `cli/local_stack.py` | Local stack setup command |
| `credentials_sync.py` | Shared provider/bootstrap env to credentials sync |
| `logging_config.py` | Structured logging setup |
| `knowledge/utils.py` | Multi-knowledge-base vector DB utilities |
| `custom_tools/chat_ui.py` | Runtime-bound MindRoom Chat UI action requests with canonical Matrix conversation and sender identity |
| `tools/chat_ui.py` | Tool-catalog registration and discovery metadata for Chat UI actions |
| `visible_voice_echo.py` | Immediate router voice-placeholder delivery, replacement ordering, and deduplication |
| `avatar_generation.py` | Generates and manages avatar assets for agents, rooms, and spaces |

## Persistent State

Persistent state lives under `mindroom_data/` by default (next to `config.yaml`, overridable via `MINDROOM_STORAGE_PATH`):

- `agents/*/sessions/` and `teams/*/sessions/` – SQLite event history for Agno conversations, such as an agent's traces in `agents/<agent>/sessions/<agent>.db`, optionally rooted at `MINDROOM_SESSION_STORAGE_PATH`
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
