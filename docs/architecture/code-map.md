# Code Map

Where things live in `src/mindroom/`, for locating code.
When you add, rename, or remove a key module, update its row in the same PR.

## Inbound Turn Pipeline

The path from a Matrix message to a delivered response; [Bot Runtime](bot-runtime.md) explains why the runtime is split this way.

```text
Matrix sync callback
  -> matrix/durable_ingestion.py                           (validate owned Nio work, commit its sequence and effects, then acknowledge)
  -> bot.py (AgentBot/TeamBot runtime shell)
  -> journal_dispatch.py + pending_event_worker.py         (fan admitted events out to callbacks; unsettled work is woken again)
  -> turn_controller.py (owns one turn: precheck -> normalize -> resolve -> coalesce -> decide -> execute -> record)
       -> ingress_validation.py                                  (trust, dedup, echo drop; commands exit before batching)
       -> inbound_turn_normalizer.py + conversation_resolver.py  (canonical turn input, conversation identity)
       -> coalescing/ingress_lanes.py                            (per-(room, sender) receipt-order FIFO; STT readiness waits here)
       -> coalescing/gate.py                                     (ordinary text dispatches immediately; adaptive text and media debounce)
       -> text_ingress_dispatch.py + turn_policy.py              (ignore / route / respond decision, command execution)
       -> response_runner.py -> ai.py / teams/envelope.py        (lifecycle lock, entity envelopes)
            -> response_turn.py                                  (shared blocking/streaming turn drivers)
       -> streaming.py + delivery_gateway.py                     (progressive edits, Matrix send)
       -> turn_store.py / handled_turns.py                       (durable dedup so restarts don't double-reply)
```

## Modules

