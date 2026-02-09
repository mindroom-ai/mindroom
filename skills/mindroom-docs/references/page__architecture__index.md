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
│  │  (Mem0 + ChromaDB, agent/room/team scopes)      │    │
│  └─────────────────────────────────────────────────┘    │
└─────────────────────────────────────────────────────────┘
```

## Components

- [Matrix Integration](https://docs.mindroom.chat/architecture/matrix/index.md) - How MindRoom connects to Matrix
- [Agent Orchestration](https://docs.mindroom.chat/architecture/orchestration/index.md) - How agents are managed

## Data Flow

1. **Message arrives** from Matrix homeserver
1. **Router decides** which agent should handle it (if no explicit mention)
1. **Agent processes** the message using the Agno runtime
1. **Tools execute** as needed (file operations, API calls, etc.)
1. **Response sent** back to Matrix room
1. **Memory updates** asynchronously in background
