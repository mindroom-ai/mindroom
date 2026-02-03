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
│ 1. Create "user" │
│    Matrix account│
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

1. **User Account Creation**: A "user" Matrix account (named "Mindroom User") is created first via `_ensure_user_account()` - this is a human observer account, separate from agent accounts
2. **Entity Order**: Router is created first, then agents, then teams (order defined in `all_entities` list)
3. **Agent Account Setup**: Each bot calls `ensure_user_account()` during `start()` to create its own Matrix account
4. **Room Setup** (`_setup_rooms_and_memberships`):
   - Ensure all configured rooms exist (router creates them)
   - Invite agents and users to rooms
   - Have bots join their configured rooms
5. **Sync Loops**: Each bot runs `_sync_forever_with_restart()` with automatic retry on failure

## Hot Reload

Config changes are detected via polling (not Watchdog):

1. `watch_file()` polls `config.yaml` every second by checking `st_mtime`
2. On change, the callback triggers `orchestrator.update_config()` (via `_handle_config_change()`)
3. Diff computed via `_identify_entities_to_restart()`:
   - Compare agent configs using `model_dump(exclude_none=True)`
   - Check for new/removed entities
   - Check if router needs restart (configured room set changed)
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

2. **Message Processing** (in `_on_message`):
   - Skip own messages (except voice transcriptions from router)
   - Check sender authorization
   - Handle message edits separately
   - Check if already responded (`ResponseTracker`)
   - Router handles commands exclusively
   - Extract message context (mentions, thread history)
   - Skip messages from other agents (unless mentioned)
   - Check for team formation or individual response
   - Generate response (streaming or batch)
   - Store conversation memory as background task

3. **Routing** (when no agent mentioned):
   - Router uses `suggest_agent_for_message()` to pick the best agent
   - Considers room configuration and message content
   - Only routes when multiple agents are available in the room

## Concurrency

MindRoom handles multiple conversations concurrently:

- Each bot runs its own sync loop via `_sync_forever_with_restart()`
- Sync loop failures trigger automatic restart with linear backoff (5s, 10s, 15s, ... up to 60s)
- Event callbacks run as background tasks (never block the sync loop)
- `ResponseTracker` prevents duplicate replies to the same message
- `StopManager` handles cancellation of in-progress responses

### Sync Loop Recovery

```python
# Simplified from _sync_forever_with_restart()
retry_count = 0
while bot.running:
    try:
        await bot.sync_forever()
    except Exception:
        retry_count += 1
        wait_time = min(60, 5 * retry_count)  # 5s, 10s, 15s, ... up to 60s
        await asyncio.sleep(wait_time)
```

### Graceful Shutdown

On shutdown (`orchestrator.stop()`):

1. Cancel all sync tasks
2. Signal all bots to stop (`bot.running = False`)
3. Call `bot.stop()` for each bot, which:
   - Waits for background tasks to complete (5s timeout)
   - Closes the Matrix client