| Module | Purpose |
|--------|---------|
| `bounded_bytes.py` | Shared asynchronous and synchronous byte collection that rejects overflowing chunks before buffering them |
| `origins.py` | Shared browser-origin normalization and allowlist validation for dashboard auth, CORS, Connections same-origin checks, the Computers and Connections MindRoom Chat allowlists, and MCP gateway origins |
| `matrix_openid.py` | Matrix OpenID verification at the configured homeserver and audience binding, shared by Computers and the Connections portal |
| `atomic_file.py` | Shared atomic byte publication and cleanup relative to an opened directory |
| `orchestrator.py` | MultiAgentOrchestrator - boots agents, manages sync loops, hot-reload |
| `orchestration/` | Extracted orchestrator helpers (config update plans, plugin watch, rooms, runtime) |
| `orchestration/config_lifecycle.py` | Debounced config-reload lifecycle: queueing, response drain, and update-plan dispatch |
| `config/bundle.py` | Native staged bundle validation, drift protection, directory publication, and recovery journals |
| `cli/config_bundle.py` | Bundle install, change-classification, and runtime-confirmed apply/rollback command adapters, plus initialize-only runtime bootstrap |
| `runtime_state.py` | Shared runtime readiness state for health/ready endpoints |
| `event_loop_stall.py` | Native-thread event-loop stall detector that logs the blocking stack |
| `runtime_resolution.py` | Authoritative runtime resolution for one agent materialization |
| `bot.py` | AgentBot and TeamBot runtime shells for Matrix lifecycle, sync callbacks, and room behavior |
| `turn_controller.py` | TurnController - owns one inbound turn from ingress to recorded outcome |
| `ingress_validation.py` | Ingress boundary validation: trust, effective requester, handled-id dedup, router-echo drop, command detection |
| `inbound_turn_normalizer.py` | Raw input shaping (text, voice, sidecars, media) into canonical turn inputs |
| `conversation_resolver.py` | Conversation identity, thread history, and ingress envelope assembly |
| `coalescing/ingress_lanes.py` | Per-(room, sender) receipt-order FIFO delivering resolving ingress (voice/STT readiness) to conversations |
| `coalescing/gate.py` | Live message coalescing gate; ordinary text dispatches immediately, adaptive text waits for its quiet window, and media waits for attachments and a trailing caption |
| `coalescing/batch.py` | Coalesced dispatch batch construction |
| `text_ingress_dispatch.py` | Text ingress dispatch path used by TurnController |
| `turn_policy.py` | Pure turn policy: decide ignore, route, or respond for inbound turns |
| `participation.py` | Framework-independent participation state: one immutable decision, concurrent checks, and approval-preserving settlement |
| `participation_hook.py` | The reply's participation check as a pre-request loop hook: a non-streaming decision request on the reply's exact messages, or the configured judgment backend, ending the run when it stays silent |
| `judgment/` | Backend-independent boolean and choice questions, minimized context, shared execution limits, the bounded HTTP client, and LLM, System One, and OpenAI Decisions adapters |
| `participation_judgment.py` | Bind the participation rubric to an opt-in LLM, TypeSafe, or OpenAI Decisions judge and map its result to a participation decision |
| `mid_turn.py` | Per-response finish-or-wrap-up decisions over immutable queued-message snapshots |
| `mid_turn_judgment.py` | Bind the active request and agent settings to the shared LLM, TypeSafe, or OpenAI Decisions judgment backend |
| `config/mid_turn.py` | Opt-in agent settings for the mid-turn judgment backend and decision instructions |
| `judgment/cold_cache.py` | Decide from a thread's idle time and an opt-in judge whether the thread moves to the agent's cheap model |
| `config/cold_cache.py` | Opt-in agent settings for the cold-cache judgment backend |
| `providers/tool_policy.py` | Task-local restriction enforced by provider adapters before native tools can execute |
| `providers/groq_model.py` | Groq adapter enforcing provider tool restrictions for Compound systems |
| `config/participation.py` | Opt-in participation settings for existing thread agents: bounded pause and decision instructions |
| `config/skill_learning.py` | Opt-in agent settings for skill reviews: interval, review model, notices, and archival |
| `dispatch_replay_guard.py` | Replay-guard checks for dispatch sequencing |
| `event_journal/` | Durable ownership of admitted Matrix events, conversation projection, and delivery outbox |
| `event_journal/write_queue.py` | The one writer task each backend's writes queue for, from any event loop; the writes queued at once commit in one transaction |
| `response_sources.py` | Immutable response-attempt source identity shared by runtime and persistence boundaries |
| `reply_lifecycle.py` | Pure rules for durable reply records: every event that changes a reply, its spans, rows, and effects (see [Reply Messages](reply-messages.md)) |
| `reply_presentation.py` | Reply presentation model, its JSON codec, and rendering into a body, trace, and terminal wire status |
| `reply_scope.py` | `ReplyRuntime` and `SpanHandle`: reply claims, span exits, write-ahead, owed writes, and runtime ownership |
| `edit_regenerator.py` | Decides which message edits regenerate a reply and hands each to a regeneration off the room's event lane |
| `event_journal/replies.py` | `ReplyStore`: reply rules applied inside journal transactions, with their in-transaction effects |
| `event_journal/legacy_response_attempts.py` | One-time cancellation of the approvals an earlier release left pending, and removal of the tables that kept their reply identity |
| `event_journal/scheduled_approvals.py` | Stored scheduled tool calls and their one-shot approvals: binding, fire-time arming, withdrawal, claim with its receipt, and outcome |
| `journal_dispatch.py` | Fan admitted journal events out to typed Matrix callbacks and settle the ones that finish |
| `pending_event_worker.py` | Decides when pending journal work runs, and wakes itself again whenever a pass stops early |
| `command_turn_executor.py` | Command execution and durable command/config mutation journals |
| `reaction_dispatch.py` | Durable semantic routing for Matrix reactions |
| `user_stop_reconciliation.py` | Records a Stop reaction on the reply it stops |
| `visible_response_reconciliation.py` | Visible Matrix response recovery, adoption, and replay reconciliation |
| `turn_store.py` | Unified durable turn access (wraps the handled-turn ledger) |
| `handled_turns.py` | Disk-backed handled-turn ledger preventing duplicate responses |
| `response_runner.py` | Response lifecycle execution (locking, streaming vs non-streaming, cancellation, detached inbox responses, shutdown drains) |
| `response_turn.py` | Shared blocking/streaming response-turn drivers behind the agent and team envelopes (attempt loop, model-switch continuation, empty-run retry, interrupt recording) |
| `response_attempt.py` | Runs one visible response attempt as its reply span's cancellable task |
| `response_lifecycle.py` | Shared response lifecycle helpers and queued-notice state |
| `execution_preparation.py` | Request-scoped execution preparation for prompts and persisted replay |
| `response_payload_preparation.py` | Execution-side, under-lock assembly of one response's payload from immutable ingress inputs |
| `delivery_gateway.py` | Visible Matrix delivery for already-generated responses (send, edit, finalize) |
| `custom_tools/matrix_message_idempotency.py` | Bounded durable keyed Matrix sends: preparation, receipts, retention, replay, and current authorization checks |
| `personal_room_lifecycle.py` | Personal-room command and membership policy, target-service routing, reconciliation, and separate rejoin/cleanup retention projections |
| `post_response_effects.py` | Shared post-response effects after Matrix delivery |
| `file_access.py` | Agent `file_access` resolution and the shared path authorization every path-taking tool opens files through |
| `orchestration/config_warnings.py` | Startup and reload warnings for risky but allowed config choices (foreign homeserver authorities, unconfined primary-process tools next to worker code tools) |
| `approval/tool_approval.py` | Tool-call approval rule evaluation and public approval API |
| `approval/execution.py` | Agent reconstruction and exact-call execution for persisted native approval continuations |
| `approval/resume.py` | Resuming a persisted paused agent run on the owned loop with exact decisions: its history replay, native history, hooks, and the parent continuation the delegation driver injects |
| `loop_hooks.py` | The hooks an agent run or resume installs on the owned loop, in the order the turn applies them |
| `approval/tool_owners.py` | Recorded toolkit restoration and exact owner validation for saved approvals |
| `approval/response.py` | Response-side native approval continuation persistence, card publication, and terminal settlement |
| `approval/manager.py` | Matrix-backed tool approval runtime state |
| `oauth/credential_binding.py` | Canonical OAuth provider and worker-target bindings for browser workflows |
| `oauth/credential_lifecycle.py` | Single transaction owner for scoped OAuth load, refresh, callback publication, invalidation, and reset state |
| `oauth/credential_store.py` | Per-scope SQLite OAuth credential storage, revisions, and reset receipts |
| `oauth/reset.py` | OAuth reset target resolution and requester-bound browser intents |
| `oauth/reset_execution.py` | MCP retirement and durable reset execution |
| `oauth/microsoft.py` | Microsoft 365 OAuth providers sharing one Entra app, with the tenant resolved per runtime |
| `custom_tools/oauth_connections.py` | Requester-bound agent tool for issuing OAuth reset confirmation links |
| `egress_broker/` | Agent Keys: the egress broker in the primary that adds stored keys or connected-account tokens to worker HTTP(S) requests the worker never sees |
| `egress_broker/service.py` | Broker lifecycle on its own thread, secret resolution per verified worker scope, and the per-call broker env for worker-routed calls |
| `egress_broker/proxy.py` | Authenticated forward-proxy listener: CONNECT interception or blind tunnels, plain HTTP forwarding, and the unmatched-host policy |
| `egress_broker/mitm.py` | TLS interception for hosts with rules: `Host` checks against the CONNECT target and per-request credential injection |
| `egress_broker/_relay.py` | HTTP/1.1 plumbing both listener paths share: request framing, guarded dialing, body streaming, refusals, and audit rows |
| `egress_broker/dial.py` | Guarded upstream dialing: refuses loopback, private, and cloud metadata destinations and connects only to the address it validated, through the operator proxy when the primary has one |
| `egress_broker/rules.py` | Pure rule matching, path restriction checks, ambiguous-path refusal, and credential injection into a header or query parameter |
| `egress_broker/user_services.py` | Services users define per scope, stored beside the scope's keys and merged after the config services into the config the broker matches |
| `egress_broker/secrets.py` | Per-scope egress keys in the primary-only credential store and each service's key-or-account status |
| `egress_broker/tokens.py` | Signed, expiring worker proxy tokens that carry the worker scope, agent, and requester |
| `egress_broker/audit.py` | Metadata-only SQLite request log, pruned to 30 days and 100,000 rows |
| `egress_broker/ca.py` | Local broker CA, per-host leaf certificates, the SNI check against the CONNECT host, and the worker trust bundle |
| `api/egress_broker.py` | Dashboard Agent Keys routes: service status, presets, keys, account connect and disconnect, the full request log, and the CA download |
| `api/connections_session.py` | Connections portal sign-in: exchanges a Matrix OpenID token from MindRoom Chat for a portal session cookie |
| `api/connections_session_cookie.py` | Signed, process-local portal session cookies with a fixed one-hour lifetime |
| `api/egress_credentials.py` | Personal `/api/connections/egress` routes: per-agent services and keys, user services, account connections, and the caller's request log |
| `api/egress_status.py` | Service status, preset, and request-log responses and the account-connection gate shared by both egress APIs |
| `config/egress_broker.py` | `egress_broker` config models: services, preset expansion, rules, auth types, and their validation |
| `custom_tools/egress_credentials.py` | `egress_credentials` agent tool: which services have a key or connected account in the agent's scope, and where to add missing ones |
| `event_journal/tool_jobs.py` | Background tool job snapshots and outcome payloads, written only by the runtime generation that owns them |
| `tool_jobs/runtime.py` | Accepted execution ownership, durable outcomes, scoped discovery, and result claims |
| `delegation/background.py` | A background subagent's job: native child admission, outcome, and cleanup inside the generic execution owner |
| `delegation/job_approvals.py` | A background child's approval requests, asked through its job's cards |
| `tool_jobs/instances.py` | One per-storage-root instance: startup-pinned setting and published runtime |
| `tool_jobs/settings.py` | Startup-pinned feature and toolkit exclusion settings |
| `tool_jobs/execution.py` | The loop's job admission middleware, the managed call it runs, and the `background_after` definitions step |
| `tool_jobs/consumption.py` | Exact durable acknowledgement after the parent run saves a consumed result |
| `tool_jobs/execution_scope.py` | Response execution envelopes |
| `tool_jobs/resources.py` | Retained resource cleanup ownership |
| `tool_jobs/authorization.py` | Current local grants using shared construction policy |
| `tool_jobs/execution_authority.py` | Application-entry authority checks |
| `tool_jobs/provenance.py` | Exact MCP bridge identity for grant checks |
| `tool_jobs/control.py` | Newer human messages that release waits, and cancellation checkpoints |
| `tool_jobs/background_after.py` | The reserved `background_after` argument of managed calls and its validation |
| `tool_jobs/results.py` | Non-executable durable tool-value and rich-artifact serialization |
| `tool_jobs/legacy_results.py` | Job result payloads in the shape the Agno-based executor saved them |
| `tool_jobs/conversation.py` | The conversation key a reply finds its jobs by, and how long a reply waits for a job's approval |
| `tool_jobs/notices.py` | The notice on a tool result and the turn-start note about jobs that ended |
| `tool_jobs/publication.py` | Each agent's live jobs, published per room as state for the client jobs bar |
| `tool_jobs/client_cancel.py` | Client cancel events, `!jobs` and `!cancel_job`, and a left room's jobs |
| `tool_jobs/approvals.py` | Approval cards a job posts and owns for its child's or its own gated call, denied when the job ends early |
| `tool_jobs/follow_ups.py` | A follow-up's journal source, identity, streak, and envelope |
| `tool_system/construction.py` | Selected toolkit factory and digested constructor identity |
| `tool_system/filters.py` | Shared include/exclude function policy |
| `custom_tools/job.py` | Reserved job discovery and control tool |
| `orchestration/tool_job_runtime.py` | Managed job lifecycle: recovery, revocation, published jobs state, card denial, follow-ups, and retention |
| `workspaces.py` | Agent workspace scaffolding, template seeding, and context file resolution |
| `worker_computer/worker_browser.py` | Serializes dedicated-worker headless browser calls, retains browser resources, and owns configuration/environment retirement and shutdown cleanup |
| `agents.py` | `create_agent`: builds one configured agent's toolkits, prompt, model provider, store, and surface into an `AgentBuild` |
| `config/` | Pydantic models for YAML config parsing (root model in `config/main.py`) |
| `config/personal_rooms.py` | Opt-in personal-room settings and validation for commands, aliases, and message templates |
| `routing.py` | Intelligent responder selection when no agent or team is mentioned |
| `routing_judgment.py` | Opt-in bounded System One or OpenAI Decisions responder selection, with explicit no-fit outcomes and existing LLM routing fallback |
| `teams/formation.py` | Forming a team from one turn: who was asked, who is eligible, which members can be materialized, and the execution-time mode choice for an ad hoc team |
| `teams/members.py` | Materializing a team's exact members as agent builds without silent shrinkage, and the leader's and members' model aliases for a turn |
| `teams/spec.py` | `TeamMode`, `TeamMember`, and `TeamSpec`: what a team run is built from |
| `teams/prompt.py` | The leader's system message in the layout Agno rendered: roster, mode block, tool instructions, provider note, context |
| `teams/delegate_tools.py` | `delegate_task_to_member` and `delegate_task_to_members`, the member result texts, and the pause a paused member propagates to the team run |
| `teams/runtime.py` | `run_team` and `continue_team` over the owned loop: members as child runs, member decisions routed by member and call, team usage |
| `teams/presentation.py` | The team reply document: header, member slots, consensus, tool trace, and the durable `team_stream` snapshot an approval resume restores |
| `teams/envelope.py` | `team_response`, `team_response_stream`, and `continue_paused_team_run`: build the leader and members, run the team through the turn drivers, drive a member's delegations and resume the run with their results, render the reply |
| `agent_policy.py` | Canonical execution-policy derivation from authored agent config |
| `agent_cli/minimal_agent.py` | Same live Agent with one provider-facing Bash tool and hidden canonical tool preparation |
| `agent_cli/cli_shell_agent.py` | Standard agents whose native shell commands call their other tools through `mindroom-agent` |
| `agent_cli/` | Response-owned CLI grants, call admission, shell access, discovery, and result projection |
| `agent_modes.py` | Conversation-scoped standard/minimal selection persistence |
| `agent_cli/minimal_mode_preflight.py` | Minimal-mode eligibility for `!mode` and minimal subagents, reported as one actionable checklist |
| `commands/mode_commands.py` | Authorized agent mode selection with canonical session scope and deployment preflight |
| `api/agent_cli.py` | Authenticated transport for response-owned CLI operations and live call receipts |
| `agent_cli/cli_approval_recovery.py` | Exact saved CLI approval execution through rebuilt canonical bindings and ordinary interrupted-response recovery |
| `agent_cli/cli_approval_waits.py` | Response-owned CLI approval waits, exact journal claims, and terminal cleanup |
| `tool_system/agent_tool_calls.py` | Prepared live-Agent catalog and serialized native execution of qualified tools |
| `tool_system/tool_access.py` | Shared qualified tool identities, discovery, schemas, and local argument validation |
| `memory/` | Mem0 memory: agent and team-scoped |
| `memory/auto_flush.py` | Background passes that turn file-memory agents' new finished turns into daily notes, with per-conversation progress below `tracking/` |
| `knowledge/file_memory_knowledge.py` | Shared resolution for agent file-memory semantic knowledge overlays |
| `memory_scope_ids.py` | Cycle-free canonical agent memory scope identifiers |
| `history/conversation_text.py` | What stored turns said, for background model calls: which turns and messages count and who said each |
| `embeddings/` | The `Embedder` interface semantic indexes embed queries and documents through, its OpenAI-compatible, Ollama, and local sentence-transformers implementations, embedder health, and vector compatibility signatures |
| `knowledge/` | Knowledge base / RAG file indexing with watcher |
| `knowledge/file_listing.py` | Which files belong to a knowledge base: include patterns, traversal, symlink-safe inclusion rules |
| `knowledge/collection_lifetime.py` | Compatible publication selection, shared reader locks, and exclusive collection reclamation |
| `knowledge/search.py` | What an agent searches: its published sources, the `search_knowledge_base` tool and its results, and the `<knowledge_base>` prompt block |
| `knowledge/readers.py` | The Agno document readers and chunkers knowledge indexing reads files with; the only knowledge module that imports Agno |
| `knowledge/collections.py` | Chroma collection lifecycle for one knowledge base: naming, opening, probing, writing a file's chunk rows, deleting, reclaiming |
| `knowledge/git_source.py` | The Git checkout a knowledge base indexes: clone, fetch, force-align, LFS hydration, credential injection |
| `knowledge/refresh_runner.py` | Dispatches one knowledge refresh: subprocess spawn, cancellation cleanup, publish and reconcile decisions |
| `knowledge/refresh_locks.py` | Process-wide refresh serialization (in-loop and cross-process source-root locks) and active-refresh bookkeeping |
| `knowledge_graph/scope.py` | Which graph an agent or private instance owns, named in code from its state root, embedder, and generation |
| `knowledge_graph/store.py` | The runtime's one Graphiti client and its episode, fact, and entity operations; health and the store registry |
| `knowledge_graph/clients.py` | Graphiti's LLM, embedder, and reranker clients backed by MindRoom's model and embedder construction |
| `knowledge_graph/usage.py` | Which conversation an extraction call's tokens are charged to |
| `knowledge_graph/extraction.py` | What Graphiti is told when it extracts an episode: the entity types and the extraction instructions, built-in, configured, and the names of the episode's known entities |
| `knowledge_graph/embedded_server.py` | The embedded FalkorDB server MindRoom starts, stops, and recovers after a crash |
| `knowledge_graph/episodes.py` | Stored runs rendered as `conversation` and `tool_results` episodes, and thread classification |
| `knowledge_graph/names.py` | The one name of each known agent, person, and `knowledge_graph.aliases` entry: speaker labels, names listed for extraction, and lookups |
| `knowledge_graph/ingestion.py` | Background ingestion passes over several graphs at once, with per-kind markers, backoff, and dropped episodes |
| `knowledge_graph/runtime.py` | Starts and stops the store and ingestion worker as agents enable the graph |
| `config/knowledge_graph.py` | The `knowledge_graph` section: extraction model, extraction instructions, ingestion settings, aliases, and generation |
| `custom_tools/knowledge_graph.py` | The read-only `knowledge_graph` tool |
| `tool_system/skills.py` | OpenClaw-compatible skill loading from skill roots and workspaces, and the read-only `SkillsToolkit` with its prompt block |
| `tool_system/skill_usage.py` | Workspace skill usage records the skill learner's archival reads as an inactivity clock |
| `tool_system/plugins.py` | Plugin loading and tool/skill extension |
| `tool_system/google_workspaces.py` | Workspace-specific Google OAuth provider construction and tool registration |
| `tool_system/atlassian_connections.py` | Additional Atlassian Cloud connection providers and prefixed tool registration |
| `scheduling/tasks.py` | Cron and natural-language task scheduling |
| `scheduling/scheduled_tool_calls.py` | Resolving a scheduled call on the agent's own live tools and running it once by task ID with its approval |
| `scheduling/executor.py` | Fire one scheduled task: hook emission, visible or silent Matrix delivery, and failure notices |
| `scheduling/scheduled_run_records.py` | Agent-workspace JSON receipts for silent scheduled runs |
| `tools/` | 100+ tool integrations |
| `tools/lumalabs.py` | Configurable SDK model binding for both inherited Luma video-generation methods |
| `tool_system/dependencies.py` | Auto-install per-tool optional dependencies at runtime |
| `ai.py` | AI response generation, streaming, and Matrix run metadata |
| `providers/model_loading.py` | Model instantiation and provider-specific loader selection |
| `providers/anthropic/messages.py` | The owned Claude adapter (`ModelProvider`) for the direct API, Vertex AI, and Bedrock Mantle routes, with their client factories, error mapping, and `build_provider` |
| `providers/anthropic/settings.py` | Claude route settings parsed from `extra_kwargs` under Agno's field names; unknown keys fail at construction |
| `providers/anthropic/wire.py` | Claude Messages API formatting of owned messages and tools, inlining of URL-backed media, and parsing of the finished response into an assistant message |
| `providers/anthropic/repairs.py` | Replay-safe transforms of a Claude request body: citations, tool-result order, tool-search blocks, inline media budgets |
| `providers/anthropic/prompt_cache.py` | The wire preparation of one Claude request body: history repairs, deferred tool search, tool choice, and the prompt-cache ladder |
| `providers/cache_ladder.py` | The Claude prompt-cache ladder primitives (breakpoint markers, message rungs, the tools marker, the shared system prefix) shared by the Claude adapter, the OpenRouter Claude ladder, and the Agno path |
| `providers/anthropic/native_compaction.py` | Claude native compaction: the compaction edit, the checkpoint replay projection, and the checkpoint recorded on each response |
| `model_catalog.py` | Allowlisted model metadata, Matrix icon upload/cache, and catalog revision |
| `model_catalog_receiver.py` | Router discovery admission, authenticated responses, and scope/lifetime checks |
| `model_selection.py` | Structured model request/result values and frozen acknowledgement metadata |
| `model_selection_scope.py` | Current joined membership and readable-root eligibility for model selection |
| `ai_runtime.py` | Agent-run input preparation and queued-notice hooks |
| `providers/media_fallback.py` | Provider-boundary inline-media retry and process-local capability learning per model route |
| `providers/model_stream_output.py` | Shared policy for streamed output that makes provider retries unsafe |
| `providers/openai/` | Owned OpenAI-family adapters: one Chat Completions adapter for every OpenAI-compatible route, one Responses adapter for OpenAI and Codex, `build_provider`, the OpenRouter Claude cache ladder, OpenAI server-side tool search tagging, and the history budget's size of explicitly replayed Responses input (`replay_estimate.py`) |
| `providers/pipeline/` | Stages composed around a provider's `stream()`: the malformed tool-call guidance retry, portable deferred tool search, media fallback, stream retry with the idle limit, the typed safeguard refusal, and request logging |
| `providers/registry.py` | `build_model_provider`: the dashboard key, the family adapter for a configured model, and the request pipeline around it (not wired yet) |
| `providers/gemini/` | Owned Gemini adapter for the Gemini API and Vertex AI: contents, Files API uploads, schema conversion, Google Search grounding, decision-request config, and reply parsing (not wired yet) |
| `providers/ollama_adapter.py` | Owned Ollama adapter over the native chat API (not wired yet) |
| `providers/synthetic_adapter.py` | Owned synthetic provider streaming seeded Lorem Ipsum and shell calls, and the response plan `synthetic_model.py` serves to the Agno path (not wired yet) |
| `providers/extra_kwargs.py` | Parses a model's `extra_kwargs` into its adapter's settings, rejecting unknown keys |
| `providers/errors.py` | One classification of provider statuses, context-window wording, and transport failures into the normalized provider errors, shared by every adapter |
| `providers/assistant_turn.py` | What every adapter does to finish a turn: the `mindroom_model` provenance stamp, timings, `<think>` splitting, one message per tool result |
| `llm/request_context.py` | The per-request logging context (agent, session, correlation id) bound around a model request and read by request telemetry |
| `providers/remote_media.py` | Downloads URL media for the adapters that send media only inline |
| `budgets/pricing.py` | Per-model USD pricing of token counters, keyed by the provider and model ID usage records |
| `budgets/spend.py` | Month-to-date per-requester spend derived from retained usage |
| `budgets/caps.py` | Per-user monthly caps kept as runtime state under the storage root, written atomically and read lock-free |
| `budgets/monitor.py` | Background spend refresh, the per-user cap store, and the per-reply decision to use the fallback model |
| `config/budgets.py` | Opt-in budgets: default monthly cap and fallback model |
| `session_storage/agent_storage.py` | Opens an agent's, a team scope's, or the system usage ledger's conversation store, running the preflight archive check and the one-time migrations first |
| `session_storage/store.py` | The owned conversation store: one SQLite database of sessions, their run log, summary rows, and usage ledger, each operation in one transaction on its own connection |
| `session_storage/schema.py` | Table names and the layout of a conversation database, column for column as Agno 3 created it |
| `session_storage/codec.py` | Run rows to run records and back, with the JSON keys stored runs carry |
| `session_storage/types.py` | `ConversationSession` and the other values the store reads and writes |
| `session_storage/lanes.py` | One FIFO lane per conversation database for storage operations started from async code |
| `session_storage/legacy_agno2_runs.py` | Run data older Agno releases stored: Agno 2 run blobs, JSON columns encoded twice, and rows without a position |
| `history/summary_rows.py` | The conversation log's summary rows: compare-and-append of compaction chunks, the event ids each row folds, and edit and redaction cuts |
| `history/summary_rows_schema.py` | Summary-row table layout and the shared SQL for which runs a session load returns |
| `history/legacy_compaction_state.py` | One-time conversion, when a conversation database opens, of earlier compaction state, the compaction archive, and unpositioned run rows into the log |
| `history/mid_turn_compaction.py` | Compaction between the model requests of one reply: request sizing, folding, and the scoped or run-local summary |
| `history/legacy_summary_system_prompt.py` | Approval resume of runs paused with the summary inside their saved system message |
| `history/summary_message.py` | The compaction summary as a user message: its text, its scope or run-local marker, and the marker that replays the scope's summary after a mid-turn compacted prompt |
| `history/session_runs.py` | Finished Matrix turns of an agent's conversations, live and compacted, with the approval hold-back and redaction filter background workers share |
| `skill_learning/capture.py` | The final model request of a counting response, kept for the review to fork |
| `skill_learning/runner.py` | In-memory reply counts per conversation; starts a review when a count reaches the interval, stops it when a new response starts, and posts change notices |
| `skill_learning/reviewer.py` | One bounded skill review: a fork of the response's final request with its tools unchanged, or a redacted digest replay when the request cannot be forked or another review model is set |
| `skill_learning/tools.py` | Skill tools shared by chat and the review: ownership, read-before-write, and landed-change tracking |
| `skill_learning/transcript.py` | Reply counting and the digest a replayed review reads: older turns shortened plus the newest messages verbatim |
| `skill_learning/library.py` | Confined workspace skill writes, ownership provenance, history snapshots, and archival |
| `automations/runner.py` | Automation schedule loop: cron timing per shared agent or private instance, definition lookup and `AutomationContext`, the visible prompts tagged `automation/<name>` in the agent's room or the requester's personal room, and the steps after their runs |
| `automations/registry.py` | The `@automation` decorator, discovery in plugin hooks modules, and the catalog of plugin automations beside the lazily loaded built-ins, carried by the plugin snapshot (`HookRegistry.automations`) |
| `automations/steps.py` | `AutomationContext`, the `Ask` and `Done` steps an automation returns to the runner, the automation hook-source prefix, and the always-loaded workspace files the built-ins maintain |
| `automations/threads.py` | The record of threads automations started, under `tracking/automations/`, so none is read back as a conversation |
| `automations/prompt_curation.py` | The `prompt_curation` automation: size check over always-loaded files, the bounded prompt, and the verify that asks for a re-check |
| `automations/exploration.py` | The `exploration` automation: leads from conversations' tool calls, the prompt, and the follow-up that schedules ingestion |
| `automations/dreaming.py` | The `dreaming` automation: changed-input agenda, per-run staging of `memory/`, proposal validation, the fresh-thread review, and batch-conflict apply with progress in the automation state directory below `tracking/` |
| `config/automations.py` | Automation entries (built-in and plugin) and their validation |
| `custom_tools/skill_manage.py` | Chat-time `skill_manage`, like Hermes' foreground tool, for agents that list it or learn skills |
| `session_storage/preflight.py` | Required session-column checks and retained archives for incompatible owned session stores |
| `agent_descriptions.py` | Shared agent description rendering for delegation and orchestration |
| `delegation/personas.py` | Agent-authored subagent personas: the authored prompt and tool, skill, and knowledge subsets, `subagents/<name>.md` workspace profiles, and the caller tool, skill, and knowledge checks |
| `delegation/direct.py` | Reserve, run, and settle one child turn inside the caller's tool call, shared by `run_subagent` and Dynamic Workflow subagent participants |
| `credentials/manager.py` | Unified credential management (CredentialsManager) |
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
| `voice/handler.py` | Voice message download, transcription, mention normalization, and ASR cleanup |
| `matrix_rtc/audio_process.py` | Main-process side of a GPT-Live call whose media runs in its own process, plus the idle process started ahead of the next call |
| `matrix_rtc/audio_child.py` | Child process that runs one GPT-Live call's LiveKit room, frame encryption, and agent session away from the MindRoom event loop |
| `matrix_rtc/audio_protocol.py` | Length-prefixed JSON frames between MindRoom and a call's audio process |
| `tool_system/sandbox_proxy.py` | Container sandbox proxy for isolating shell/python tools |
| `api/sandbox_request_cancellation.py` | Stops an in-flight sandbox runner request when the primary stops waiting for it |
| `api/sandbox_execute_stream.py` | Streams one runner call's progress and keep-alives before its result, and stops the call when the primary hangs up |
| `tool_system/progress.py` | Live progress a running tool call reports to whatever owns it |
| `tool_system/dedicated_threads.py` | One thread per blocking call of a background job or worker proxy call, outside the bounded shared pools |
| `workers/in_flight.py` | Workers with a call in flight, which idle cleanup refreshes before it runs |
| `shell/output_capture.py` | Bounded shell output spools, completion validation, and atomic output-file publication |
| `shell/execution.py` | Shell command execution core: spawning, output buffering, stopping at a timeout, and the handle registry of Desktop shell and script runs |
| `shell/supervisor.py` | Worker-local shell supervisor process that runs shell commands and script handles for sandbox request subprocesses, stopping a command whose relay dies |
| `streaming.py` | Streaming state machine: placeholder, progressive edits, tool traces, cancellation |
| `prompts.py` | Built-in prompt defaults and prompt override registry |
| `attachments/storage.py` | Attachment persistence, registration, and context-scoped resolution |
| `attachments/ids.py` | Leaf attachment-ID helpers kept free of matrix-client imports |
| `attachments/media.py` | Convert attachment records to Agno media objects |
| `media_inputs.py` | Shared media-input container passed across bot, teams, and AI layers |
| `api/` | FastAPI REST API (dashboard, credentials, OpenAI-compatible endpoint) |
| `api/open_access.py` | Host allow-list and browser-origin guard for requests served without a credential (open dashboard auth, unauthenticated `/v1`) |
| `api/request_body_limit.py` | Pure ASGI middleware answering 413 for dashboard API request bodies over 16 MiB, except knowledge uploads |
| `api/validation_errors.py` | 422 handler for the dashboard API and sandbox runner that returns FastAPI's validation error detail without the rejected request values |
| `api/budgets.py` | Dashboard and budget-service routes for budget status and setting or clearing one user's cap |
| `api/usage_export.py` | Application-scoped usage-export preparation: one background scan, a bounded cache for daily/request-detail variants, committed-generation validation, and non-blocking shutdown cleanup |
| `custom_tools/` | Built-in custom tool implementations (gmail, calendar, scheduler, etc.) |
| `custom_tools/microsoft_graph.py` | Shared Microsoft Graph client for the Microsoft 365 tools: requester tokens, error codes, and workspace downloads and uploads |
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
| `tool_system/call_record.py` | Context-scoped recorder the tool hook bridge tells of each tool call before it runs, which reply spans persist |
| `tool_system/declarations.py` | Leaf tool metadata enums and dataclasses shared by implementations and the runtime catalog |
| `tool_system/registration.py` | Leaf built-in and plugin tool registration surface |
| `tool_system/metadata.py` | Runtime tool lookup, validation, plugin resolution, and instance construction |
| `tool_system/runtime_context.py` | Shared runtime ContextVar for tool calls (including attachment scope) |
| `tool_system/tool_function.py` | `ToolFunction`, the only tool shape the owned loop sees: finished schema, loop flags, entrypoint, injected parameter names |
| `tool_system/agno_toolkits.py` | Adapts Agno toolkits and functions to `ToolFunction` once, builds the Agno-shaped call-time arguments (run context, actor stand-in, media) their entrypoints expect, and is where modules outside the tool packages import Agno's tool types from |
| `tool_system/invocation.py` | Decodes a model's tool-call arguments and runs one call through middleware, to its outcome for the loop or to the tool's own value for runners outside it: injection, synchronous offload, generator draining, error text |
| `agent_loop/build.py` | `AgentBuild`, what `create_agent` produces for the loop, and `prompt_for_turn`, its system message with one turn's context appended |
| `agent_loop/prompt.py` | `PromptSpec` and `render_system_message`: an agent's system message from its prompt inputs, in the layout Agno 3.0.9 sent |
| `agent_loop/structured.py` | Structured-output response formats and JSON prompt per provider capability, and lenient parsing of answers into the schema |
| `agent_loop/helper.py` | `complete_once`: one tool-less helper request, optionally read into a schema, with the run record its usage is saved from; `complete_prompt` renders the helper's system message for it |
| `agent_loop/output_limit.py` | `output_limit_policy`: answers the tool calls of a response the output limit cut off instead of running them, and fails the run on the third cut-off response |
| `agent_loop/tool_search.py` | `resolve_call_tool_calls`: routes a model's portable `call_tool` wrapper calls to the deferred functions they name before the loop resolves them |
| `agno_compat_*.py` and subsystem-local `agno_compat_*.py` | Agno SDK repairs and private bindings; see `docs/architecture/agno-compatibility.md` for the boundary inventory and upstream tracking |
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
| `stop.py` | `SpanRegistry`: cancels exactly the reply span a Stop reaches, then its Agno run; the Stop button reaction |
| `topic_generator.py` | Managed room topics from the configured description or generated with AI |
| `debug_report.py` | Read-only collection of what the backend stored about one reported conversation: event journal, Agno runs, tool-call and LLM request logs, and log lines |
| `cli/main.py` | Main CLI entry point (Typer app) |
| `cli/banner.py` | CLI startup banner |
| `cli/config.py` | Config subcommand logic |
| `cli/connect.py` | `mindroom connect` pairing helpers and owner placeholder replacement |
| `cli/debug_report.py` | `mindroom debug-report` command: bug report parsing, config-only storage location, and JSON output |
| `cli/doctor.py` | Doctor command implementation |
| `cli/help_format.py` | Help-page panel order, green help theme, and the examples epilog shared by CLI commands |
| `cli/interactive.py` | Arrow-key prompts and step headers for terminal setup, with line prompts for piped input |
| `cli/local_stack.py` | Local stack setup command |
| `credentials/sync.py` | Shared provider/bootstrap env to credentials sync |
| `logging_config.py` | Structured logging setup |
| `knowledge/utils.py` | Resolves the knowledge bases an agent searches: published indexes, availability notices, and refresh scheduling |
| `custom_tools/chat_ui.py` | Runtime-bound MindRoom Chat UI action requests with canonical Matrix conversation and sender identity |
| `custom_tools/stop_reply.py` | Agent tool that stops another agent's reply with a 🛑 reaction relaying the caller's requester |
| `custom_tools/shared_directories.py` | Agent tool that creates shared directories and invites agents on behalf of a user who may use them |
| `shared_directories/` | The primary-only membership ledger and storage of shared directories, the paths agents see, and the share roots `file` and `coding` accept |
| `custom_tools/thread_move.py` | Copies a thread into another room as a new thread: per-entity re-posts, mention-guarded router relays, tag copy, and the moved notice in the source thread |
| `custom_tools/computer_announcement.py` | Tool hook that shows a chat_ui agent's worker computer in MindRoom Chat on its first worker browser call in a conversation |
| `tools/chat_ui.py` | Tool-catalog registration and discovery metadata for Chat UI actions |
| `voice/visible_voice_echo.py` | Immediate router voice-placeholder delivery, replacement ordering, and deduplication |
| `avatar_generation.py` | Generates and manages avatar assets for agents, rooms, and spaces |

## Persistent State

[Data Persistence](../deployment/storage.md#data-persistence) lists the main directories under the storage root (`mindroom_data/` by default, overridable via `MINDROOM_STORAGE_PATH`).
An agent's Agno session database, including its traces, is `agents/<agent>/sessions/<agent>.db`, and private agents keep the same per-agent layout under their resolved private state roots.
Its `<agent>_sessions_summaries` table holds compaction summary rows, and `<agent>_sessions_removed_runs` records runs that edits and redactions removed.
