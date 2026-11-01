# Code Map

Where things live in `src/mindroom/`, for locating code.
When you add, rename, or remove a key module, update its row in the same PR.

## Inbound Turn Pipeline

The path from a Matrix message to a delivered response; [Bot Runtime](bot-runtime.md) explains why the runtime is split this way.

```text
Matrix sync callback
  -> matrix/durable_ingestion.py                           (validate owned Nio work, commit its sequence and effects, then acknowledge)
  -> bot.py (AgentBot runtime shell, for agents, teams, and the router)
  -> journal_dispatch.py + pending_event_worker.py         (fan admitted events out to callbacks; unsettled work is woken again)
  -> turns/ingress.py (owns one event to gate admission: onboarding command -> approval reply -> precheck -> claim -> normalize -> resolve -> admit)
       -> ingress_validation.py                                  (classify provenance once, dedup, echo drop; commands exit before batching)
       -> inbound_turn_normalizer.py + conversation_resolver.py  (canonical turn input, conversation identity)
       -> voice/ingress.py + turns/interactive_selection.py      (voice readiness and selection answers, admitted in receipt order)
       -> coalescing/ingress_ticket.py                           (one item's lane slot, turn claim, and queued notice until admission)
       -> coalescing/ingress_lanes.py                            (per-(room, sender) receipt-order FIFO; STT readiness waits here)
       -> coalescing/gate.py                                     (ordinary text dispatches immediately; adaptive text and media debounce)
  -> turns/dispatch.py (owns one turn from gate exit: prepare -> decide -> execute -> record)
       -> turn_policy.py + command_turn_executor.py              (ignore / route / respond decision, command execution)
       -> response_runner.py -> ai.py / teams/envelope.py        (lifecycle lock, entity envelopes)
       -> reply_turn/locked_turn.py                              (one settlement for agent, team, and approval turns)
            -> response_turn.py                                  (shared streaming turn driver; blocking replies collect it)
       -> streaming.py + delivery_gateway.py                     (progressive edits, Matrix send)
       -> turn_store.py / event_journal/turn_rows.py             (durable dedup so restarts don't double-reply)
```

## Modules

