# Architecture

MindRoom's architecture consists of several key components working together.

## Overview

```
┌─────────────────────────────────────────────────────────┐
│                   Matrix Homeserver                      │
│              (Synapse, Conduit, etc.)                    │
└──────────────────────┬──────────────────────────────────┘
                       │
┌──────────────────────▼──────────────────────────────────┐
│              MultiAgentOrchestrator                      │
│  ┌─────────────────────────────────────────────────┐    │
│  │                   Matrix Client                  │    │
│  │         (nio, sync loops, presence)             │    │
│  └─────────────────────────────────────────────────┘    │
│                                                          │
│  ┌─────────┐  ┌─────────┐  ┌─────────┐  ┌─────────┐    │
│  │ Router  │  │ Agent 1 │  │ Agent 2 │  │  Team   │    │
│  └────┬────┘  └────┬────┘  └────┬────┘  └────┬────┘    │
│       │            │            │            │          │
│  ┌────▼────────────▼────────────▼────────────▼────┐    │
│  │              Agno Runtime                       │    │
│  │         (LLM calls, tool execution)            │    │
│  └─────────────────────────────────────────────────┘    │
│                                                          │
│  ┌─────────────────────────────────────────────────┐    │
│  │                Memory System                     │    │
│  │  (Mem0, file, or none; agent/team scopes)       │    │
│  └─────────────────────────────────────────────────┘    │
└─────────────────────────────────────────────────────────┘
```

## Components

