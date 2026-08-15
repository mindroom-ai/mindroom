---
icon: lucide/workflow
---

# Agent Orchestration

The `MultiAgentOrchestrator` (in `src/mindroom/orchestrator.py`) manages the lifecycle of all agents, teams, and the router.

## Boot Sequence

```
main() entry
       │
       ▼
┌──────────────────┐
│ Sync Provider    │
│ Credentials      │
│ (.env/bootstrap  │
│ env → shared     │
│ credentials)     │
└────────┬─────────┘
         │
         ▼
┌──────────────────┐
│  Initialize()    │
│ ─────────────────│
│ 1. Parse config  │
│    (Pydantic)    │
│ 2. Load plugins  │
│ 3. Create "user" │
│    Matrix account│
│    (mindroom_user)│
│ 4. Prepare       │
│    entity Matrix │
│    accounts      │
│ 5. Create bots   │
│    for entities  │
└────────┬─────────┘
         │
         ▼
┌──────────────────┐
│    Start()       │
│ ─────────────────│
│ 1. try_start()   │
│    each bot      │
│ 2. Setup rooms   │
│    & memberships │
│ 3. Create sync   │
│    tasks         │
└────────┬─────────┘
         │
         ▼
┌──────────────────────────────────────┐
│  Auxiliary Tasks (auto-restart)      │
│ ─────────────────────────────────────│
│ • config watcher (file polling)      │
│ • skills watcher (skill cache)       │
│ • API server (if enabled)            │
│  (each wrapped in                    │
│   _run_auxiliary_task_forever)        │
└───────────────┬──────────────────────┘
                │
                ▼
┌──────────────────────────────────────┐
│  Bot Sync Tasks (asyncio.gather)     │
│ ─────────────────────────────────────│
│ • One sync loop per bot              │
│ • sync_forever_with_restart()        │
│ • Awaited until shutdown             │
└──────────────────────────────────────┘
```

**Key details:**

- **Entity order**: Router first, then agents, then teams
- **Room setup** (`_setup_rooms_and_memberships`): Router creates rooms, invites agents, teams, and users, then bots join
- **Sync loops**: Each bot runs `sync_forever_with_restart()` with automatic retry; the default `matrix_sync.mode: classic` uses classic `/v3/sync` and `matrix_sync.mode: sliding` opts into MSC4186 Simplified Sliding Sync
- **Internal user identity**: `mindroom_user.username` is the account-creation request; runtime authorization uses the persisted actual Matrix ID

## Runtime Replacement Admission

Config changes are detected via polling (`watch_paths()` checks watched source-file mtimes every second and fires after one quiet scan).
MCP catalog changes use the same replacement admission path when the changed server has dependent agents or teams.
The MCP manager callback schedules an orchestrator-owned background task so the triggering tool call can return and release its admission slot before replacement draining begins.

1. On a config change, `ConfigReloadLifecycle.request_reload()` queues a debounced reload.
2. On an MCP catalog change, the orchestrator returns immediately when no configured entity references that server, while still clearing the worker validation snapshot cache.
   The dependent-entity check runs again under the config update lock immediately before replacement.
3. `ConfigReloadLifecycle.apply_with_response_admission()` serializes config reloads and MCP catalog replacements behind one global admission owner.
4. Sampling the in-flight count and closing the shared `ResponseAdmissionGate` happen atomically, so a new response cannot race the decision to apply.
   The gate covers Matrix-driven response lifecycles only.
   Direct agent-run entry points that bypass the response lifecycle (`mindroom.api.openai_compat` and cascaded voice in `mindroom.matrix_rtc.call_tools`) are not admitted through it, so a replacement can still land underneath one of those runs.
   The gate stays closed, but is not held, across config loading and plan application.
   Holding it would stall the apply against itself: applying the plan stops bots, and stopping a bot drains its detached responses.
5. While the gate is closed, a response waits before taking a lifecycle lock, incrementing the in-flight count, or publishing a placeholder.
   The gate is global and covers the whole apply window regardless of how narrow the plan turns out to be.
   When the apply finishes, responses owned by unchanged or replacement runtimes compete for admission normally.
6. A runtime being replaced wakes its pre-admission waiters with `ResponseAdmissionRefusedError`.
   This is deliberately not an `asyncio.CancelledError`, because the Matrix callback must fail and invalidate the old sync checkpoint so the replacement runtime replays the source event.
   The refusal path performs no Matrix I/O, so replacement shutdown cannot stall on an untimed send.
   Auto-resume messages received by replacement bots during the apply wait for the gate to reopen instead of being dropped.
7. If responses never drain, either replacement flow stops deferring after 600 seconds and closes the gate over still-running responses.
   This bounded forced apply prevents a busy install from starving config or MCP replacement forever.
8. For config reloads, `ConfigReloadLifecycle.update_config()` loads the new config and `_identify_entities_to_restart()` computes the diff using `model_dump(exclude_none=True)`.
9. The orchestrator applies the resulting plan: affected entities are stopped, recreated, and restarted.
10. Removed entities run `cleanup()` to leave rooms and stop the bot.
11. New and restarted bots go through room setup.
12. The gate reopens once the apply finishes, whether it succeeded, failed, or was cancelled, and deferred responses may then start.

Skills are watched separately via `_watch_skills_task()` with cache invalidation.

## Orchestration Subpackage

The `src/mindroom/orchestration/` subpackage contains helpers extracted from the monolithic orchestrator:

- **`runtime.py`** — Sync loop helpers: `sync_forever_with_restart()` with exponential backoff capped at 60 seconds, `cancel_task()`, and `create_logged_task()` for safe asyncio task creation.
- **`config_lifecycle.py`** — Debounced config-reload and shared replacement-admission lifecycle: `ConfigReloadLifecycle` owns reload queueing, serialized global response draining for config and MCP replacements, and the load → diff → plan sequencing that dispatches config plans back to the orchestrator.
- **`config_updates.py`** — Config diffing and reload planning: `build_config_update_plan()` computes a `ConfigUpdatePlan` by calling `_identify_entities_to_restart()`, which diffs old and new configs using `model_dump(exclude_none=True)`.
- **`plugin_watch.py`** — Plugin hot-reload watcher: `watch_plugins_task()` polls configured plugin roots, with `PluginWatchState` owning the watcher baselines and dirty-state revision.
- **`rooms.py`** — Room invitation helpers: `get_authorized_user_ids_to_invite()` and `get_root_space_user_ids_to_invite()` compute which users should be invited to managed rooms and the root Matrix space.

### Runtime Resolution

Agent and team materialization is handled by dedicated top-level modules (not inside the `orchestration/` subpackage):

- **`src/mindroom/runtime_resolution.py`** — Resolves `ResolvedAgentRuntime` (the full set of runtime parameters for one agent instance) including `ResolvedKnowledgeBinding` for knowledge base attachment.
- **`src/mindroom/team_exact_members.py`** — Resolves `ResolvedExactTeamMembers` for team materialization via `materialize_exact_requested_team_members()`.
- **`src/mindroom/agent_policy.py`** — Resolves canonical execution policies and private-team eligibility derived from authored agent config.
- **`src/mindroom/model_loading.py`** — Owns `get_model_instance()` and provider-specific model loader selection.
- **`src/mindroom/ai_runtime.py`** — Owns agent-run input copying, queued-notice hooks, and inline-media fallback helpers used during execution.
- **`src/mindroom/agent_storage.py`** — Owns agent session and learning SQLite storage construction helpers.
- **`src/mindroom/agent_descriptions.py`** — Owns shared agent description rendering used by routing and delegation.
- **`src/mindroom/runtime_state.py`** — Shared runtime readiness state with `set_runtime_starting()`, `set_runtime_ready()`, and `set_runtime_failed()` used by health endpoints.

## Message Handling

Correctness-critical timeline callbacks cross durable journal admission before ordinary callbacks run, and background dispatch workers then process committed work without blocking the sync loop.

**Inbound message flow:**

1. `matrix/journal_ingress.py` commits the event before nio accepts it.
2. `journal_dispatch.py` and `pending_event_worker.py` dispatch admitted or recovered work.
3. `turn_controller.py` runs ingress validation, normalization, conversation resolution, receipt ordering, and coalescing.
4. `text_ingress_dispatch.py` and `turn_policy.py` decide whether to ignore, route, execute a command, or respond.
5. `response_runner.py` and `response_turn.py` execute the selected agent or team.
6. `delivery_gateway.py` sends or edits the Matrix response and `TurnStore` records durable terminal truth.

**Message edits**: When a user edits a message that already received an agent response, the agent regenerates its response for the updated content.
The agent edits its own previous reply in place rather than sending a new message.
Edits from other agents are ignored, and the feature requires that the turn's `anchor_event_id` is recorded in the `TurnStore`.

**`_on_media_message`**: Handles media events (images, videos, files, and audio).
Downloads and decrypts media data, then processes it through the selected responder.
When no agent or team is mentioned, routing selects the appropriate agent or team, similar to text messages.

**`_on_reaction`**: Handles `ReactionEvent` for the interactive Q&A system (e.g., confirming or rejecting agent suggestions) and config confirmation workflows.

**Routing** (when no agent or team is mentioned): Router narrows candidates from room configuration or joined MindRoom entities, filters them by sender permissions, lets one remaining candidate answer directly, and uses `suggest_responder_for_message()` only when multiple candidates remain.
In threads where multiple non-agent users have posted, routing is skipped entirely — an explicit `@mention` is required.
Non-MindRoom bots listed in `bot_accounts` are excluded from this detection.

## Concurrency

- Each bot runs its own sync loop via `sync_forever_with_restart()`
- Sync loop failures trigger automatic restart with capped exponential backoff (5s, 10s, 20s, 40s, then 60s maximum)
- Watchdog-driven restarts of stalled sync loops add 0–10s of random jitter on top of the backoff so a loop-wide stall does not restart every sync loop as one thundering herd
- An automatic receive-loop restart replaces only the sync task and its watchdog, so in-flight responses keep their original owner and finish across the restart
- The response runtime is drained and cancelled only when the bot itself stops: a config reload replacing the entity, entity removal, or process shutdown
- Each of those lifecycle events logs `restart_reason_category` and `resulting_action`, so `matrix_sync_transport_restart` is distinguishable from `matrix_agent_response_runtime_shutdown` in logs
- Admitted callbacks are dispatched as background work and remain durably retryable until settled
- `TurnStore`, backed by the durable handled-turn ledger, prevents duplicate replies
- `StopManager` handles cancellation of in-progress responses

### Graceful Shutdown

On `orchestrator.stop()`:

1. Mark the runtime stopped, signal runtime shutdown, unbind external triggers, and close approval transport/runtime state.
2. Cancel config reload, drain MCP catalog and dispatch-recovery work, and cancel startup maintenance.
3. Stop todo-poke and memory auto-flush workers plus knowledge watching and refresh scheduling.
4. Cancel pending bot starts and stop the MCP manager.
5. Cancel sync tasks before stopping bots so shutdown cannot race active receive loops.
6. Stop all bots concurrently, wait for attachment cleanup, and close the shared event journal last because bots borrow it while draining delivery work.