| Module | Purpose |
|--------|---------|
| `failure_text.py` | The text a caught exception reports: its text, or its type name when it has none |
| `tls_trust.py` | Shared TLS trust (system store plus certifi's roots, unless the operator sets `SSL_CERT_FILE` or `SSL_CERT_DIR`) for Matrix, the egress broker's upstream connections, and Realtime speech |
| `bounded_bytes.py` | Shared asynchronous and synchronous byte collection that rejects overflowing chunks before buffering them |
| `origins.py` | Shared browser-origin normalization and allowlist validation for dashboard auth, CORS, Connections same-origin checks, the Computers and Connections MindRoom Chat allowlists, and MCP gateway origins |
| `matrix_openid.py` | Matrix OpenID verification at the configured homeserver and audience binding, shared by Computers and the Connections portal |
| `atomic_file.py` | Shared atomic byte publication and cleanup relative to an opened directory |
| `delivery_replay.py` | Durable per-scope claims for inbound deliveries: event ids, Matrix thread roots, and nonces, each scope in its own file under its own lock; external triggers also split their old shared file through it |
| `orchestrator.py` | MultiAgentOrchestrator - composition root: wires the fleet, services, and reload lifecycle, and applies reload plans |
| `integrations.py` | The built-in integration registry, the one place core wires an integration's runtime: each integration's public routes, its binding to the live bots and the shutdown drain of what it bound, final-reply mirroring, config checks, and created-agent tool edits |
| `orchestration/` | Extracted orchestrator helpers (config update plans, plugin watch, rooms, runtime) |
| `orchestration/config_lifecycle.py` | Debounced config-reload lifecycle: the process's one config load (published to the embedded API), queueing, response drain, and update-plan dispatch |
| `config/load_attempt.py` | The one config-file load attempt that the runtime and the dashboard API share |
| `config/commit.py` | The one config-file write: validated file content, include and personal-agent name refusals, and the staleness check for changes derived from a loaded config, under the roster lock |
| `api/config_lifecycle.py` | The dashboard API's published config snapshots: loads, commits published atomically with the file write, and the strict and last-good per-request readers |
| `orchestration/config_updates.py` | The config update plan: the one decision of what a reload restarts, starts, removes, and re-authorizes |
| `orchestration/entity_fleet.py` | The one owner of every managed bot's lifecycle: start, background retry, restart stop, removal, and process shutdown |
| `orchestration/rooms.py` | Managed room setup through the router: room creation, policy, invitations, root Space, server emoji room, and bot memberships |
| `orchestration/accounts.py` | Managed Matrix accounts: the internal user's and each configured entity's, localpart collisions checked before any is created, then the identity registry validated |
| `orchestration/storage_preparation.py` | Every startup migration, in the one order they depend on, before anything reads the storage root |
| `orchestration/turn_recovery.py` | When each bot replays the turn-journal work it left pending: once the bots its turns need completed their first sync |
| `config/bundle.py` | Native staged bundle validation, drift protection, directory publication, and recovery journals |
| `cli/config_bundle.py` | Bundle install, change-classification, and runtime-confirmed apply/rollback command adapters, plus initialize-only runtime bootstrap |
| `runtime_state.py` | Shared runtime readiness state for health/ready endpoints |
| `event_loop_stall.py` | Native-thread event-loop stall detector that logs the blocking stack |
| `runtime_resolution.py` | Authoritative runtime resolution for one agent materialization |
| `bot.py` | AgentBot runtime shell of every agent, team, and the router: Matrix lifecycle, sync callbacks, and room behavior |
| `bot_runtime_view.py` | One bot generation's runtime state: its run phase and admission fence, and the runtime's shared config, gate, and membership index read through its orchestrator |
| `turns/ingress.py` | TurnIngress - owns one inbound text or media event from its Matrix callback to gate admission, including onboarding commands, approval replies, edits, and commands that bypass the gate |
| `turns/dispatch.py` | TurnDispatcher - owns one turn from the coalescing gate's exit to its recorded outcome: prepare, decide, execute, record |
| `turns/interactive_selection.py` | Admits an answer to an interactive question behind earlier ingress and runs its response after the gate |
| `turns/queued_notice.py` | The queued notice and approval-wait reaction that text, media, and voice ingress share |
| `voice/ingress.py` | VoiceIngress - fixes a voice message's conversation, then readies it behind its sender's lane |
| `voice/readiness.py` | Voice preparation and checkpointing, gated on the router's visible echo |
| `router/duties.py` | What only the router bot does: keep room-backed reply grants current from its sync, set up and welcome the rooms it joins, clean orphaned bot accounts, and emit room-membership hooks |
| `router/relay.py` | Router relay delivery: target readiness, handoff metadata, and visible delivery |
| `ingress_validation.py` | Ingress boundary validation: classify each event's provenance once against the entity registry, handled-id dedup, reply access at the first gate and again under the admission fence |
| `turns/facts.py` | One inbound event's classified provenance (`IngressFacts`), the turn origin derived from it, and the one command rule |
| `inbound_turn_normalizer.py` | Raw input shaping (text, voice, sidecars, media) into canonical turn inputs |
| `conversation_resolver.py` | A turn's thread decided once at ingress, the context read in that thread, and envelope assembly from classified provenance |
| `coalescing/ingress_ticket.py` | `IngressTicket`: one ingress item's lane slot, turn claim, and queued notice until gate admission moves them, released by one idempotent `close` |
| `coalescing/ingress_lanes.py` | Per-(room, sender) receipt-order FIFO delivering resolving ingress (voice/STT readiness) to conversations |
| `coalescing/gate.py` | Live message coalescing gate; ordinary text dispatches immediately, adaptive text waits for its quiet window, and media waits for attachments and a trailing caption |
| `coalescing/batch.py` | Coalesced dispatch batch construction |
| `turn_policy.py` | Pure turn policy: decide ignore, route, or respond for inbound turns |
| `participation.py` | Framework-independent participation state: one immutable decision, concurrent checks, and approval-preserving settlement |
| `participation_hook.py` | The reply's participation check as a pre-request loop hook: a decision request on the reply's exact messages, or the configured judgment backend, ending the run when it stays silent |
| `judgment/` | Backend-independent boolean and choice questions, minimized context, shared execution limits, the bounded HTTP client, and LLM, System One, and OpenAI Decisions adapters |
| `participation_judgment.py` | Bind the participation rubric to an opt-in LLM, TypeSafe, or OpenAI Decisions judge and map its result to a participation decision |
| `mid_turn.py` | Per-response finish-or-wrap-up decisions over immutable queued-message snapshots |
| `mid_turn_judgment.py` | Bind the active request and agent settings to the shared LLM, TypeSafe, or OpenAI Decisions judgment backend |
| `config/mid_turn.py` | Opt-in agent settings for the mid-turn judgment backend and decision instructions |
| `judgment/cold_cache.py` | Decide from a thread's idle time and an opt-in judge whether the thread moves to the agent's cheap model |
| `config/cold_cache.py` | Opt-in agent settings for the cold-cache judgment backend |
| `providers/tool_policy.py` | A decision request's tool policy: `tool_choice` "none" keeps tool schemas and allows no tool use, native tools included |
| `config/participation.py` | Opt-in participation settings for existing thread agents: bounded pause and decision instructions |
| `config/skill_learning.py` | Opt-in agent settings for skill reviews: interval, review model, notices, and archival |
| `dispatch_replay_guard.py` | Replay-guard checks for dispatch sequencing |
| `event_journal/` | Durable ownership of admitted Matrix events, conversation projection, and delivery outbox |
| `event_journal/upgrades.py` | The ordered schema upgrades both backends run inside their schema transaction, against one schema inspector, then the current schema and the conversions that fill it |
| `event_journal/write_queue.py` | The one writer task each backend's writes queue for, from any event loop; the writes queued at once commit in one transaction |
| `response_sources.py` | Immutable response-attempt source identity shared by runtime and persistence boundaries, and the `SourceFate` a response returns for them |
| `reply_lifecycle.py` | Pure rules for durable reply records: every event that changes a reply, its spans, rows, and effects (see [Reply Messages](reply-messages.md)) |
| `reply_presentation.py` | Reply presentation model, its JSON codec, and rendering into a body, trace, and terminal wire status |
| `reply_scope.py` | `ReplyRuntime` and `SpanHandle`: reply claims, span exits, write-ahead, owed writes, and runtime ownership |
| `edit_regenerator.py` | Decides which message edits regenerate a reply and runs each off the room's event lane, as the entity answers now |
| `event_journal/views.py` | The narrow journal views collaborators are typed as, each operation declared once on the view that owns it; `PrincipalStore` inherits them |
| `event_journal/principal_rows.py` | `PrincipalRows`: the backend and principal a view is bound to, and the typed read, write, and recovery-read helpers that pick each operation's lane |
| `event_journal/matrix_delivery_rows.py` | The delivery outbox's transactions: enqueueing replies and final answers, acknowledgement, retirement, and permanent failure |
| `event_journal/replies.py` | `ReplyStore`: reply rules applied inside journal transactions, with their in-transaction effects |
| `event_journal/approval_store.py` | `ApprovalStore`: one principal's approval calls, cards, grants, scheduled calls, and paused-run continuations |
| `event_journal/approval_calls.py` | One row per gated call, whatever owns it, and `resolve_call`, the only writer of a call's decision and its card's terminal edit |
| `event_journal/approval_cards.py` | Approval cards and receipts: reservation, the events that show them, terminal edits, retirement, departure cleanup, and the typed `ApprovalCardView` clicks and recovery read |
| `event_journal/approval_grants.py` | Timed grants: the consent a card offers, the grant it originates, its batch, and revocation edits |
| `event_journal/approval_signals.py` | `ApprovalSignals`: what committed approval writes tell the process, each principal's paused replies that bots' holds follow and each decided call that wakes its waiter and sends its card's terminal edit |
| `event_journal/legacy_approval_records.py` | One-time conversion of an earlier release's per-kind approval tables to one row per call and card |
| `event_journal/legacy_response_attempts.py` | One-time cancellation of the approvals an earlier release left pending, and removal of the tables that kept their reply identity |
| `event_journal/turn_rows.py` | Turn state as rows, agent-scoped: lookups, narrow writes, the one reshape that forms a new source tuple, the agent's redaction record and the eligibility guard every write checks, delivery binding, and retention |
| `event_journal/legacy_turn_rows.py` | One-time conversion of `turn_records` blobs, journal redaction tombstones, and the pre-journal JSON ledger into turn rows |
| `event_journal/legacy_approval_failures.py` | One-time conversion of the free-text failure reasons failing approval continuations stored into their failure kind and detail |
| `event_journal/scheduled_approvals.py` | Stored scheduled tool calls and their one-shot approvals: binding, fire-time arming, withdrawal, claim with its receipt, and outcome |
| `journal_dispatch.py` | Fan admitted journal events out to typed Matrix callbacks and settle the ones that finish |
| `pending_event_worker.py` | Decides when pending journal work runs, and wakes itself again whenever a pass stops early |
| `command_turn_executor.py` | Command execution and durable command/config mutation journals |
| `reaction_dispatch.py` | Durable semantic routing for Matrix reactions, and the `reaction:received` hooks for reactions no built-in consumer took |
| `user_stop_reconciliation.py` | Records a Stop reaction on the reply it stops |
| `visible_response_reconciliation.py` | Visible Matrix response recovery, adoption, and replay reconciliation, and the settlement of the sources a turn's response left unclaimed |
| `turn_store.py` | One agent's turn state for the runtime: awaited reads and writes over `event_journal/turn_rows.py`, the in-process claim registry, and locked source and edit-snapshot preparation |
| `response_runner.py` | Response lifecycle execution: admission, the conversation lock, claiming the reply, preparing the agent or team turn, and streamed or collected delivery; it returns what the turn did with its sources and hands a paused turn to `approval/continuation_runner.py` |
| `reply_turn/inbox.py` | `InboxResponses`: owns detached inbox response tasks, and the turn claim each holds, until they finish, and proves their journal recovery when a shutdown drain cancels them |
| `reply_turn/enrichment.py` | Request enrichment for one reply turn: Matrix targeting context, silent-schedule guidance, timestamped model context, and the account of an interrupted attempt |
| `reply_turn/locked_turn.py` | `LockedTurns`: the settlement every locked reply turn shares (its ending, span exit, and lifecycle finalization), its effects and identity, and `ReplyTurnDeps`, which the response runner and the approval runner both settle through |
| `reply_turn/request.py` | `ResponseRequest`, the values one reply turn runs with, and the helpers every owner of its locked lifecycle shares (correlation, typing, reply authorization) |
| `response_turn.py` | The streaming response-turn driver behind agent and team turns and the collector blocking callers read it with (attempt loop, model-switch continuation, empty-run retry, the turn record and its interrupted row), `ResponseTurnContext` with a turn's inputs, and the run metadata and request-log context a turn binds around its model requests and stream pulls |
| `loop_hooks.py` | The hooks every run or resume installs on the agent loop, composed once in the order the turn applies them; `agent_loop_hooks` and `leader_loop_hooks` name what an agent's and a team leader's run differ by, and `HookTurn` holds what every run takes from its turn |
| `run_storage.py` | Where an agent or team run stores (`RunStorage`, taken from a scope context or bound to a turn), the terminal record it hands over and the error when it hands none, and the persist callback that keeps and stores it |
| `response_attempt.py` | Runs one visible response attempt as its reply span's cancellable task |
| `final_delivery.py` | `TurnEnding`, the one typed value of how a reply turn ended, the delivery outcome that carries it, and the views derived from it: an approval's failure, the `message:cancelled` reason, and the timing label |
| `response_lifecycle.py` | Shared response lifecycle helpers, the response hooks and silent-schedule result record, queued-notice state, and the approval holds it follows from `ApprovalSignals` |
| `execution_preparation.py` | Request-scoped execution preparation for prompts and persisted replay |
| `response_payload_preparation.py` | Execution-side, under-lock assembly of one response's payload from immutable ingress inputs |
| `delivery_gateway.py` | Visible Matrix delivery for already-generated responses (send, edit, finalize) |
| `custom_tools/matrix_message_idempotency.py` | Bounded durable keyed Matrix sends: preparation, receipts, retention, replay, and current authorization checks |
| `personal_room_lifecycle.py` | Personal-room command and membership policy, personal-agent provisioning from a template, target-service routing, reconciliation, and separate rejoin/cleanup retention projections |
| `personal_agents.py` | Onboarding one user's personal agent: name choice, Matrix account registration, then the roster entry |
| `post_response_effects.py` | Shared post-response effects after Matrix delivery |
| `thread_summary/state.py` | `SummaryState`: one walk of a thread's history into trusted summary notices, authorized human pins, and ordinary messages, and the current-title readers listings, exports, and calls share |
| `thread_summary/schedule.py` | The process's per-thread summary baselines and write locks, the next threshold, and the pre-queue gate |
| `thread_summary/generation.py` | The summary model call, its response schemas, and initial tag normalization |
| `thread_summary/notices.py` | Summary notice sends ordered after their predecessors, and the manual and welcome writes |
| `thread_summary/automatic.py` | The automatic pass after a reply: thresholds, pin and resolved checks, initial tags, and the source pin re-check before delivery |
| `file_access.py` | Agent `file_access` resolution and the shared path authorization every path-taking tool opens files through, and the size-checked reads of files that upload tools send |
| `orchestration/config_warnings.py` | Startup and reload warnings for risky but allowed config choices (foreign homeserver authorities, unconfined primary-process tools next to worker code tools, Agent Keys opt-ins without a worker-routed shell or python) |
| `approval/tool_approval.py` | Tool-call approval rule evaluation and public approval API |
| `approval/execution.py` | Agent reconstruction and exact-call execution for persisted native approval continuations |
| `approval/resume.py` | Resuming a persisted paused agent run on the agent loop with exact decisions: its history replay, native history, hooks, and the parent continuation the delegation driver injects |
| `approval/receipt.py` | The trusted approval receipt: the text naming each decided call of one tool-approval continuation, and the request hook that adds it to a reader's messages before its first request |
| `approval/receipt_readers.py` | Which runs read the bound approval receipt: the binding an approval continuation sets around itself, free of model-loop imports so work the loop starts can leave it |
| `approval/tool_owners.py` | Recorded toolkit restoration and exact owner validation for saved approvals |
| `approval/response.py` | Which calls of a paused run need a person: the approval plan of a paused generation and the publication of its cards |
| `approval/continuation_rules.py` | Pure rules for what a paused reply does next: `next_step` over its continuation's state, lease or claim holder, failure kind, claim outcome, and final answer, for each trigger (dispatch, a resume's end, an unavailable owner, a minimal-mode exit) |
| `approval/continuation_runner.py` | `ApprovalContinuationRunner`: a reply's approval lifecycle that `ResponseRunner` hands paused turns to: pausing for approval, resuming the approved run in its reply, minimal-mode CLI waits, and running the steps `continuation_rules` decides, including failure settlement; it settles through `reply_turn/locked_turn.py` and knows nothing of the response runner |
| `approval/manager.py` | Matrix-backed tool approval runtime state: card publication, clicks, deadlines, and recovery over typed card reads |
| `approval/auto_approve_windows.py` | Durable per-conversation `!auto_approve` windows that approve one requester's gated calls without cards |
| `oauth/credential_binding.py` | Canonical OAuth provider and worker-target bindings for browser workflows |
| `oauth/credential_lifecycle.py` | Single transaction owner for scoped OAuth load, refresh, callback publication, invalidation, and reset state |
| `oauth/credential_store.py` | Per-scope SQLite OAuth credential storage, revisions, and reset receipts |
| `oauth/reset.py` | OAuth reset target resolution and requester-bound browser intents |
| `oauth/reset_execution.py` | MCP retirement and durable reset execution |
| `oauth/microsoft.py` | Microsoft 365 OAuth providers sharing one Entra app, with the tenant resolved per runtime |
| `oauth/spotify.py` | Spotify OAuth provider |
| `oauth/legacy_spotify_credentials.py` | Tokens the former Spotify routes stored, adopted once into a scope's OAuth store when that store is created |
| `custom_tools/oauth_connections.py` | Requester-bound agent tool for issuing OAuth reset confirmation links |
| `egress_broker/` | Agent Keys: the egress broker in the primary that adds stored keys or connected-account tokens to worker HTTP(S) requests the worker never sees |
| `egress_broker/env.py` | Whether the broker and its personal page are on, the one rule for which agents use Agent Keys (`egress_credentials` plus a worker-routed `shell` or `python`), and the worker env overlay |
| `egress_broker/service.py` | Broker lifecycle on its own thread, secret resolution per verified worker scope, and the per-call broker env for worker-routed calls |
| `egress_broker/proxy.py` | Authenticated forward-proxy listener: CONNECT interception or blind tunnels, plain HTTP forwarding, and the unmatched-host policy |
| `egress_broker/mitm.py` | TLS interception for hosts with rules: `Host` checks against the CONNECT target and per-request credential injection |
| `egress_broker/_relay.py` | HTTP/1.1 plumbing both listener paths share: request framing, guarded dialing, body streaming, refusals, and audit rows |
| `egress_broker/dial.py` | Guarded upstream dialing: refuses loopback, private, and cloud metadata destinations and connects only to the address it validated, through the operator proxy when the primary has one |
| `egress_broker/rules.py` | Pure rule matching, path restriction checks, ambiguous-path refusal, and credential injection into a header or query parameter |
| `egress_broker/user_services.py` | Services users define per scope, stored beside the scope's keys and merged after the config services into the config the broker matches |
| `egress_broker/secrets.py` | Per-scope egress keys in the primary-only credential store and each service's key-or-account status |
| `egress_broker/oauth_reuse.py` | Per-service opt-ins that let Agent Keys use a connected OAuth account, stored beside the connection they unlock |
| `egress_broker/tokens.py` | Signed, expiring worker proxy tokens that carry the worker scope, agent, and requester |
| `egress_broker/audit.py` | Metadata-only SQLite request log, pruned to 30 days and 100,000 rows |
| `egress_broker/ca.py` | Local broker CA, per-host leaf certificates, the SNI check against the CONNECT host, and the worker trust bundle |
| `api/egress_broker.py` | Dashboard Agent Keys routes: service status, presets, keys, account connect, disconnect, and opt-in, the full request log, and the CA download |
| `api/connections_session.py` | Connections portal sign-in: exchanges a Matrix OpenID token from MindRoom Chat for a portal session cookie |
| `api/connections_session_cookie.py` | Signed, process-local portal session cookies with a fixed one-hour lifetime |
| `api/egress_credentials.py` | Personal `/api/connections/egress` routes: per-agent services and keys, user services, account connections and opt-ins, and the caller's request log |
| `api/egress_status.py` | Service status, preset, and request-log responses and the account-connection gate shared by both egress APIs |
| `config/egress_broker.py` | `egress_broker` config models: services, preset expansion, rules, auth types, and their validation |
| `custom_tools/egress_credentials.py` | `egress_credentials` agent tool, whose presence opts an agent into Agent Keys: which services have a key or connected account in the agent's scope, and where to add missing ones |
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
| `tool_jobs/execution_authority.py` | `JobTool` and `JobCall`, what job admission and grant checks read about an offered function and one call of it, and the application-entry authority checks on the call a task runs under |
| `tool_jobs/provenance.py` | Exact MCP bridge identity for grant checks |
| `tool_jobs/control.py` | Newer human messages that release waits, and cancellation checkpoints |
| `tool_jobs/background_after.py` | The reserved `background_after` argument of managed calls and its validation |
| `tool_jobs/results.py` | Non-executable durable tool-value and rich-artifact serialization |
| `tool_jobs/legacy_results.py` | Job result payloads in the shapes earlier releases saved them |
| `tool_jobs/conversation.py` | The conversation key a reply finds its jobs by, and how long a reply waits for a job's approval |
| `tool_jobs/notices.py` | The notice on a tool result and the turn-start note about jobs that ended |
| `tool_jobs/publication.py` | Each agent's live jobs, published per room as state for the client jobs bar |
| `tool_jobs/client_cancel.py` | Client cancel events, `!jobs` and `!cancel_job`, and a left room's jobs |
| `tool_jobs/approvals.py` | Approval cards a job posts and owns for its child's or its own gated call, denied when the job ends early |
| `tool_jobs/follow_ups.py` | A follow-up's journal source, identity, streak, and envelope |
| `tool_system/construction.py` | Selected toolkit factory and digested constructor identity |
| `tool_system/filters.py` | Shared include/exclude function policy |
| `custom_tools/job.py` | Reserved job discovery and control tool |
| `orchestration/server_state.py` | Each entity's runtime origin and worker computer, published per room as state so MindRoom Chat finds its server |
| `orchestration/services.py` | `RuntimeService`, the sync-and-stop shape of the long-running services that follow the active config |
| `orchestration/tool_job_runtime.py` | Managed job lifecycle: recovery, revocation, published jobs state, follow-ups, and retention |
| `orchestration/script_runtime.py` | Background script lifecycle: startup recovery of preserved runs, reload revocation, maintenance passes, the published worker lease, and when script tools are served |
| `custom_tools/script.py` | `script` agent tool: start, inspect, cancel, and list the requester's background scripts |
| `script_runs/manager.py` | Starts, polls, and cancels script runs in a worker or unsafe-local, and finalizes their cleanup |
| `script_runs/store.py` | The primary-only SQLite store of script runs and their tool-call receipts, with atomic state transitions |
| `script_runs/legacy_schema.py` | Script-run columns that older releases' databases lack |
| `script_runs/models.py` | Run and call records, run states, and the run ID, supervisor handle, and worker key a run derives |
| `script_runs/reasons.py` | The durable reasons a run was cancelled or interrupted |
| `script_runs/policy.py` | The tool grants a launch captures and the current tool a script call resolves to |
| `script_runs/broker.py` | Authenticates a script's tool calls by capability and runs each claimed call under the run's current authority |
| `script_runs/recovery.py` | The recovery signature that lets a restarted primary adopt a still-running worker script |
| `script_runs/supervised.py` | Launch, status, and signal of one script process through a shell supervisor, shared by unsafe-local runs and worker routes |
| `script_runs/launch_contract.py` | Script environment names, size limits, and snapshot layout for launchers, the shim, and the SDK, plus the tool-call argument digest the SDK and the broker share |
| `script_runs/shim.py` | Import-light entry point that verifies a run's snapshot inside the script process and runs its source |
| `script_runs/wire.py` | The primary-worker script protocol: its version, request and response models, normalized receipts, and error |
| `script_runs/worker_client.py` | The primary's HTTP client for a worker's script routes |
| `api/sandbox_runner_scripts.py` | Worker routes that launch, poll, and signal a run under the worker's supervisor |
| `api/script_gateway.py` | Capability-authenticated HTTP gateway scripts call tools through |
| `script_sdk.py` | The standard-library `MindRoomTools` client a script calls tools with |
| `workspaces.py` | Agent workspace scaffolding, template seeding, and context file resolution |
| `worker_computer/worker_browser.py` | Serializes dedicated-worker headless browser calls, retains browser resources, and owns configuration/environment retirement and shutdown cleanup |
| `agents.py` | `create_agent`: builds one configured agent's toolkits, prompt, model provider, and surface into an `AgentBuild` |
| `config/` | Pydantic models for YAML config parsing (root model in `config/main.py`) |
| `config/resolution.py` | What one agent, team, or scope gets from the config, every inherited value filled in (`resolve_scope`, `resolve_agent`, and one function per fact) |
| `config/personal_rooms.py` | Opt-in personal-room settings and validation for commands, aliases, and message templates |
| `config/personal_agents.py` | `personal_agents` templates, the roster store, and expansion of roster entries into ordinary agents before config validation |
| `config/slack.py` | The top-level `slack:` section: the email domain and Matrix user ID template that map Slack workspace members to Matrix users; also validates the `slack` tool's `channels` and `room` across agents and lists the agents that answer Slack channels |
| `routing.py` | Intelligent responder selection when no agent or team is mentioned |
| `routing_judgment.py` | Opt-in bounded System One or OpenAI Decisions responder selection, with explicit no-fit outcomes and existing LLM routing fallback |
| `teams/formation.py` | Forming a team from one turn: who was asked, who is eligible, which members can be materialized, and the execution-time mode choice for an ad hoc team |
| `teams/members.py` | Resolving a team's exact members without silent shrinkage, the leader's and members' model aliases for a turn, and `build_team_spec`: the leader's model, prompt, job controls, call budget, and history settings with each member's roster entry and the build its first delegation makes |
| `teams/spec.py` | `TeamMode`, `TeamMember` with its `MemberBuild`, and `TeamSpec`: what a team run is built from |
| `teams/prompt.py` | The leader's system messages, rendered like an agent's: the stable one holds the team's identity, the markdown line, the roster and mode block, and tool instructions; the turn one holds the date, session context, and enrichment |
| `teams/delegate_tools.py` | `delegate_task_to_member` and `delegate_task_to_members`, the member result texts, the pause a paused member propagates to the team run, and the answer its resumed run gives the waiting call |
| `teams/runtime.py` | `run_team` and `continue_team` over the agent loop: members as child runs, member decisions routed by member and call, team usage |
| `teams/presentation.py` | The team reply document: header, member slots, consensus, tool trace, the durable `team_stream` snapshot an approval resume restores, the live document a resumed run publishes, and the document's text without tool markers that a reply records |
| `teams/legacy_team_presentation.py` | Team reply snapshots of version 2, which keyed member slots by Agno's url-safe member id, read as version 3 when an approval resume restores one |
| `teams/envelope.py` | `team_response_stream`: prepare a team turn, run its leader through the turn driver, and render the reply; `run_prepared_team` runs an OpenAI-compatible API team turn under the same leader hooks and storage |
| `teams/leader.py` | `LeaderRun`: how a team's leader runs in one turn or resume, its storage, hooks, request settings, and model, which a live turn and a saved approval's resume both build to start or continue the leader's run |
| `teams/attempt.py` | One attempt of a team run, a turn's or a resume's: the reply document its events build, the records the run hands over, the text an interrupted turn records (its document without tool markers), and its run metadata |
| `teams/resume.py` | Resuming a paused team run: `TeamResume` routes decisions to the calls the team waits for, `drive_team_delegations` runs a member's delegations and continues with their results, `continue_paused_team_run` rebuilds the team from a saved approval, and the document a team pause shows |
| `agent_policy.py` | Execution-policy derivation from one typed input, `PolicyInputs`, for validated agents, dashboard drafts, and worker config files |
| `agent_cli/minimal_agent.py` | Minimal mode: the bootstrap prompt and context documents from the build surface, and the turn's one provider-facing Bash tool over the build's catalog |
| `agent_cli/checkpoint.py` | Minimal Bash's provider batch, saved as a `before_dispatch` hook before any Bash call takes effect |
| `agent_cli/cli_shell_agent.py` | Standard agents whose native shell commands call their other tools through `mindroom-agent`, bound per attempt |
| `agent_cli/` | Response-owned CLI grants, call admission, shell access, discovery, and result projection |
| `agent_modes.py` | Conversation-scoped standard/minimal selection persistence |
| `agent_cli/minimal_mode_preflight.py` | Minimal-mode eligibility for `!mode` and minimal subagents, reported as one actionable checklist |
| `commands/mode_commands.py` | Authorized agent mode selection with canonical session scope and deployment preflight |
| `api/agent_cli.py` | Authenticated transport for response-owned CLI operations and live call receipts |
| `agent_cli/cli_approval_recovery.py` | Exact saved CLI approval execution through rebuilt canonical bindings and ordinary interrupted-response recovery |
| `agent_cli/cli_approval_waits.py` | Response-owned CLI approval waits, exact journal claims, and the end of each scope, which it reports to the approval runner |
| `tool_system/agent_tool_calls.py` | The live turn's catalog of qualified tools and their serialized execution through the build's tool policy |
| `tool_system/tool_access.py` | Shared qualified tool identities, discovery, schemas, and local argument validation |
| `memory/` | Agent and team memory over Mem0 or Markdown files; an agent's lives at its state root or workspace, every team's Mem0 memory in one store at `team_memory/`, and each team's file memory at `team_memory/<team scope>/` |
| `memory/_policy.py` | Memory scopes: the team scope ID, the team memory root and each team's file directory, the team scopes an agent reads, and the backend an agent or team uses |
| `memory/legacy_team_memory.py` | One-time startup merge of the team memory copies earlier releases kept in every member's storage |
| `memory/_entrypoint_snapshots.py` | In-process copy of each conversation's inlined `MEMORY.md`, reused while that copy only gains lines (not when a capped file drops its oldest kept entry) and that conversation's prompt cache may still be warm |
| `memory/auto_flush.py` | Background passes that turn file-memory agents' new finished turns into daily notes, with per-conversation progress below `tracking/`, and the service that runs them while auto-flush is enabled |
| `memory/legacy_auto_flush.py` | Auto-flush state earlier releases wrote: the reply path's queue file, removed once, and progress entries and note tags in creation order, read as log positions when auto-flush next reads the conversation |
| `knowledge/file_memory_knowledge.py` | Shared resolution for agent file-memory semantic knowledge overlays |
| `memory_scope_ids.py` | Cycle-free canonical agent memory scope identifiers |
| `history/conversation_text.py` | What stored turns said, for background model calls and compaction hooks: which turns and messages count and who said each |
| `embeddings/` | The `Embedder` interface semantic indexes embed queries and documents through, its OpenAI-compatible, Ollama, and local sentence-transformers implementations, embedder health, and vector compatibility signatures |
| `knowledge/` | Knowledge base / RAG file indexing with watcher |
| `knowledge/file_listing.py` | Which files belong to a knowledge base: include patterns, traversal, symlink-safe inclusion rules |
| `knowledge/collection_lifetime.py` | Compatible publication selection, shared reader locks, and exclusive collection reclamation |
| `knowledge/search.py` | What an agent searches: its published sources, the `search_knowledge_base` tool and its results, and the `<knowledge_base>` prompt block |
| `knowledge/readers.py` | The Agno document readers and chunkers knowledge indexing reads files with; the only knowledge module that imports Agno |
| `knowledge/collections.py` | Collection naming and ownership for one knowledge base: opening, writing a file's chunk rows, reclaiming superseded collections and legacy Chroma storage |
| `knowledge/vector_store.py` | One collection as one SQLite file: chunk rows, float32 vectors, an FTS5 keyword index, and the collection's own claims, failures, and lifecycle, changed in one transaction with its rows; in-process search |
| `knowledge/index_metadata.py` | A base's two state documents: the publication, written only by the publisher under the source-root lock, and the refresh status, written only by the runtime |
| `knowledge/manager.py` | The candidate lifecycle of one semantic base: which collection to resume, copy, or start, reconciling it with the source, and publishing it |
| `knowledge/file_indexer.py` | One refresh's file indexing: read, chunk, batch-embed, and write each file with its claim, with that refresh's failure evidence |
| `knowledge/corpus_signature.py` | File signatures, the stat-first corpus scan, and the corpus hash a publication records |
| `knowledge/legacy_index_state.py` | Startup conversion of knowledge state earlier releases kept outside each collection |
| `knowledge/index_reader.py` | Published index handle: embeds a query and searches the current compatible publication on a worker thread |
| `knowledge/git_source.py` | The Git checkout a knowledge base indexes: clone, fetch, force-align, LFS hydration, credential injection |
| `knowledge/refresh_runner.py` | One knowledge refresh: the child's work under the source-root lock, and the runtime side that spawns it and records its outcome in the refresh status |
| `knowledge/refresh_scheduler.py` | The process's one refresh scheduler: which bases are refreshing, one queued request each, the concurrency limit, and on-access cooldowns |
| `knowledge/refresh_locks.py` | The source-root lock that serializes one source's refresh and mutation work, in-loop and across processes |
| `knowledge_graph/scope.py` | Which graph an agent or private instance owns, named in code from its state root, embedder, and generation |
| `knowledge_graph/store.py` | The runtime's one Graphiti client and its episode, fact, and entity operations; health and the store registry |
| `knowledge_graph/clients.py` | Graphiti's LLM, embedder, and reranker clients backed by MindRoom's model and embedder construction |
| `knowledge_graph/usage.py` | Which conversation an extraction call's tokens are charged to |
| `knowledge_graph/extraction.py` | What Graphiti is told when it extracts an episode: the entity types and the extraction instructions, built-in, configured, and the names of the episode's known entities |
| `knowledge_graph/embedded_server.py` | The embedded FalkorDB server MindRoom starts, stops, and recovers after a crash |
| `knowledge_graph/episodes.py` | Stored runs rendered as `conversation` and `tool_results` episodes, and thread classification |
| `knowledge_graph/names.py` | The one name of each known agent, person, and `knowledge_graph.aliases` entry: speaker labels, names listed for extraction, and lookups |
| `knowledge_graph/ingestion.py` | Background ingestion passes over several graphs at once, with per-kind markers, backoff, and dropped episodes |
| `knowledge_graph/legacy_ingestion.py` | Ingestion markers earlier releases kept as run keys in creation order, read as log positions when ingestion next reads the conversation |
| `knowledge_graph/runtime.py` | Starts and stops the store and ingestion worker as agents enable the graph |
| `config/knowledge_graph.py` | The `knowledge_graph` section: extraction model, extraction instructions, ingestion settings, aliases, and generation |
| `custom_tools/knowledge_graph.py` | The `knowledge_graph` tool: lookups, and the edits `knowledge_graph_edits` allows; also the graph as a `search_knowledge_base` source |
| `tool_system/skills.py` | OpenClaw-compatible skill loading from skill roots and workspaces, and the read-only `SkillsToolkit` with its prompt block |
| `tool_system/skill_usage.py` | Workspace skill usage records the skill learner's archival reads as an inactivity clock |
| `tool_system/plugins.py` | Plugin generations: loading, publishing, and disposing them, candidate validation in a separate module namespace, and `config_plugins`, one config's tools, hooks, skill roots, and OAuth providers from one published generation |
| `tool_system/registry.py` | `ToolRegistry`, the tools one config can build (built-ins, its loaded plugins' tools, its MCP servers, and the tools of plugins that failed to load as unavailable), the built-in tools, and the registration scope plugin modules register into |
| `plugin_api.py` | The supported import surface for plugin code, re-exporting entry points from the modules that own them |
| `tool_system/google_workspaces.py` | Workspace-specific Google OAuth provider construction and tool registration |
| `tool_system/atlassian_connections.py` | Additional Atlassian Cloud connection providers and prefixed tool registration |
| `scheduling/tasks.py` | Cron and natural-language task scheduling |
| `scheduling/runtime.py` | The one owner of scheduled-task runners and of when each runner may use its task's owner bot |
| `scheduling/recurring_schedule.py` | Durable checkpoints for recurring task timers and their pending Matrix trigger |
| `scheduling/ownership.py` | Which bot runs a scheduled task, decided once at save time from the room's full state |
| `scheduling/errors.py` | `SchedulingError`, the one failure a scheduling operation reports, with subtypes for a missing task and failed task storage, which chat, the scheduler tool, and the schedules API each render |
| `scheduling/scheduled_tool_calls.py` | Resolving a scheduled call on the agent's own live tools and running it once by task ID with its approval |
| `scheduling/executor.py` | Fire one scheduled task: hook emission, visible or silent Matrix delivery, and failure notices |
| `scheduling/scheduled_run_records.py` | Agent-workspace JSON receipts for silent scheduled runs |
| `tools/` | Built-in tool registrations and their helpers; importing the package registers every built-in tool |
| `tools/lumalabs.py` | Configurable SDK model binding for both inherited Luma video-generation methods |
| `tool_system/dependencies.py` | Auto-install per-tool optional dependencies at runtime |
| `ai.py` | The agent turn: `stream_agent_response` runs an agent through the turn driver, `ai_response` collects that stream, and delegated children run through it too |
| `providers/credentials.py` | Which key a configured model authenticates with: the dashboard key and the missing-key checks the CLI and dashboard report |
| `providers/key_checks.py` | The request that proves a provider API key valid, which `mindroom doctor` and the dashboard's provider setup send |
| `providers/names.py` | `provider_route`, the route a configured provider value selects with aliases such as `gemini` resolved, which model dispatch and the wire adapters compare, and `ROUTE_PROVIDERS`, the provider name of each route the provider registry builds, whose routes config validation accepts as a model's `provider`, in a module that imports nothing |
| `providers/identity.py` | `model_identity`, a configured model's provider name and effective model ID read from its config alone, which every provider builder and pricing take |
| `providers/anthropic/messages.py` | The Claude adapter (`ModelProvider`) for the direct API, Vertex AI, and Bedrock Mantle routes, with their client factories, error mapping, and `build_provider` |
| `providers/anthropic/settings.py` | Claude route settings parsed from `extra_kwargs` under Agno's field names, and the keys each route accepts |
| `providers/anthropic/wire.py` | Claude Messages API formatting of messages and tools, and parsing of the finished response into an assistant message that stores its blocks once, in request shape |
| `providers/anthropic/stored_turns.py` | The blocks a stored Claude assistant turn replays: its stored `claude_blocks`, or blocks rebuilt from its message fields, which never take a cache breakpoint |
| `providers/anthropic/legacy_stored_turns.py` | Claude turns stored before `claude_blocks`: response-shape blocks read in request shape, and older turns rebuilt from their fields |
| `providers/anthropic/repairs.py` | Replay-safe shapes of Claude blocks (citations, tool-search blocks) and request-scoped body repairs: unavailable tool references, tool-result order, inline media budgets |
| `providers/anthropic/prompt_cache.py` | The wire preparation of one Claude request body: history repairs, deferred tool search, tool choice, and the prompt-cache ladder |
| `providers/cache_ladder.py` | The Claude prompt-cache ladder primitives (breakpoint markers, message rungs, the tools marker) shared by the Claude adapter and the OpenRouter Claude ladder |
| `providers/anthropic/native_compaction.py` | Claude native compaction: the compaction edit, the checkpoint replay projection, and the checkpoint recorded on each response |
| `model_catalog.py` | Allowlisted model metadata, Matrix icon upload/cache, and catalog revision |
| `model_catalog_receiver.py` | Router discovery admission, authenticated responses, and scope/lifetime checks |
| `model_selection.py` | Structured model request/result values and frozen acknowledgement metadata |
| `model_selection_scope.py` | Current joined membership and readable-root eligibility for model selection |
| `ai_runtime.py` | The queued-message notice a reply's loop appends after a tool batch, its mid-turn judgment, and the empty-response notice |
| `providers/openai/` | OpenAI-family adapters: one Chat Completions adapter for every OpenAI-compatible route, one Responses adapter for OpenAI and Codex, `build_provider`, the OpenRouter Claude cache ladder, OpenAI server-side tool search tagging, the Responses explicit cache breakpoint on the stable system message and ordered output replay, and the history budget's size of explicitly replayed Responses input (`replay_estimate.py`) |
| `providers/openai/legacy_response_replay.py` | The hosted tool-search items that Responses replies stored without an ordered output kept apart, read for replay |
| `providers/pipeline/` | Stages composed around a provider's `stream()`: portable deferred tool search (`llm/tool_search.py`), media fallback, downloads of the media URLs a route does not send (`remote_media.py`), the malformed tool-call guidance retry, stream retry with the idle limit, the typed safeguard refusal, and request logging |
| `providers/registry.py` | `build_model_provider`, the only way a configured model is built: the dashboard key, the family adapter, and the request pipeline around it with the model's stream idle limit; `check_extra_kwargs` rejects, at config load and without loading a provider SDK, `extra_kwargs` keys the model's route does not accept |
| `providers/gemini/` | Gemini adapter for the Gemini API and Vertex AI: contents, Files API uploads, Google Search grounding, decision-request config, reply parsing, and the `extra_kwargs` a Gemini model accepts (`settings.py`, which loads no Google SDK) |
| `providers/ollama/` | Ollama adapter over the native chat API, the `extra_kwargs` an Ollama model accepts (`settings.py`, which loads no ollama SDK), and the Ollama SDK's failures as normalized provider errors, which the adapter and the Ollama embedder share (`errors.py`) |
| `providers/synthetic_adapter.py` | Synthetic provider streaming seeded Lorem Ipsum and shell calls |
| `providers/model_http_client.py` | The runtime-owned HTTP client the OpenAI and Anthropic SDK clients send their requests through |
| `providers/prompt_cache_key.py` | Stable prompt-cache routing keys derived from execution identity |
| `providers/extra_kwargs.py` | Parses a model's `extra_kwargs` into its adapter's settings, rejecting unknown keys, and reports the non-secret settings that request logs record as model params |
| `providers/errors.py` | One classification of provider statuses, context-window wording, and transport failures into the normalized provider errors, shared by every adapter |
| `providers/assistant_turn.py` | What adapters share to build a turn: the `mindroom_model` provenance stamp and timings, the progress event of a provider chunk without events, the events that end a finished turn, and the split of a leading inline `<think>` block into reasoning |
| `llm/request_context.py` | The per-request logging context (agent, session, correlation id) bound around a model request and read by request telemetry |
| `providers/media_files.py` | Reads the attachment files every wire sends; a file that cannot be read raises `InvalidRequest`, so the media fallback replaces that file with its note |
| `budgets/pricing.py` | Per-model USD pricing of token counters, keyed by the provider and model ID usage records |
| `budgets/spend.py` | Month-to-date per-requester spend derived from retained usage |
| `budgets/caps.py` | Per-user monthly caps kept as runtime state under the storage root, written atomically and read lock-free |
| `budgets/monitor.py` | Month-to-date spend from background usage scans plus each finished reply's cost, the per-user cap store, and the fallback-model decision `turn_model.py` applies |
| `config/budgets.py` | Opt-in budgets: default monthly cap and fallback model |
| `session_storage/agent_storage.py` | Opens an agent's, a team scope's, or the system usage ledger's conversation store, naming its owner, and prepares each database file once per process: the preflight archive check, the link-refusing connect, the tables of a new database, the conversion of one in an older layout, and the layout version upgrade |
| `session_storage/store.py` | The conversation store: one SQLite database of sessions, runs with a row per stored message and per tool execution, summary rows, and the usage ledger, with full run saves refused once a run is final and single facts patched onto stored rows, each operation in one transaction on its own connection |
| `session_storage/schema.py` | File and table names, a team scope's database name, identifier quoting, table and column listing, and the layout of a conversation database: the sessions table, the six `*_log_*` tables, the meta row naming the owner, and the usage ledger with its upsert |
| `session_storage/summary_rows.py` | The conversation log's summary rows: compare-and-append of compaction chunks and mid-turn folds, the event ids each row covers, the tool calls folds took, and edit and redaction cuts with the snapshot a cut is decided from |
| `session_storage/usage_reader.py` | Read-only reads of a conversation database's sessions, owner, layout version, and usage ledger for usage reports, returning what is stored unchecked for the report to validate |
| `session_storage/codec.py` | A run record to its run row, message rows, and tool-execution rows and back, with the JSON keys stored runs carry, and the JSON decode that reads worker-written text without failing |
| `session_storage/types.py` | `ConversationSession` and the other values the store reads and writes |
| `session_storage/lanes.py` | One FIFO lane per conversation database for storage operations started from async code |
| `session_storage/legacy_agno2_runs.py` | Data older Agno releases stored: Agno 2 run blobs, rows without a position, and odd timestamps, which the conversion reads, and session JSON columns encoded twice, which the conversion reads and stores once |
| `session_storage/legacy_tool_results.py` | Tool results an earlier release stored combined in one message, split into one message per result when a database converts |
| `session_storage/legacy_agno3_conversion.py` | The one-way conversion of an Agno-layout conversation database to storage v2, run by a store open that finds an older layout and by the startup sweep over the session state root |
| `session_storage/legacy_agent_session_owners.py` | Agent session rows and runs that name an id other than their agent, set to the agent once, as layout version 3, by the agent's store open and the startup sweep |
| `session_storage/legacy_session_columns.py` | The layout version upgrade: session rows that named their kind and owner and held their state inside `session_data` convert once to layout version 4, whose meta row names the owner, with the team owner the startup sweep reads from the rows, and every database then takes the later steps to the current version |
| `session_storage/legacy_paused_runs.py` | Paused runs earlier releases stored with a paused notice as their content, Agno's summary block in their system message, or delegation calls that do not name the member run they wait on, rewritten once at layout version 6 and at the end of the Agno-layout conversion |
| `session_storage/legacy_conversion_backup.py` | The `<name>.db.pre-storage-v2` and `<name>.db.pre-storage-v4` copies a database gets before its conversions, after a free-space check, through no-follow descriptors |
| `session_storage/legacy_agno3_schema.py` | The Agno layout's table names, DDL, run-row decoder, and member-subtree delete, read only by the conversion and its Agno-era steps |
| `session_storage/legacy_usage_ledger.py` | The conversion's step that seeds the usage ledger of an Agno-layout database without one from its run rows and Agno 2 blobs |
| `session_storage/legacy_usage_totals.py` | The layout version 7 step that keeps the usage stored session totals held beyond their run rows as one undated `pre_ledger` ledger row |
| `session_storage/legacy_scope_metadata.py` | The layout version 7 step that moves the owner's per-scope force-compaction flag and preserved seen ids into one metadata value |
| `session_storage/legacy_run_requirements.py` | The layout version 8 step, and the Agno-layout conversion's decode, that fold each paused run's stored requirements into the tool rows they copy, so a paused call is stored once |
| `session_storage/legacy_compaction_state.py` | The conversion's step that turns earlier compaction state, the compaction archive, and unpositioned run rows into summary rows and positioned runs before they convert |
| `history/mid_turn_compaction.py` | Compaction between the model requests of one reply: request sizing, folding, and the scoped or run-local summary |
| `history/turn_recorder.py` | `TurnRecorder`: what one turn did, which the turn driver writes, and the interrupted-replay snapshot the driver stores when the turn stops |
| `history/legacy_stored_tool_calls.py` | Tool-call shapes earlier releases stored, which the conversion to storage v2 stores in the current shape in every message a converted run replays |
| `history/summary_message.py` | The compaction summary as a user message: its text, its scope or run-local marker, and the marker that replays the scope's summary after a mid-turn compacted prompt |
| `history/session_runs.py` | The follower background readers share: a conversation's Matrix turns in log order, read in one snapshot with what a redaction would cut, the due scan, backoff, and where a conversation never read starts |
| `history/` | Persisted history: what a request replays from stored runs, the replay budget, compaction before and during a reply, summary rows, and interrupted-turn replay |
| `history/request_messages.py` | A model request assembled from a scope's stored runs, its compaction summary, and the current turn |
| `history/replay.py` | Materializes, estimates, and fits persisted history within one replay budget |
| `history/runtime.py` | Prepares a scope's history before a reply (replay plan and compaction check) and runs its mid-turn and run-local compactions |
| `history/compaction.py` | Scoped compaction: summarizing a scope's runs, or a running turn, into its summary rows |
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
| `delegation/targets.py` | Turning a `run_subagent` or `continue_subagent` call into the child it starts or continues, for both the direct tools and the native driver |
| `delegation/legacy_records.py` | Delegation records whose working state lived only in their workspace files, left as they were |
| `delegation/legacy_snapshots.py` | Delegation child snapshots stored without a field a later release added, rewritten once in subagent session files at startup and in runs' metadata as conversation layout version 5 |
| `credentials/manager.py` | Credential stores (CredentialsManager) and `credential_layers`, the one resolver of where a service's credentials are read and written for a target |
| `credentials/policy.py` | Each credential service's placement per worker scope, and whether workers may receive it by grant or lease |
| `credentials/environment.py` | Credentials the environment declares (provider keys, bootstrap values, seeds), read by the primary under its saved ones |
| `matrix/` | Matrix protocol integration (client, users, rooms, presence, provisioning, message formatting) |
| `matrix/state.py` | `matrix_state.yaml` accounts, rooms, space, and server emoji room, changed only through `update_matrix_state` |
| `matrix/large_messages.py` | Large-message sidecar storage and retrieval for oversized Matrix payloads |
| `matrix/server_features.py` | The unstable features a homeserver lists in `/_matrix/client/versions` |
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
| `matrix/projected_thread_membership.py` | Thread-root proofs for tools and outbound mutations, read from the conversation projection like turns |
| `matrix/identity.py` | Matrix ID parsing and utilities |
| `matrix/mentions.py` | Matrix mention formatting |
| `matrix/member_display_names.py` | Current member display names snapshotted from the synced nio room cache for model-facing `<msg>` tags |
| `matrix/typing.py` | Typing indicator utilities |
| `matrix/avatar.py` | Avatar management |
| `commands/` | Chat command parsing (`!help`, `!schedule`, `!config`, etc.) |
| `commands/config_commands.py` | Chat-based config commands (`!config`) |
| `commands/config_confirmation.py` | Interactive config confirmation workflows |
| `voice/handler.py` | Voice message download, transcription, mention normalization, and ASR cleanup |
| `voice/openai_realtime_speech.py` | OpenAI text-to-speech through the Realtime API, shared by voice messages, the `openai` toolkit, and cascaded calls |
| `matrix_rtc/realtime_tts.py` | LiveKit TTS for cascaded calls on `gpt-realtime` models |
| `matrix_rtc/audio_process.py` | Main-process side of a GPT-Live call whose media runs in its own process, plus the idle process started ahead of the next call |
| `matrix_rtc/audio_child.py` | Child process that runs one GPT-Live call's LiveKit room, frame encryption, and agent session away from the MindRoom event loop |
| `matrix_rtc/audio_protocol.py` | Length-prefixed JSON frames between MindRoom and a call's audio process |
| `tool_system/agent_tools.py` | The toolkits one agent may use per turn (`AgentTools.candidates`) and where each runs |
| `tool_system/sandbox_proxy.py` | The one tool placement decision (`ToolPlacement`) and the proxy that routes worker calls |
| `api/sandbox_request_cancellation.py` | Stops an in-flight sandbox runner request when the primary stops waiting for it |
| `api/sandbox_execute_stream.py` | Streams one runner call's progress and keep-alives before its result, and stops the call when the primary hangs up |
| `tool_system/progress.py` | Live progress a running tool call reports to whatever owns it |
| `tool_system/dedicated_threads.py` | One thread per blocking call of a background job or worker proxy call, outside the bounded shared pools |
| `workers/in_flight.py` | Workers with a call in flight, which idle cleanup refreshes before it runs |
| `workers/protocol.py` | The typed requests and responses the primary and its sandbox runners exchange, and the worker protocol version both check |
| `workers/backend_kinds.py` | The worker backends `MINDROOM_WORKER_BACKEND` can select, the one table of its accepted values, and which backends are dedicated |
| `workers/progress.py` | Warmup progress of the workers a dedicated Docker or Kubernetes backend starts, sent to every caller waiting on the same worker |
| `workers/backends/dedicated_launch.py` | The runner environment and startup manifest every dedicated Docker or Kubernetes worker starts with, from its backend's container layout |
| `shell/output_capture.py` | Bounded shell output spools, completion validation, and atomic output-file publication |
| `shell/execution.py` | Shell command execution core: spawning, output buffering, stopping at a timeout, and the handle registry of Desktop shell and script runs |
| `shell/supervisor.py` | Worker-local shell supervisor process that runs shell commands and script handles for sandbox request subprocesses, stopping a command whose relay dies |
| `streaming.py` | Streaming state machine: placeholder, progressive edits (durable checkpoints while live frames run), tool traces, reasoning, and the terminal write of a stream that finished; a stopped or failed stream raises for its span exit to end |
| `live_frames/frames.py` | The `io.mindroom.live` frame format: snapshots and UTF-16 splice deltas of a reply's live documents, and the durable checkpoint watermark key |
| `live_frames/publisher.py` | One streaming reply's frame loop: a frame per tick when its documents changed, periodic snapshots, and the watermark its durable writes carry |
| `live_frames/transport.py` | Whether the homeserver relays live frames, and sending one frame, Megolm-encrypted in encrypted rooms, over the MSC2477 ephemeral route |
| `prompts.py` | Built-in prompt defaults and prompt override registry |
| `attachments/storage.py` | Attachment persistence, registration, and context-scoped resolution |
| `attachments/ids.py` | Leaf attachment-ID helpers kept free of matrix-client imports |
| `attachments/media.py` | Convert attachment records to the model media values in `llm/media.py` |
| `media_inputs.py` | Shared media-input container passed across bot, teams, and AI layers |
| `api/` | FastAPI REST API (dashboard, credentials, OpenAI-compatible endpoint) |
| `api/app_state.py` | One API app's state: what it owns itself, and the one `EmbeddedRuntime` value `mindroom run` lends it while it serves |
| `api/auth_user.py` | The typed login of one API request and the one rule for which Matrix user it acts as |
| `api/open_access.py` | Host allow-list and browser-origin guard for requests served without a credential (open dashboard auth, unauthenticated `/v1`) |
| `api/slack_events.py` | Public `POST /api/slack/events`: verifies Slack's signature, answers `url_verification`, and delivers events within Slack's 3-second limit, finishing slower deliveries in the background |
| `api/request_body_limit.py` | Pure ASGI middleware answering 413 for dashboard API request bodies over 16 MiB, except knowledge uploads |
| `api/validation_errors.py` | 422 handler for the dashboard API and sandbox runner that returns FastAPI's validation error detail without the rejected request values |
| `api/budgets.py` | Dashboard and budget-service routes for budget status and setting or clearing one user's cap |
| `api/usage_export.py` | Application-scoped usage-export preparation: one background scan, a bounded cache for daily/request-detail variants, committed-generation validation, and non-blocking shutdown cleanup |
| `custom_tools/` | Built-in custom tool implementations (gmail, calendar, scheduler, etc.) |
| `custom_tools/microsoft_graph.py` | Shared Microsoft Graph client for the Microsoft 365 tools: requester tokens, error codes, and workspace downloads and uploads |
| `custom_tools/todo_state.py` | Leaf storage, the stored item shape and its strict reader, and actionability primitives for native per-thread todo state |
| `custom_tools/todo_poke.py` | Native scanner and background worker that wakes idle agents with actionable assigned todos |
| `custom_tools/todo_template_render.py` | Full-Jinja rendering of workspace todo templates in a short-lived, memory- and CPU-limited child process |
| `custom_tools/calculator.py` | Agno calculator with bounded `factorial()` and `is_prime()` arguments |
| `custom_tools/sleep.py` | Agno sleep toolkit with pauses capped at 300 seconds |
| `thread_export/workspace_sync.py` | Always-on debounced runner that keeps `<workspace>/thread_exports/` current through the live bots' clients and journal principals |
| `background_tasks.py` | Background task management for non-blocking operations, and `SupervisedLoop`, which restarts a crashed long-running loop with backoff |
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
| `desktop/bridge_session.py` | Opens, starts, and closes one Desktop bridge run (Matrix device, capability providers, bridge, admission, and workers) in one order, shared by the app helper and the terminal |
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
| `tool_system/metadata.py` | Tool construction from a `ToolRegistry`, authored override validation, and metadata export |
| `tool_system/runtime_context.py` | Shared runtime ContextVar for tool calls (including attachment scope), the identity a call runs as (`current_execution_identity`, detached and team-member scopes), and the hook bindings a turn carries |
| `tool_system/tool_function.py` | The tool values, free of Agno: `ToolFunction`, the only tool shape the agent loop sees (finished schema, the gate that says why a call waits, the stop flag, entrypoint, what each injected parameter receives) and the owner of how a call of it pauses, `AdaptedToolkit`, `ToolValue`, a finished tool value's text and media, and `ToolOutcome`, a call's value with its stop and failure flags |
| `tool_system/authoring.py` | MindRoom's Agno-free tool authoring API, which MindRoom's own tools use and plugins import: `Toolkit`, `@tool`, `passthrough_tool` for a function offered with the schema it authors, `current_call()` for the call a function runs for, and `toolkit_functions()` for a toolkit's callables by name as a run calls them |
| `tool_system/tool_schema.py` | Derives the description and parameter schema of every tool function whose signature gives them, MindRoom's own and Agno toolkits' alike, from its docstring and annotations, as Agno derives them for the annotations in use except that `Any` and `object` take any JSON value and `*args` and `**kwargs` stay out of the schema, and validates a call's arguments against them |
| `tool_system/agno_toolkits.py` | Adapts Agno and owned toolkits and functions to `ToolFunction`, applying the function filters, output files, and worker stand-ins their builder configured, and, besides the vendor toolkit subclasses and the Google auth binding in `oauth/agno_compat_google_auth.py`, is the only module that looks inside an Agno toolkit: its function names, one function by name, renames, and connection; builds the Agno-shaped call-time arguments (run context, actor stand-in) vendor entrypoints expect, and reads the Agno `ToolResult` a vendor tool returns or streams as a `ToolValue` |
| `tool_system/toolkit_connections.py` | Connects the `requires_connect` toolkits, such as Postgres, around each run that offers them, and holds the per-call toolkit lifetime (connect, owned bodies, close, retained cleanup) the script broker and MCP gateway share |
| `tool_system/invocation.py` | Decodes a model's tool-call arguments, runs one call through middleware to its outcome for the loop or to the tool's own value for runners outside it (injection, synchronous offload, generator draining, error text), and reads that value as the text and media references a runner hands its client |
| `tool_system/tool_hooks.py` | MindRoom's tool-call policy as middleware around every call: plugin before-call, environment, and after-call hooks, the background-script approval gate, job and authority checkpoints, durable call recording, and failure records |
| `tool_system/tool_environment.py` | The environment one tool call gives its shell commands, local or worker-routed: the plugin hooks' changes with the response's CLI grant over them |
| `llm/` | Provider-neutral values the runtime shares: messages, media, usage, run records and the pause requirements read from their tools, run events, the `ModelProvider` protocol, and normalized provider errors, with the dict keys stored runs carry |
| `llm/legacy_run_content.py` | The content a failed run earlier releases stored: its failure text in place of the model's, handed over redacted |
| `llm/tool_search.py` | Portable deferred tool search for routes the build marks portable: the pipeline stage that leaves deferred schemas off the request and shows stored deferred calls as `call_tool` calls, the loop's rewrite of a model's `call_tool` calls to the functions they name, and the argument parsing both share, which every wire that sends tool-call arguments as an object uses to replay a call another provider stored |
| `agent_loop/model_loop.py` | `run_model_loop`, the request and tool loop agents, team leaders, and skill reviews share: send the messages, append the answer, run its tool calls, and stop on a final answer, a stopping tool, a pause, a hook, or the request cap |
| `agent_loop/run.py` | `run_agent` and `resume_agent`: agent runs and approval resumes over the model loop, each ending in one terminal record handed to the persist callback before the run ends |
| `agent_loop/spec.py` | `AgentSpec` and `RunContext`: what an agent run is built from and the run's own identity |
| `agent_loop/tools.py` | The tools a run offers, and one assistant message's tool calls planned, run, and answered: resolution, the tool-call limit, pause classification, and result messages |
| `agent_loop/build.py` | `AgentBuild`, what `create_agent` produces for the loop, and `prompt_for_turn`, its stable and turn system messages with one turn's context appended to the turn one |
| `agent_loop/prompt.py` | `PromptSpec` and `render_system_messages`: an agent's system prompt from named sections, as a stable system message and a turn system message |
| `agent_loop/structured.py` | The schema-derived JSON prompt a JSON-mode route needs for an output schema, the answer text of a forced-tool route's forced call, and parsing answers into the schema: strict where the route enforces the schema, with repairs only on JSON-mode routes |
| `agent_loop/helper.py` | `complete_prompt`: one helper request, without the caller's tools, whose system message is its instructions as written, optionally read into a schema, with the call's content-free usage (`HelperUsage`), which the caller records through `helper_usage.py` |
| `agent_loop/output_limit.py` | `output_limit_policy`: answers the tool calls of a response the output limit cut off instead of running them, and fails the run on the third cut-off response |
| `tools/agno_compat_*.py`, `custom_tools/agno_compat_*.py`, and `oauth/agno_compat_google_auth.py` | Repairs of Agno toolkits and of Google auth's toolkit binding; see `docs/architecture/agno-compatibility.md` for the boundary inventory and upstream tracking |
| `constants.py` | Shared constants, paths, and environment variable defaults |
| `error_handling.py` | User-friendly error message extraction |
| `authorization.py` | Sender and per-agent authorization checks |
| `access_policy.py` | Resolve membership access config into immutable effective room and responder policies |
| `config/access.py` | Membership access configuration models (responder access, room defaults) |
| `config/legacy_access.py` | One-shot migration from retired access fields to the membership schema; delete with the retired fields |
| `thread_utils.py` | Thread analysis and agent detection |
| `session_ids.py` | Leaf helpers for the canonical persisted room/thread session ID |
| `turn_model.py` | The model one turn runs: explicit choice, thread and room overrides, authored model, the team rule, then the budget fallback |
| `thread_models.py` | Durable per-thread model overrides backing `!model` and the `thread_model` tool |
| `room_model_overrides.py` | Durable per-room runtime model defaults backing `!room_model` |
| `file_watcher.py` | File change detection for config hot-reload |
| `interactive.py` | Interactive Q&A system via Matrix reactions |
| `stop.py` | `SpanRegistry`: cancels exactly the reply span a Stop reaches; the Stop button reaction |
| `topic_generator.py` | Managed room topics from the configured description or generated with AI |
| `debug_report.py` | Read-only collection of what the backend stored about one reported conversation: event journal, stored runs, tool-call and LLM request logs, and log lines |
| `cli/main.py` | Main CLI entry point (Typer app) |
| `cli/banner.py` | CLI startup banner |
| `cli/config.py` | Config subcommand logic |
| `cli/connect.py` | `mindroom connect` pairing helpers and owner placeholder replacement |
| `cli/debug_report.py` | `mindroom debug-report` command: bug report parsing, config-only storage location, and JSON output |
| `cli/doctor.py` | Doctor command implementation |
| `cli/help_format.py` | Help-page panel order, green help theme, and the examples epilog shared by CLI commands |
| `cli/interactive.py` | Arrow-key prompts and step headers for terminal setup, with line prompts for piped input |
| `cli/local_stack.py` | Local stack setup command |
| `credentials/sync.py` | Shared provider keys, the embedder key, and the Ollama host as the runtime resolves them |
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
| `slack/` | Native Slack: the minimal Web API client, personal and project tools, events, and agent reply mirroring |
| `slack/api.py` | Minimal async Slack Web API client over `httpx`, with Slack error and rate-limit mapping, parsing of conversation, message, permalink, and user targets, and message permalinks |
| `slack/app.py` | The Slack app's workspace-wide credentials from the shared credential store, and the bot identity `auth.test` reports for them, cached per bot token |
| `slack/events.py` | Pure Slack event handling: the signing-secret check, which Events API messages may wake anyone, the thread keys, which project agent answers an event, and which personal watches an event may be for |
| `slack/files.py` | Slack file transfers: a download from Slack's file host, refused past 50 MB and written into the workspace's `slack-downloads/` without following links, and the external upload flow, which never sends the token to Slack's presigned upload link |
| `slack/identity.py` | Maps a Slack author to a MindRoom account: the Slack display name and, for full workspace members whose profile email is in `slack.email_domain`, the Matrix user ID from `slack.matrix_user_id_template`; cached for ten minutes per team and user; also looks up a channel with `conversations.info`, cached the same way for names but fresh for deciding whether the app may act there, and holds that project channel rule |
| `slack/integration.py` | Slack's entry in the integration registry: the events route, the runtime those events reach bound to the live bots, project-turn reply mirroring, and the Slack config checks; it loads the Slack runtime only on first use, because config validation reads the registry |
| `slack/oauth.py` | The built-in `slack` OAuth provider: each requester's own Slack user token requested as `user_scope`, accepted only from the workspace the bot token belongs to |
| `slack/render.py` | Plain dictionaries for agents from Slack answers: messages with author names and permalinks, people, conversations, and the display-name rule; Slack message markup as plain text |
| `slack/delivery.py` | Delivers a Slack event to the project agent answering its channel, while the app is still in it, as the mapped author, then to personal watches; retries after the route acknowledged, reacts 👀, and sends notices only the author sees |
| `slack/turns.py` | Posts a Slack message as one target agent's own hook dispatch, mentioning only that agent with the Slack text fenced as plain text; claims each message once per target and keeps a Slack thread on its Matrix thread |
| `slack/reply_mirror.py` | Posts the final reply of an agent's Slack project turn into its Slack thread as the app, in the background after the Matrix reply; only a managed account's hook dispatch whose Slack thread delivery bound to this room and Matrix thread, in a channel and room the agent currently answers |
| `slack/watch_delivery.py` | Delivers a Slack event to the personal watches it is for, once Slack confirms the owner can see it and while the watch room may receive it |
| `slack/watches.py` | Personal Slack watches: the durable store of who receives which Slack mentions and direct messages in which room, the check that a room holds only the owner and MindRoom's accounts and hides earlier messages from people who join later, and the `slack_watch` actions that start, stop, and report a watch |
| `custom_tools/slack.py` | The `slack` toolkit: personal calls with the requester's own Slack user token, project calls with the app's bot token only in listed channels the bot belongs to; search, recent conversations, read, post, edit, delete, react, workspace downloads and uploads through `slack/files.py`, rendered by `slack/render.py`; in a personal toolkit built for a Matrix room, `slack_watch` runs the watch actions of `slack/watches.py` |

## Persistent State

[Data Persistence](../deployment/storage.md#data-persistence) lists the main directories under the storage root (`mindroom_data/` by default, overridable via `MINDROOM_STORAGE_PATH`).
An agent's conversation database is `agents/<agent>/sessions/<agent>.db`, and private agents keep the same per-agent layout under their resolved private state roots.
Its tables are named after its sessions table `<agent>_sessions`: `_log_runs` holds a row per run, `_log_messages` a row per stored message, `_log_tools` a row per tool execution, `_log_summaries` the compaction summary rows, `_log_removed` the runs that edits and redactions removed, `_log_meta` the layout version, run counter, and owner, and `_usage` the usage ledger.
A database converted from the Agno layout keeps its pre-conversion copy beside it as `<agent>.db.pre-storage-v2`, and one converted from layout version 2 or 3 as `<agent>.db.pre-storage-v4`.
`tracking/personal_agents.json` is the roster of agents generated from `personal_agents` templates, read on every config load.