- [Matrix Integration](https://docs.mindroom.chat/architecture/matrix/) - How MindRoom connects to Matrix
- [Internal Turn CLI](https://docs.mindroom.chat/architecture/agent-cli/) - Minimal-mode discovery, response ownership, and approval recovery
- [Agent Orchestration](https://docs.mindroom.chat/architecture/orchestration/) - How agents are managed
- [Bot Runtime](https://docs.mindroom.chat/architecture/bot-runtime/) - The inbound turn pipeline and its module boundaries
- [Migration and Compatibility Boundaries](https://docs.mindroom.chat/architecture/migrations/) - Current owners for historical formats, dependency migrations, and retained compatibility
- [Matrix Event-Journal Security](https://docs.mindroom.chat/architecture/matrix-event-journal-security/) - Which decrypted plaintext is durable, who owns it, and what removes it
- [Matrix Event-Journal Contracts](https://docs.mindroom.chat/dev/matrix-event-journal-contracts/) - What the journal guarantees, and the homeserver behaviour you would otherwise rediscover by debugging

## Key Internal Modules

| Module | Purpose |
|--------|---------|
| `bounded_bytes.py` | Shared asynchronous byte collection that rejects overflowing chunks before buffering them |
| `knowledge/collection_lifetime.py` | Compatible publication selection, shared reader locks, and exclusive collection reclamation |
| `orchestrator.py` | MultiAgentOrchestrator — boots entities, manages sync loops, hot-reload |
| `orchestration/` | Extracted orchestrator helpers (sync loops, config diffing, room invitations) |
| `orchestration/config_lifecycle.py` | Debounced config-reload lifecycle: queueing, response drain, and update-plan dispatch |
| `orchestration/background_workers.py` | Config-gated start and stop of the memory auto-flush and skill-learning workers |
| `config_bundle.py` | Native staged bundle validation, drift protection, directory publication, and recovery journals |
| `cli/config_bundle.py` | Bundle install receipts and initialize-only runtime bootstrap command adapter |
| `runtime_state.py` | Shared runtime readiness state for health/ready endpoints |
| `runtime_resolution.py` | Authoritative runtime resolution for agent materialization |
| `team_exact_members.py` | Runtime resolution for team member materialization |
| `model_loading.py` | Authoritative model instantiation and provider-specific loader selection |
| `ai_runtime.py` | Agent-run input preparation and queued-notice hooks |
| `agent_storage.py` | Agent session and learning SQLite storage construction helpers |
| `background_loop.py` | Wakeable loops and cross-thread wake signals shared by durable per-session background workers |
| `skill_learning/queue.py` | Durable per-conversation review counters: run-ID accounting, scope keys, retries, and retention |
| `skill_learning/worker.py` | Background worker that counts completed runs, triggers reviews at the interval, and posts change notices |
| `skill_learning/reviewer.py` | One bounded skill review: Hermes-derived prompt, skill-only tools, read-before-write, and input budget |
| `skill_learning/transcript.py` | Conversation evidence for a review: older-turn digest plus the newest messages verbatim |
| `skill_learning/library.py` | Confined learner-owned skill writes, ownership marker, history snapshots, archival, and change fingerprints |
| `session_storage_preflight.py` | Required session-column checks and retained archives for incompatible owned session stores |
| `agent_descriptions.py` | Shared agent description rendering for routing and delegation |
| `agent_policy.py` | Derives canonical execution policies from authored agent config |
| `minimal_agent.py` | Same live Agent with one provider-facing Bash tool and hidden canonical tool preparation |
| `agent_cli/` | Response-owned CLI grants, call admission, worker leases, discovery, and result projection |
| `agent_modes.py` | Conversation-scoped standard/minimal selection persistence |
| `cli_approval_recovery.py` | Exact saved CLI approval execution through rebuilt canonical bindings and ordinary interrupted-response recovery |
| `cli_approval_waits.py` | Response-owned CLI approval waits, exact journal claims, and terminal cleanup |
| `commands/mode_commands.py` | Authorized agent mode selection with canonical session scope and deployment preflight |
| `api/agent_cli.py` | Authenticated transport for response-owned CLI operations and live call receipts |
| `api/sandbox_runner_cli.py` | Worker CLI grant installation, network verification, and pinned shell transport |
| `tool_system/agent_tool_calls.py` | Prepared live-Agent catalog and serialized native execution of qualified tools |
| `tool_system/tool_access.py` | Shared qualified tool identities, discovery, schemas, and local argument validation |
| `shell_output_capture.py` | Bounded shell output spools, completion validation, and atomic output-file publication |
| `workspaces.py` | Agent workspace scaffolding, template seeding, context file resolution |
| `worker_browser.py` | Serializes dedicated-worker headless browser calls, retains browser resources, and owns configuration/environment retirement and shutdown cleanup |
| `tool_system/google_workspaces.py` | Workspace-specific Google OAuth provider construction and tool registration |
| `bot.py` | AgentBot and TeamBot runtime shells for Matrix lifecycle and sync callbacks |
| `matrix/durable_ingestion.py` | Validates owned batches, invokes journal admission, runs ordered hooks/callbacks, and acknowledges nio |
| `matrix/journal_ingress.py` | Typed event classification from nio provenance and reconstruction of stored events |
| `matrix/media.py` | Shared Matrix media encryption preparation, upload, download, and decryption helpers |
| `matrix/encrypted_file.py` | Dependency-free encrypted-file serialization shared by uploads, desktop, and runtime media |
| `matrix/personal_rooms.py` | Target-agent service for eligible personal-room creation, ownership checks, invitations, and recoverable welcome delivery |
| `matrix/personal_room_store.py` | Durable per-requester room ownership, operator adoption attestations, welcome receipts, and cleanup retention |
| `event_journal/` | Durable ownership of admitted Matrix events, conversation projection, and delivery outbox |
| `journal_dispatch.py` | Fan admitted journal events out to typed Matrix callbacks and settle the ones that finish |
| `pending_event_worker.py` | Decides when pending journal work runs, and wakes itself again whenever a pass stops early |
| `turn_controller.py` | TurnController — owns one inbound turn from ingress to recorded outcome |
| `ingress_validation.py` | Ingress boundary validation: trust, effective requester, handled-id dedup, router-echo drop, command detection |
| `inbound_turn_normalizer.py` | Raw input shaping (text, voice, sidecars, media) into canonical turn inputs |
| `conversation_resolver.py` | Conversation identity, thread history, and ingress envelope assembly |
| `ingress_lanes.py` | Per-(room, sender) receipt-order FIFO delivering resolving ingress (voice/STT readiness) to conversations |
| `coalescing.py` | Live message coalescing gate; ordinary text dispatches immediately, adaptive text waits for its quiet window, and media waits for attachments and a trailing caption |
| `text_ingress_dispatch.py` | Text ingress dispatch path used by TurnController |
| `turn_policy.py` | Pure turn policy: decide ignore, route, or respond for inbound turns |
| `participation.py` | Framework-independent participation state: one immutable decision, concurrent checks, and approval-preserving settlement |
| `mid_turn.py` / `mid_turn_judgment.py` | Per-response finish-or-wrap-up judgments for queued human messages, bound to interchangeable LLM or TypeSafe backends |
| `config/mid_turn.py` | Opt-in agent settings for the mid-turn judgment backend and decision instructions |
| `agno_participation.py` | Agno participation adapter: prepared request checks, primary-run isolation, metrics, and scoped model interception |
| `judgment/` | Backend-independent boolean and choice questions, minimized context, shared execution limits, and LLM/System One adapters |
| `participation_judgment.py` | Bind the participation rubric to an opt-in LLM or TypeSafe judge and map its result to a participation decision |
| `provider_tool_policy.py` | Task-local restriction enforced by provider adapters before native tools can execute |
| `groq_model.py` | Groq adapter enforcing provider tool restrictions for Compound systems |
| `config/participation.py` | Opt-in participation settings for existing thread agents: bounded pause and decision instructions |
| `config/skill_learning.py` | Opt-in agent settings for background skill reviews: interval, reviewer model, notices, and archival |
| `config/personal_rooms.py` | Opt-in personal-room settings and validation for commands, aliases, and message templates |
| `command_turn_executor.py` | Command execution and durable command/config mutation journals |
| `reaction_dispatch.py` | Durable semantic routing for Matrix reactions |
| `user_stop_reconciliation.py` | STOP ordering, response cancellation, and terminal turn reconciliation |
| `visible_response_reconciliation.py` | Visible Matrix response recovery, adoption, and replay reconciliation |
| `turn_store.py` | Unified durable turn access (wraps the handled-turn ledger) |
| `handled_turns.py` | Disk-backed handled-turn ledger preventing duplicate responses |
| `response_runner.py` | Response lifecycle execution (locking, streaming vs non-streaming, cancellation, detached inbox responses, shutdown drains) |
| `response_turn.py` | Shared blocking and streaming response-turn drivers, including retries and dynamic-tool continuation |
| `response_attempt.py` | Executes one visible response attempt with stop tracking |
| `response_terminal.py` | Classifies pending-visible failures and terminal stream outcomes |
| `response_lifecycle.py` | Shared response lifecycle helpers and queued-notice state |
| `approval_tools.py` | Rebuilds required saved-approval tools from current configuration and scoped MCP catalogs for agent and team continuation |
| `execution_preparation.py` | Request-scoped execution preparation for prompts and persisted replay |
| `response_payload_preparation.py` | Execution-side, under-lock assembly of one response's payload from immutable ingress inputs |
| `delivery_gateway.py` | Visible Matrix delivery for already-generated responses (send, edit, finalize) |
| `custom_tools/chat_ui.py` | Runtime-bound MindRoom Chat UI action requests with canonical Matrix conversation and sender identity |
| `custom_tools/matrix_message_idempotency.py` | Bounded durable keyed Matrix sends: preparation, receipts, retention, replay, and current authorization checks |
| `tools/chat_ui.py` | Tool-catalog registration and discovery metadata for Chat UI actions |
| `visible_voice_echo.py` | Immediate router voice-placeholder delivery, replacement ordering, and deduplication |
| `personal_room_lifecycle.py` | Personal-room command and membership policy, target-service routing, reconciliation, and separate rejoin/cleanup retention projections |
| `post_response_effects.py` | Shared post-response effects after Matrix delivery |
| `routing.py` | Intelligent agent or team selection when no entity is mentioned |
| `routing_judgment.py` | Opt-in bounded System One responder selection, with explicit no-fit outcomes and existing LLM routing fallback |
| `streaming.py` | Streaming state machine and progressive response state |
| `media_inputs.py` | Shared media-input container passed across bot, teams, and AI layers |
| `provider_media_fallback.py` | Retries provider requests without rejected inline media and remembers unsupported kinds per model route for the process lifetime |
| `model_stream_output.py` | Shared policy for streamed output that makes provider retries unsafe |
| `file_memory_knowledge.py` | Shared resolution for agent file-memory semantic knowledge overlays |
| `memory_scope_ids.py` | Cycle-free canonical agent memory scope identifiers |
| `avatar_generation.py` | Generates and manages avatar assets for agents, rooms, and spaces |
| `topic_generator.py` | AI-generated room topics |
| `background_tasks.py` | Non-blocking async task management with GC protection |
| `api/usage_export.py` | Application-scoped usage-export preparation: one background scan, a bounded cache for daily/request-detail variants, committed-generation validation, and non-blocking shutdown cleanup |
| `desktop/session.py` | Owns the desktop device's durable NIO session and storage binding |
| `desktop/transport.py` | Polls owned to-device work and acknowledges only after durable command admission |
| `desktop/command_journal.py` | Persists command admission, execution outcomes, and pending responses |
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

## Storage upgrade boundaries

Historical formats stay with their storage or lifecycle owners, while current callers consume canonical identities and paths.
`legacy_private_storage_aliases.py` owns historical requester spellings and verified aliases; only startup migration, `private_storage_paths.py`, and `usage_stats_storage.py` can import it.
Usage discovery uses verified aliases only to classify coverage, skipping duplicate historical paths while scanning their canonical directories.
Worker mount planning and sandbox path validation use `private_storage_paths.py`, while `private_instance_identity_store.py` validates current identities.

`oauth/legacy_credentials.py` owns publication-field normalization and lossless historical requester bindings.
Only `oauth/credential_store.py` can import it; the store retains schema, scope validation, current credential state, transaction locks, retries, and commit ownership.
OAuth credentials stored only in legacy JSON files require reconnection; those files and their obsolete sidecars remain untouched.

Existing lifecycle adapters remain at their focused entry points: `legacy_private_storage.py` at startup, `config/legacy_access.py` during config loading, and Nio journal and crypto adapters when their stores open.
`session_storage_preflight.py` checks owned session databases before opening them and archives session directories whose tables lack required columns; see [Session Storage Recovery](https://docs.mindroom.chat/architecture/orchestration/#session-storage-recovery).
Tach visibility rules keep compatibility internals behind their owning boundaries.

## Data Flow

1. **Message arrives** from the Matrix homeserver; `matrix/durable_ingestion.py` validates the owned batch, invokes the journal transaction owner to commit it, runs ordered hooks/callbacks, and acknowledges nio.
   `matrix/journal_ingress.py` supplies typed classification from nio provenance and replay reconstruction.
   `journal_dispatch.py` hands admitted events through `bot.py` to `turn_controller.py`, which owns the turn from ingress to recorded outcome.
2. **Input is validated, normalized, and resolved**: `ingress_validation.py` checks trust and the effective requester, deduplicates handled event ids, and drops trusted router echoes; `inbound_turn_normalizer.py` shapes raw text, voice, and media into canonical turn inputs, and `conversation_resolver.py` resolves thread identity and history; `!commands` are control inputs that dispatch directly here instead of entering coalescing
3. **Messages are ordered and coalesced**: `ingress_lanes.py` delivers each sender's messages in receipt order (late-ready voice/STT waits in the lane), and `coalescing.py` batches each sender's live conversation burst.
   Ordinary text completes an utterance and dispatches immediately; adaptive text waits its configured quiet period.
   Later adaptive text cannot extend an earlier immediate text's wait.
   A live batch ending in media waits for more attachments or a trailing caption.
   Follow-up backlogs queued behind an active response flush at idle in receipt order, one combined turn per consecutive same-requester run, so no sender's messages execute under another sender's identity; conversations never wait on each other.
4. **The turn is planned**: `turn_policy.py` decides to ignore, route, or respond; a direct responder is resolved when one eligible agent or team remains, otherwise the router selects among candidates
5. **Selected entity processes** the message via `response_runner.py` and the Agno runtime, executing tools as needed
6. **Response is delivered** through `delivery_gateway.py`, which owns Matrix send/edit/finalization while `streaming.py` owns progressive response state
7. **The turn is recorded** in the durable handled-turn ledger (`turn_store.py` / `handled_turns.py`) so restarts do not double-reply
8. **Memory updates** asynchronously in background

See [Bot Runtime](https://docs.mindroom.chat/architecture/bot-runtime/) for the module boundaries and the ongoing simplification roadmap.

## Confined filesystem paths

`mindroom.path_confinement.resolve_path_within_root` is the shared implementation for workspace and artifact path containment.
Callers supply an authorized root and explicitly choose `internal` (allow links targeting that root), `reject` (reject descendant links), or `preserve_leaf` (reject linked parents while preserving the final entry for unlink or atomic replacement).
Existing workspace and tool wrappers retain their input rules and error messages.
Do not add another `resolve()` plus containment check when implementing a file consumer.

Path resolution is a point-in-time check, not protection for a later file open.
For local I/O requiring protection against symlink swaps, use `open_directory_within_root` or `open_regular_file_within_root` and keep operations relative to the returned descriptor.
Publish bytes using `atomic_file.atomic_write_bytes_at`, or stream through `atomic_write_file_at`; both use the same atomic transaction and descriptor-relative cleanup.
The action-based browser publishes captures and completed downloads through these helpers and snapshots upload sources through confined descriptors.
Upload snapshots remain private until tab or profile teardown because Playwright reads selected files lazily.
Drive downloads stream into a confined atomic file rooted at the authorized workspace.
Caller-owned root authorization, directory identity checks, filesystem permissions, and requester isolation remain required.
These helpers do not prevent hard-link aliasing or arbitrary directory relocation, and do not protect subsequent pathname opens by native MCP browser tools, Drive uploads, shell commands, or ordinary file-tool implementations.
