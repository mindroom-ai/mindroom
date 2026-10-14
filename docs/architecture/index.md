---
icon: lucide/layout
---

# Architecture

## Components

- [Matrix Integration](matrix.md) - The Matrix identity invariant and the model-selection client protocol
- [Internal Turn CLI](agent-cli.md) - Why minimal-mode CLI calls are owned by one response turn, and how they recover
- [Agent Orchestration](orchestration.md) - Room setup, session-storage recovery, and runtime replacement rationale
- [Bot Runtime](bot-runtime.md) - Why the turn runtime is split as it is, the invariants its seams protect, and planned simplifications
- [Reply Messages](reply-messages.md) - The outcomes the reply design rules out, the decisions behind it, its accepted limitations, and the invariants tests pin
- [Code Map](code-map.md) - The inbound turn pipeline at module level, key modules and their purpose, and where persistent state lives
- [Migration and Compatibility Boundaries](migrations.md) - Why historical formats are handled as they are, and which migrations dependencies own
- [Matrix Event-Journal Security](matrix-event-journal-security.md) - Which decrypted plaintext is durable, who owns it, and what removes it
- [Background Tool Jobs](background-tool-jobs.md) - The decisions, invariant purposes, and limits behind background tool jobs
- [Matrix Event-Journal Contracts](matrix-event-journal-contracts.md) - Event-journal decisions the code does not explain, and the homeserver behaviour you would otherwise rediscover by debugging

## Storage upgrade boundaries

Historical formats stay with their storage or lifecycle owners, while current callers consume canonical identities and paths.
[Migration and Compatibility Boundaries](migrations.md) records the decisions behind them.

## Confined filesystem paths

Use `mindroom.path_confinement.resolve_path_within_root`, the shared implementation for workspace and artifact path containment, and choose its symlink policy explicitly; do not add another `resolve()` plus containment check when implementing a file consumer.

Path resolution is a point-in-time check, not protection for a later file open.
Local I/O that must withstand symlink swaps uses `open_directory_within_root` or `open_regular_file_within_root` and keeps operations relative to the returned descriptor.
Publish bytes with `atomic_file.atomic_write_bytes_at`, or stream through `atomic_write_file_at`, so every writer shares one atomic transaction and descriptor-relative cleanup.

Callers still own root authorization, directory identity checks, filesystem permissions, and requester isolation.
The helpers do not prevent reading through hard-link aliases or arbitrary directory relocation, and they do not protect code that reopens a pathname itself, such as shell commands and native MCP browser tools.
