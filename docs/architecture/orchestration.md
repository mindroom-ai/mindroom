---
icon: lucide/workflow
---

# Agent Orchestration

The `MultiAgentOrchestrator` (in `src/mindroom/bot.py`) manages the lifecycle of all agents, teams, and the router.

## Lifecycle

The boot sequence runs through these phases:

```
main() entry
       │
       ▼
┌──────────────────┐
│ Sync Credentials │
│ (.env → vault)   │
└────────┬─────────┘
         │
         ▼
┌──────────────────┐
│  Initialize()    │
│ ─────────────────│
│ 1. Create user   │
│    account       │
│ 2. Parse config  │
│    (Pydantic)    │
│ 3. Load plugins  │
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
│  Concurrent Tasks (asyncio.wait)     │
│ ─────────────────────────────────────│
│ • orchestrator_task (sync loops)     │
│ • watcher_task (config file polling) │
│ • skills_watcher_task (skill cache)  │
└──────────────────────────────────────┘
```

### Boot Sequence Details

1. **User Account Creation**: A "user" account is created first via `_ensure_user_account()`
2. **Entity Order**: Router starts first, then agents, then teams
3. **Room Setup** (`_setup_rooms_and_memberships`):
   - Ensure all configured rooms exist (router creates them)
   - Invite agents and users to rooms
   - Have bots join their configured rooms
4. **Sync Loops**: Each bot runs `_sync_forever_with_restart()` with automatic retry on failure

## Hot Reload

Config changes are detected via polling (not Watchdog):

1. `watch_file()` polls `config.yaml` every second by checking `st_mtime`
2. On change, `_handle_config_change()` triggers `orchestrator.update_config()`
3. Diff computed via `_identify_entities_to_restart()`:
   - Compare agent configs using `model_dump(exclude_none=True)`
   - Check for new/removed entities
   - Check if router needs restart (room changes)
4. Affected entities are stopped, recreated, and restarted
5. Removed entities run `cleanup()` (leave rooms, stop bot)
6. New/restarted bots go through room setup

**No process restart required!**

Skills are also watched separately via `_watch_skills_task()` with cache invalidation.

## Message Handling

Messages are processed via Matrix event callbacks. The actual flow:

1. **Event Callback Registration** (in `AgentBot.start()`):
   - Callbacks are wrapped in `_create_task_wrapper()` to run as background tasks
   - This ensures the sync loop is never blocked by event processing

2. **Message Processing** (simplified):
   - Check if message mentions this agent
   - Check thread context and history
   - Determine if agent should respond
   - Generate response (streaming or batch)
   - Store conversation memory

3. **Routing** (when no agent mentioned):
   - Router uses `suggest_agent_for_message()` to pick the best agent
   - Considers room configuration and message content

## Concurrency

MindRoom handles multiple conversations concurrently:

- Each bot runs its own sync loop via `_sync_forever_with_restart()`
- Sync loop failures trigger automatic restart with exponential backoff (5s, 10s, ... up to 60s)
- Event callbacks run as background tasks (never block the sync loop)
- `ResponseTracker` prevents duplicate replies per thread
- `StopManager` handles cancellation of in-progress responses

### Sync Loop Recovery

```python
# Simplified from _sync_forever_with_restart()
while bot.running:
    try:
        await bot.sync_forever()
    except Exception:
        wait_time = min(60, 5 * retry_count)
        await asyncio.sleep(wait_time)
```

### Graceful Shutdown

On shutdown (`orchestrator.stop()`):

1. Cancel all sync tasks
2. Signal all bots to stop (`bot.running = False`)
3. Wait for background tasks to complete (5s timeout)
4. Close Matrix clients
