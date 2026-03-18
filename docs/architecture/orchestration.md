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
│  Concurrent Tasks (asyncio.wait)     │
│ ─────────────────────────────────────│
│ • orchestrator_task (sync loops)     │
│ • watcher_task (config file polling) │
│ • skills_watcher_task (skill cache)  │
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

1. Shut down knowledge managers (`shutdown_shared_knowledge_managers()`)
2. Cancel all sync tasks
3. Signal all bots to stop (`bot.running = False`)
4. Call `bot.stop()` for each bot (waits 5s for background tasks, closes Matrix client)
