# Response preparation, startup maintenance, and export completeness

## September 2026 changes

Published Chroma collection checks now read fresh SQLite metadata without starting a native vector-search process.
The read is scoped to the default tenant/database, is read-only, and has a 100 ms lock deadline.
Missing collections remain missing; locked or corrupt metadata raises an error rather than falsely reporting an empty index.
Native vector searches remain isolated in bounded subprocesses.
Semantic file-memory search uses the existing asynchronous search path, which overlaps embedding with native worker startup.
No cross-turn freshness cache or memory/history truncation is introduced.

`Dispatch pipeline timing` now includes `diag_knowledge_access_ms` for the knowledge-resolution work before AI preparation.
With `MINDROOM_TIMING=1`, startup recovery also reports separate `startup_recovery.history`, `startup_recovery.canonical_messages`, and `startup_recovery.requesters` timings and work counts.

Startup orphan cleanup and managed-room reconciliation use four workers each.
Operations within one room remain sequential, including aliases that resolve to the same physical room, and cancellation drains the workers.
The existing invitation, membership, and root-space retry passes remain intact.
MCP collision projection loads shared plugins once per projection rather than once per agent.
Stale-stream cleanup resolves requester chains only for recovery candidates, while retaining the full resolved-message map for intermediate reply-chain lookups.
Failed history pages remain eligible for the next recovery pass.

## Complete thread exports

Strict hydration never records an unreadable walk as a permanently spent export attempt.
Missing keys or malformed events produce an explicit unreadability error; a later export can retry after the history becomes readable.
Export policy rank 30 replaces rank 20 so previously ambiguous incomplete export markers receive a fresh attempt without changing or deleting stored messages.
Already-complete histories stay warm, and genuinely exhausted safety ceilings still prevent repeating the same maximum-cost walk.
The narrow compatibility adapter in `matrix/legacy_media_edits.py` validates historical file replacements that omitted the URL from their outer fallback.
It preserves the authored source and full sidecar contents and applies only to historical reads, not actionable ingress.
The writer already produces valid file-edit fallbacks.
Unavailable encryption keys still prevent a complete export; this change does not fabricate missing plaintext.

## Isolated measurements

Run `uv run scripts/benchmark_runtime_preparation.py` with the repository's complete environment.
It creates a disposable synthetic Chroma collection, runs five interleaved samples, and checks identical search results.
The fake embedder waits 250 ms; no provider, Matrix account, live index, or credential is used.

Observed medians on September 12, 2026, Python 3.13:

| Operation | Before | After |
| --- | ---: | ---: |
| Fresh collection existence check | 845.198 ms | 0.645 ms |
| Search with synthetic 250 ms embedding | 1,074.942 ms | 823.145 ms |
| Requester lookups for 20 completed replies plus one interrupted reply | 21 requests | 1 request |
| Plugin loads for one three-agent collision projection | 3 | 1 |

These are isolated timings and deterministic regression counts, not deployed end-to-end latency.
The search rows include native worker startup and vector retrieval, not only orchestration overhead.
Production's previously observed 86–89 second stale-recovery phase also includes history pagination and canonical content resolution; the new stage timings distinguish these costs after deployment.

## Separate event-loop work

Durable Nio SQLite commits still execute synchronously in `mindroom-nio` 1.0.4.
They cannot safely be moved individually to threads: durable transactions, Olm state, room projections, and synchronous crypto APIs currently share ownership.
A complete nonblocking fix requires a separate upstream async ownership boundary, with committed snapshots and cancellation-safe resource ownership.
This MindRoom change does not claim to remove those commit stalls or weaken FULL durability.
