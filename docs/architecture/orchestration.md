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
│ 4. Create bots   │
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
- **Room setup** (`_setup_rooms_and_memberships`): Router creates rooms, invites agents/users, bots join
- **Sync loops**: Each bot runs `sync_forever_with_restart()` with automatic retry
- **Internal user identity**: `mindroom_user.username` is bootstrap-only; only `display_name` should change later

## Hot Reload

Config changes are detected via polling (`watch_file()` checks `st_mtime` every second):

1. On change, `update_config()` is called
2. `_identify_entities_to_restart()` computes diff using `model_dump(exclude_none=True)`
3. Affected entities are stopped, recreated, and restarted
4. Removed entities run `cleanup()` (leave rooms, stop bot)
5. New/restarted bots go through room setup

Skills are watched separately via `_watch_skills_task()` with cache invalidation.

## Orchestration Subpackage

The `src/mindroom/orchestration/` subpackage contains helpers extracted from the monolithic orchestrator:

- **`runtime.py`** — Sync loop helpers: `sync_forever_with_restart()` with linear backoff (capped at 60s), `cancel_task()`, and `create_logged_task()` for safe asyncio task creation.
- **`config_updates.py`** — Config diffing and reload planning: `build_config_update_plan()` computes a `ConfigUpdatePlan` by calling `_identify_entities_to_restart()`, which diffs old and new configs using `model_dump(exclude_none=True)`.
- **`rooms.py`** — Room invitation helpers: `get_authorized_user_ids_to_invite()` and `get_root_space_user_ids_to_invite()` compute which users should be invited to managed rooms and the root Matrix space.

### Runtime Resolution

Agent and team materialization is handled by dedicated top-level modules (not inside the `orchestration/` subpackage):

- **`src/mindroom/runtime_resolution.py`** — Resolves `ResolvedAgentRuntime` (the full set of runtime parameters for one agent instance) including `ResolvedKnowledgeBinding` for knowledge base attachment.
- **`src/mindroom/team_runtime_resolution.py`** — Resolves `ResolvedExactTeamMembers` for team materialization via `materialize_exact_requested_team_members()`.
- **`src/mindroom/runtime_state.py`** — Shared runtime readiness state with `set_runtime_starting()`, `set_runtime_ready()`, and `set_runtime_failed()` used by health endpoints.

## Message Handling

Event callbacks are wrapped in `_create_task_wrapper()` to run as background tasks, ensuring the sync loop is never blocked.

**`_on_message` flow:**

1. Skip own messages (except voice transcriptions from router)
2. Check sender authorization and handle edits
3. Check if already responded (`ResponseTracker`)
4. Router handles commands exclusively
5. Extract message context (mentions, thread history, non-agent mention detection)
6. Skip messages from other agents (unless mentioned)
7. Router performs AI routing when no agent mentioned and thread doesn't have multiple human participants
8. Check for team formation or individual response
9. Generate response and store memory

**Message edits**: When a user edits a message that already received an agent response, the agent regenerates its response for the updated content.
The agent edits its own previous reply in place rather than sending a new message.
Edits from other agents are ignored, and the feature requires that the original response event ID is tracked by the `ResponseTracker`.

**`_on_media_message`**: Handles media events (images, videos, files, and audio). Downloads and decrypts media data, then processes it through the agent. When no agent is mentioned, AI routing is used to select the appropriate agent, similar to text messages.

**`_on_reaction`**: Handles `ReactionEvent` for the interactive Q&A system (e.g., confirming or rejecting agent suggestions) and config confirmation workflows.

**Routing** (when no agent mentioned): Router uses `suggest_agent_for_message()` to pick the best agent based on room configuration and message content. Only routes when multiple agents are available. In threads where multiple non-agent users have posted, routing is skipped entirely — an explicit `@mention` is required. Non-MindRoom bots listed in `bot_accounts` are excluded from this detection.

## Concurrency

- Each bot runs its own sync loop via `sync_forever_with_restart()`
- Sync loop failures trigger automatic restart with linear backoff (5s, 10s, 15s, ... up to 60s max)
- Event callbacks run as background tasks (never block the sync loop)
- `ResponseTracker` prevents duplicate replies
- `StopManager` handles cancellation of in-progress responses

### Graceful Shutdown

On `orchestrator.stop()`:

1. Set `self.running = False`
2. Stop memory auto-flush worker
3. Cancel knowledge refresh task
4. Cancel pending bot start tasks
5. Shut down knowledge managers (`shutdown_shared_knowledge_managers()`)
6. Cancel all sync tasks
7. Signal all bots to stop (`bot.running = False`)
8. Call `bot.stop()` for each bot concurrently (waits 5s for background tasks, cancels scheduled tasks, closes Matrix client)
