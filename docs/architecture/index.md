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
│  │  (Mem0, file, or none; agent/team scopes)       │    │
│  └─────────────────────────────────────────────────┘    │
└─────────────────────────────────────────────────────────┘
```

## Components

- [Matrix Integration](matrix.md) - How MindRoom connects to Matrix
- [Internal Turn CLI](agent-cli.md) - Minimal-mode discovery, response ownership, and approval recovery
- [Agent Orchestration](orchestration.md) - How agents are managed
- [Bot Runtime](bot-runtime.md) - The inbound turn pipeline and its module boundaries
- [Code Map](code-map.md) - The inbound turn pipeline at module level, key modules and their purpose, and where persistent state lives
- [Migration and Compatibility Boundaries](migrations.md) - Current owners for historical formats, dependency migrations, and retained compatibility
- [Matrix Event-Journal Security](matrix-event-journal-security.md) - Which decrypted plaintext is durable, who owns it, and what removes it
- [Matrix Event-Journal Contracts](../dev/matrix-event-journal-contracts.md) - What the journal guarantees, and the homeserver behaviour you would otherwise rediscover by debugging

## Storage upgrade boundaries

Historical formats stay with their storage or lifecycle owners, while current callers consume canonical identities and paths.
`legacy_private_storage_aliases.py` owns historical requester spellings and verified aliases; only startup migration, `private_storage_paths.py`, and `usage_stats_storage.py` can import it.
Usage discovery uses verified aliases only to classify coverage, skipping duplicate historical paths while scanning their canonical directories.
Worker mount planning and sandbox path validation use `private_storage_paths.py`, while `private_instance_identity_store.py` validates current identities.

`oauth/legacy_credentials.py` owns publication-field normalization and lossless historical requester bindings.
Only `oauth/credential_store.py` can import it; the store retains schema, scope validation, current credential state, transaction locks, retries, and commit ownership.
OAuth credentials stored only in legacy JSON files require reconnection; those files and their obsolete sidecars remain untouched.

Existing lifecycle adapters remain at their focused entry points: `legacy_private_storage.py` at startup, `config/legacy_access.py` during config loading, and Nio journal and crypto adapters when their stores open.
`session_storage_preflight.py` checks owned session databases before opening them and archives session directories whose tables lack required columns; see [Session Storage Recovery](orchestration.md#session-storage-recovery).
Tach visibility rules keep compatibility internals behind their owning boundaries.

## Data Flow

1. **Message arrives** from the Matrix homeserver; `matrix/durable_ingestion.py` validates the owned batch, invokes the journal transaction owner to commit it, runs ordered hooks/callbacks, and acknowledges nio.
   `matrix/journal_ingress.py` supplies typed classification from nio provenance and replay reconstruction.
   `journal_dispatch.py` hands admitted events through `bot.py` to `turn_controller.py`, which owns the turn from ingress to recorded outcome.
2. **Input is validated, normalized, and resolved**: `ingress_validation.py` checks trust and the effective requester, deduplicates handled event ids, and drops trusted router echoes; `inbound_turn_normalizer.py` shapes raw text, voice, and media into canonical turn inputs, and `conversation_resolver.py` resolves thread identity and history; `!commands` are control inputs that dispatch directly here instead of entering coalescing
3. **Messages are ordered and coalesced**: `ingress_lanes.py` delivers each sender's messages in receipt order (late-ready voice/STT waits in the lane), and `coalescing.py` batches each sender's live conversation burst.
   Ordinary text completes an utterance and dispatches immediately; adaptive text waits its configured quiet period.
   Later adaptive text cannot extend an earlier immediate text's wait.
   A live batch ending in media waits for more attachments or a trailing caption.
   Follow-up backlogs queued behind an active response flush at idle in receipt order, one combined turn per consecutive same-requester run, so no sender's messages execute under another sender's identity; conversations never wait on each other.
4. **The turn is planned**: `turn_policy.py` decides to ignore, route, or respond; a direct responder is resolved when one eligible agent or team remains, otherwise the router selects among candidates
5. **Selected entity processes** the message via `response_runner.py` and the Agno runtime, executing tools as needed
6. **Response is delivered** through `delivery_gateway.py`, which owns Matrix send/edit/finalization while `streaming.py` owns progressive response state
7. **The turn is recorded** in the durable handled-turn ledger (`turn_store.py` / `handled_turns.py`) so restarts do not double-reply
8. **Memory updates** asynchronously in background

See [Bot Runtime](bot-runtime.md) for the module boundaries and the ongoing simplification roadmap.

## Confined filesystem paths

`mindroom.path_confinement.resolve_path_within_root` is the shared implementation for workspace and artifact path containment.
Callers supply an authorized root and explicitly choose `internal` (allow links targeting that root), `reject` (reject descendant links), or `preserve_leaf` (reject linked parents while preserving the final entry for unlink or atomic replacement).
Existing workspace and tool wrappers retain their input rules and error messages.
Do not add another `resolve()` plus containment check when implementing a file consumer.

Path resolution is a point-in-time check, not protection for a later file open.
For local I/O requiring protection against symlink swaps, use `open_directory_within_root` or `open_regular_file_within_root` and keep operations relative to the returned descriptor.
Publish bytes using `atomic_file.atomic_write_bytes_at`, or stream through `atomic_write_file_at`; both use the same atomic transaction and descriptor-relative cleanup.
The action-based browser publishes captures and completed downloads through these helpers and snapshots upload sources through confined descriptors.
Upload snapshots remain private until tab or profile teardown because Playwright reads selected files lazily.
Drive downloads stream into a confined atomic file rooted at the authorized workspace.
Caller-owned root authorization, directory identity checks, filesystem permissions, and requester isolation remain required.
These helpers do not prevent hard-link aliasing or arbitrary directory relocation, and do not protect subsequent pathname opens by native MCP browser tools, Drive uploads, shell commands, or ordinary file-tool implementations.
