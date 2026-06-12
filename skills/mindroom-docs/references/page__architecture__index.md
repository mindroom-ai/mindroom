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

- [Matrix Integration](https://docs.mindroom.chat/architecture/matrix/) - How MindRoom connects to Matrix
- [Agent Orchestration](https://docs.mindroom.chat/architecture/orchestration/) - How agents are managed
- [Bot Runtime](https://docs.mindroom.chat/architecture/bot-runtime/) - The inbound turn pipeline and its module boundaries

## Key Internal Modules

| Module | Purpose |
|--------|---------|
| `orchestrator.py` | MultiAgentOrchestrator — boots entities, manages sync loops, hot-reload |
| `orchestration/` | Extracted orchestrator helpers (sync loops, config diffing, room invitations) |
| `orchestration/config_lifecycle.py` | Debounced config-reload lifecycle: queueing, response drain, and update-plan dispatch |
| `runtime_state.py` | Shared runtime readiness state for health/ready endpoints |
| `runtime_resolution.py` | Authoritative runtime resolution for agent materialization |
| `team_exact_members.py` | Runtime resolution for team member materialization |
| `model_loading.py` | Authoritative model instantiation and provider-specific loader selection |
| `ai_runtime.py` | Agent-run input preparation, queued-notice hooks, and inline-media fallback helpers |
| `agent_storage.py` | Agent session and learning SQLite storage construction helpers |
| `agent_descriptions.py` | Shared agent description rendering for routing and delegation |
| `agent_policy.py` | Derives canonical execution policies from authored agent config |
| `workspaces.py` | Agent workspace scaffolding, template seeding, context file resolution |
| `bot.py` | AgentBot and TeamBot runtime shells for Matrix lifecycle and sync callbacks |
| `turn_controller.py` | TurnController — owns one inbound turn from ingress to recorded outcome |
| `ingress_validation.py` | Ingress boundary validation: trust, effective requester, handled-id dedup, router-echo drop, command detection |
| `inbound_turn_normalizer.py` | Raw input shaping (text, voice, sidecars, media) into canonical turn inputs |
| `conversation_resolver.py` | Conversation identity, thread history, and ingress envelope assembly |
| `ingress_lanes.py` | Per-(room, sender) receipt-order FIFO delivering resolving ingress (voice/STT readiness) to conversations |
| `coalescing.py` | Live message coalescing gate (text dispatches immediately; media waits for attachments and a trailing caption) |
| `text_ingress_dispatch.py` | Text ingress dispatch path used by TurnController |
| `turn_policy.py` | Pure turn policy: decide ignore, route, or respond for inbound turns |
| `turn_store.py` | Unified durable turn access (wraps the handled-turn ledger) |
| `handled_turns.py` | Disk-backed handled-turn ledger preventing duplicate responses |
| `response_runner.py` | Response lifecycle execution (locking, streaming vs non-streaming, cancellation, detached inbox responses, shutdown drains) |
| `response_lifecycle.py` | Shared response lifecycle helpers and queued-notice state |
| `execution_preparation.py` | Request-scoped execution preparation for prompts and persisted replay |
| `response_payload_preparation.py` | Execution-side, under-lock assembly of one response's payload from immutable ingress inputs |
| `delivery_gateway.py` | Visible Matrix delivery for already-generated responses (send, edit, finalize) |
| `post_response_effects.py` | Shared post-response effects after Matrix delivery |
| `routing.py` | Intelligent agent or team selection when no entity is mentioned |
| `streaming.py` | Streaming state machine: placeholder, progressive edits, tool traces, cancellation |
| `media_inputs.py` | Shared media-input container passed across bot, teams, and AI layers |
| `media_fallback.py` | Retries model requests without inline media when models reject media inputs |
| `avatar_generation.py` | Generates and manages avatar assets for agents, rooms, and spaces |
| `topic_generator.py` | AI-generated room topics |
| `background_tasks.py` | Non-blocking async task management with GC protection |

## Data Flow

1. **Message arrives** from the Matrix homeserver and `bot.py` hands it to `turn_controller.py`, which owns the turn from ingress to recorded outcome
2. **Input is validated, normalized, and resolved**: `ingress_validation.py` checks trust and the effective requester, deduplicates handled event ids, and drops trusted router echoes; `inbound_turn_normalizer.py` shapes raw text, voice, and media into canonical turn inputs, and `conversation_resolver.py` resolves thread identity and history; `!commands` are control inputs that dispatch directly here instead of entering coalescing
3. **Messages are ordered and coalesced**: `ingress_lanes.py` delivers each sender's messages in receipt order (late-ready voice/STT waits in the lane), and `coalescing.py` batches per conversation — a batch ending in text is a complete utterance and dispatches immediately, while a batch ending in media waits a debounce window for more attachments or a trailing caption; conversations never wait on each other
4. **The turn is planned**: `turn_policy.py` decides to ignore, route, or respond; a direct responder is resolved when one eligible agent or team remains, otherwise the router selects among candidates
5. **Selected entity processes** the message via `response_runner.py` and the Agno runtime, executing tools as needed
6. **Response is delivered** through `streaming.py` (progressive edits) and `delivery_gateway.py` (Matrix send/edit)
7. **The turn is recorded** in the durable handled-turn ledger (`turn_store.py` / `handled_turns.py`) so restarts do not double-reply
8. **Memory updates** asynchronously in background

See [Bot Runtime](https://docs.mindroom.chat/architecture/bot-runtime/) for the module boundaries and the ongoing simplification roadmap.
