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
│ 3. Create the    │
│    internal user │
│    (only when    │
│    mindroom_user │
│    is configured)│
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
│ 2. Create sync   │
│    tasks         │
│ 3. Background    │
│    room setup    │
└────────┬─────────┘
         │
         ▼
┌───────────────────────────────────────────┐
│ Auxiliary watchers (auto-restart)         │
│ • config, plugins, and skills             │
│ • _run_auxiliary_task_forever             │
└─────────────────────┬─────────────────────┘
                      │
                      ▼
┌───────────────────────────────────────────┐
│ Runtime completion monitor                │
│ • asyncio.wait(..., FIRST_COMPLETED)      │
│ • orchestrator, shutdown, optional API    │
│ • unexpected API exit fails the runtime   │
└───────────────────────────────────────────┘
```

**Key details:**

- **Entity order**: Router first, then agents, then teams
- **Room setup** (`_setup_rooms_and_memberships`): Resolve/create rooms and the root Space, join the router, reconcile managed policy once, then invite and join the remaining identities
- **Runtime lifetime**: The orchestrator waits for explicit shutdown while individual bot sync tasks can be replaced; the completion monitor does not await only the original sync-task generation.
- **Sync loops**: Each bot runs `sync_forever_with_restart()` with automatic retry; `matrix_sync.mode: classic` uses Classic `/v3/sync`, while `sliding` uses MSC4186 Simplified Sliding Sync on a homeserver advertising `org.matrix.simplified_msc3575`
- **Internal user identity**: `mindroom_user.username` is the account-creation request; runtime authorization uses the persisted actual Matrix ID

Each room setup pass reconciles managed room policy (name, topic, power levels, encryption, join rules, root Space children, and invitations) against fresh room-state reads.
No configuration hash or persisted state cache suppresses remote drift checks on the next startup or relevant config update.
A required power-level write rereads current grants so intervening administrator changes are preserved.
Room setup runs one full pass; transport retries stay with nio, and returned administrative failures are retried on the next setup attempt.

## Session Storage Recovery

Before opening an owned session database, `session_storage_preflight.py` checks any existing session table for the required Agno columns.
If any are missing, it moves the whole `sessions/` directory, including SQLite journals and sidecars, to a unique `sessions.incompatible-*` sibling and recreates `sessions/` with its original permissions; see [Incompatible session databases](../deployment/storage.md#incompatible-session-databases) for the operator view.
Only session storage is archived; learning, authored files, credentials, and Matrix encryption keys stay in place.
Permission failures, corruption, unexpected schema objects, and unsafe paths raise errors instead of triggering an archive.
Compatible Agno 2 and mixed-schema session data stays readable through Agno's compatibility reads, with no background conversion.
Run upgrades while MindRoom is stopped so recovery cannot overlap active session writers.

## Runtime Replacement Admission

Config changes are detected via polling (`watch_paths()` checks watched source-file mtimes every second and fires after one quiet scan).
MCP catalog changes use the same replacement admission path when the changed server has dependent agents or teams.
The MCP manager callback schedules an orchestrator-owned background task so the triggering tool call can return and release its admission slot before replacement draining begins.

1. On a config change, `ConfigReloadLifecycle.request_reload()` queues a debounced reload.
2. On an MCP catalog change, the orchestrator returns immediately when no configured entity references that server, while still clearing the worker validation snapshot cache.
   The dependent-entity check runs again under the config update lock immediately before replacement.
   Each server has at most one queued catalog replacement, and changes reported before it takes the config update lock are covered by it.
3. Config reloads and MCP catalog replacements serialize behind one global admission owner; MCP replacements enter through `ConfigReloadLifecycle.apply_with_response_admission()`.
4. Sampling the in-flight count and closing the shared `ResponseAdmissionGate` happen atomically, so a new response cannot race the decision to apply.
   The gate covers Matrix-driven response lifecycles, external-trigger delivery, call admission, and requester-driven call operations.
   Text and router planning, commands, edit regeneration, interactive selections, visible router voice echoes, calls, and external triggers perform their final reply-policy check after admission and retain the slot through their direct side effect or response-runner handoff.
   The OpenAI-compatible API in `mindroom.api.openai_compat` remains outside this gate because it does not use Matrix reply authorization.
   Config loading keeps response admission open; after current responses drain, the gate closes for diff planning and publication.
   Holding the gate while loading would block responses for validation work that cannot affect the live runtime.
5. While the gate is closed, a response waits before taking a lifecycle lock, incrementing the in-flight count, or publishing a placeholder.
   The gate is global and covers the whole apply window regardless of how narrow the plan turns out to be.
   When the apply finishes, responses owned by unchanged or replacement runtimes compete for admission normally.
6. A runtime being replaced wakes its pre-admission waiters with `ResponseAdmissionRefusedError`.
   The refusal leaves the admitted source pending in the event journal so the replacement runtime can replay it.
   The refusal path performs no Matrix I/O, so replacement shutdown cannot stall on an untimed send.
   Replies the forced apply cancels are left pending the same way, so the replacement runtime replays them and continues each in its existing message.
7. If responses never drain, either replacement flow stops deferring after 600 seconds and closes the gate over still-running responses.
   This bounded forced apply prevents a busy install from starving config or MCP replacement forever.
8. For config reloads, `ConfigReloadLifecycle._update_config()` loads and validates the new config while admission remains open, then `build_config_update_plan()` computes targeted restarts and in-place reconciliations after the gate closes.
9. The orchestrator applies the resulting plan: changed entities are replaced, unchanged bots receive the new config, and room-only changes reconcile memberships in place without restarting receive loops.
   A call-enabled agent is replaced when its own call setup changes: its `calls.agents` entry, the profile it uses, a model that profile references, `calls.enabled`, or `calls.livekit_service_url`.
   Otherwise `CallManager.update_config()` hands the new config to later calls, but an agent with a call in progress is replaced after any authored config change, ending that call, because the call's tools, prompt, and approval policy were built from the config it joined with.
10. Removed entities prepare their response runtime for shutdown, reconcile approval work, and call `leave_rooms()` while ingestion remains active; the orchestrator then cancels the receive loop and stops the bot.
11. New and restarted bots go through room setup.
12. The gate reopens once the apply finishes, whether it succeeded, failed, or was cancelled, and deferred responses may then start.

Skills are watched separately via `_watch_skills_task()` with cache invalidation.

## Orchestration Subpackage

The `src/mindroom/orchestration/` subpackage holds the orchestrator's helpers:

- **`runtime.py`** — Sync loop helpers: `sync_forever_with_restart()` with exponential backoff capped at 60 seconds, `cancel_task()`, and `create_logged_task()` for safe asyncio task creation.
- **`config_lifecycle.py`** — Debounced config-reload and shared replacement-admission lifecycle: `ConfigReloadLifecycle` owns reload queueing, serialized global response draining for config and MCP replacements, and the load → diff → plan sequencing that dispatches config plans back to the orchestrator.
- **`config_updates.py`** — Config diffing and reload planning: `build_config_update_plan()` diffs the old and new configs into a `ConfigUpdatePlan` of entity restarts and in-place reconciliations.
- **`plugin_watch.py`** — Plugin hot-reload watcher: `watch_plugins_task()` polls configured plugin roots, with `PluginWatchState` owning the watcher baselines and dirty-state revision.
- **`rooms.py`** — Room invitation helpers: `get_authorized_user_ids_to_invite()` and `get_root_space_user_ids_to_invite()` compute which users should be invited to managed rooms and the root Matrix space.

## Subagent Ownership

`run_subagent` starts a separate child conversation; `continue_subagent` starts another turn in that same conversation.
The child uses the normal agent response envelope, so its history, tools, and model behavior follow the existing runtime.
Native Matrix approval pauses retain the parent wait and exact child run rather than keeping a Python call alive.

The runtime lives in the `src/mindroom/delegation/` package, with explicit imports between its modules.

| Module | Owns |
| --- | --- |
| `custom_tools/delegate.py` | Agent-facing tool schemas and direct invocation |
| `ai.py` | `run_delegated_child_response`, supplied as a typed callback to the native driver |
| `delegation/execution.py` | Parent waits, approval gates, child approval projection, and parent continuation |
| `delegation/lifecycle.py` | Child preparation, attempt identity, outcome transitions, and publication to storage and audit |
| `delegation/recovery.py` | Abandoned-turn reconciliation and recursive cancellation from retained Agno runs |
| `delegation/sessions.py` | Scoped handle reads, atomic reservations, snapshots, and liveness locks |
| `delegation/audit.py` / `delegation/records.py` | Workspace audit projections, event logs, transcripts, and receipts |
| `delegation/state.py` | Serializable runtime state and the child-runner protocol |
| `delegation/hooks.py` | Persisted plugin hook phases across approval continuations |
| `delegation/storage.py` | Frozen storage bindings for retained runs |

Both direct and native invocation use the same child preparation and lifecycle owner.
The native driver receives its response runner explicitly and does not construct the agent-facing toolkit.
Handle reads do not recover or execute children; recovery runs above storage under a liveness lock.
Audit snapshots do not settle child state or finish audit records; the lifecycle owner publishes terminal outcomes.
Editable workspace receipts never grant continuation authority.
A retained Agno run identifies the exact attempt; the lifecycle owner derives its outcome before publishing storage and audit projections.
Tach dependency rules and isolated import tests enforce these directions.

See [Agent Delegation](../tools/agent-orchestration.md#agent-delegation) for configuration, tool arguments, audit paths, and user-visible behavior.

## Message Handling

Correctness-critical timeline callbacks cross durable journal admission before ordinary callbacks run, and background dispatch workers then process committed work without blocking the sync loop.

See [Data Flow](index.md#data-flow) for the inbound path from Matrix sync to a recorded turn.
See [Message Edits](../configuration/threads.md#message-edits) for how edits to handled messages are processed.

**`_on_media_message`**: Handles media events (images, videos, files, and audio).
Downloads and decrypts media data, then processes it through the selected responder.

**`_on_reaction`**: Handles `ReactionEvent` for the interactive Q&A system (e.g., confirming or rejecting agent suggestions) and config confirmation workflows.

**Routing** (when no agent or team is mentioned): the router picks a responder as described in [How Routing Works](../configuration/router.md#how-routing-works), calling `suggest_responder_for_message()` only when several eligible candidates remain.

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

The entry-point shutdown helper cancels and settles startup before core teardown can release resources.
Auxiliary watchers are cancelled after core teardown.

On `orchestrator.stop()`:

1. Mark the runtime stopped, signal runtime shutdown, unbind external triggers, and close approval transport/runtime state.
2. Cancel config reload, drain MCP catalog and dispatch-recovery work, and cancel startup maintenance.
3. Stop todo-poke and memory auto-flush workers plus knowledge watching and refresh scheduling.
4. Cancel pending bot starts and stop the MCP manager.
5. Quiesce ingestion while its pump can still admit captured input, then cancel receive loops.
6. Stop all bots concurrently and finish retained response recovery proofs before releasing their clients.
7. Wait for attachment cleanup and close the shared journal only once no response owner remains.

Each response keeps one ownership record through terminal cleanup and recovery-proof consumption.
A timeout preserves the record and any in-flight proof, so shared resources remain available for deferred cleanup.
