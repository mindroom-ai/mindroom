---
icon: lucide/layout
---

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
│  │  (Mem0 + ChromaDB, agent/team scopes)           │    │
│  └─────────────────────────────────────────────────┘    │
└─────────────────────────────────────────────────────────┘
```

## Components

- [Matrix Integration](matrix.md) - How MindRoom connects to Matrix
- [Agent Orchestration](orchestration.md) - How agents are managed

## Key Internal Modules

| Module | Purpose |
|--------|---------|
| `orchestrator.py` | MultiAgentOrchestrator — boots entities, manages sync loops, hot-reload |
| `orchestration/` | Extracted orchestrator helpers (sync loops, config diffing, room invitations) |
| `runtime_state.py` | Shared runtime readiness state for health/ready endpoints |
| `runtime_resolution.py` | Authoritative runtime resolution for agent materialization |
| `team_runtime_resolution.py` | Runtime resolution for team member materialization |
| `agent_policy.py` | Derives canonical execution policies from authored agent config |
| `workspaces.py` | Agent workspace scaffolding, template seeding, context file resolution |
| `bot.py` | AgentBot and TeamBot runtime for Matrix event handling |
| `routing.py` | Intelligent agent selection when no agent is mentioned |
| `streaming.py` | Response streaming via progressive message edits |
| `media_inputs.py` | Shared media-input container passed across bot, teams, and AI layers |
| `media_fallback.py` | Retries model requests without inline media when models reject media inputs |
| `avatar_generation.py` | Generates and manages avatar assets for agents, rooms, and spaces |
| `response_tracker.py` | Duplicate response prevention |
| `topic_generator.py` | AI-generated room topics |
| `background_tasks.py` | Non-blocking async task management with GC protection |

## Data Flow

1. **Message arrives** from Matrix homeserver
2. **Router decides** which agent should handle it (if no explicit mention)
3. **Agent processes** the message using the Agno runtime
4. **Tools execute** as needed (file operations, API calls, etc.)
5. **Response sent** back to Matrix room
6. **Memory updates** asynchronously in background
