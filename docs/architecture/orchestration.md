---
icon: lucide/workflow
---

# Agent Orchestration

The MultiAgentOrchestrator manages the lifecycle of all agents, teams, and the router.

## Lifecycle

```
config.yaml loaded
       │
       ▼
┌──────────────────┐
│  Parse Config    │
│  (Pydantic)      │
└────────┬─────────┘
         │
         ▼
┌──────────────────┐
│ Create Entities  │
│ - Router         │
│ - Agents         │
│ - Teams          │
└────────┬─────────┘
         │
         ▼
┌──────────────────┐
│ Provision Users  │
│ (Matrix accounts)│
└────────┬─────────┘
         │
         ▼
┌──────────────────┐
│  Join Rooms      │
└────────┬─────────┘
         │
         ▼
┌──────────────────┐
│  Start Sync      │
│  (message loop)  │
└──────────────────┘
```

## Hot Reload

Config changes are detected automatically:

1. Watchdog monitors `config.yaml`
2. On change, config is re-parsed
3. Diff computed against running state
4. Affected agents are restarted
5. New rooms are joined

No restart required!

## Message Handling

```python
async def on_message(room, event):
    # 1. Check if message mentions an agent
    mentioned = extract_mentions(event.body)

    if mentioned:
        # Direct mention - route to that agent
        agent = get_agent(mentioned)
        await agent.respond(room, event)
    else:
        # No mention - use router
        agent = await router.decide(room, event)
        if agent:
            await agent.respond(room, event)
```

## Concurrency

MindRoom handles multiple conversations concurrently:

- Each agent runs its own async task
- Messages are queued per-room
- Response tracking prevents duplicates
- Rate limiting protects homeservers
