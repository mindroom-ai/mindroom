---
icon: lucide/waypoints
---

# Migration and compatibility boundaries

This page records the decisions behind MindRoom's handling of historical formats and which migrations its dependencies own.
It covers the current source and does not promise upgrades from every earlier release.
Each `# LEGACY_COMPAT:` source comment owns its rule's legacy format, release cutoff, handling, and coverage; list every rule, including the SaaS backend and scripts, from the repository root:

```bash
rg -n -F 'LEGACY_COMPAT:' --glob '*.{py,yaml}'
```

Paths below are relative to `src/mindroom/` unless they start with `scripts/` or `saas-platform/`.

## Dependency-owned migrations

- Nio owns its crypto store schema and preserves crypto and trust records; `matrix/legacy_crypto_upgrade.py` isolates only the pre-durable cutoff.
- Chroma owns storage-engine migrations; `knowledge/indexing_config.py` owns corpus compatibility.
- Mem0 owns its history-table rewrite and its default history path, which may lie outside MindRoom storage; `memory/config.py` leaves both to Mem0.
- Agno owns its session readers, but MindRoom never invokes Agno's historical migration manager; MindRoom turns Agno 2 run blobs into run rows itself.

## Decisions and invariants

- An incompatible format is not a locked database, permission failure, missing key, full disk, or corrupt current schema; migration owners reject those failures instead of converting them into deletion, and reset is an explicit owner policy, never a generic exception fallback.
- Sparse readers may ignore unknown fields or drop malformed records that can be rebuilt, but Agno session databases are not rebuildable caches, because they hold the conversation history the models see, so an incompatible one is archived.
- A conversation database from an earlier release converts once, when it opens and before any response loads it, in the transaction that creates the summary-row tables: Agno 2 run blobs and rows without a position become positioned run rows, and destructive-compaction state and the compaction archive become summary rows, because the loader reads only that shape.
- Upgrades run while no reply is in flight, so reply records adopt no stream, queued delivery, or resume an earlier release left.
  Pending approvals cannot be ruled out, so the upgrade cancels them; no continuation therefore rebuilds historical context, origin, or tool-call visibility.
- The Nio ownership cutoff drops unfinished journal work from before it, including interactive tables, the old response outbox and delivery tables, and approval transport, instead of an additive conversion chain that archives, translates, or tombstones it.
- Provider, Matrix protocol, dependency, and cancellation adapters are current behavior, not legacy compatibility, because they remain necessary even after a database reset.
- Usage diagnostics, thread model overrides, invited rooms, and the tag vocabulary cache deliberately use weak retention, so their old formats get no conversion chain.
- Authoritative SaaS account, subscription, instance, payment, usage, audit, and grant data stays under the explicit SQL migrations in `saas-platform/supabase/migrations/`.
- `scripts/local_mindroom_provisioning_service.py` imports its old `state.json` itself instead of through a `legacy_` module, because the service deploys as one standalone file.
- `egress/policy.py` keeps the older allowlist path environment name as current behavior, not legacy compatibility, because it is a deny-safe alias.
- Private-storage migration support can be removed only once an explicit supported upgrade floor requires an intermediate release that contains it, because installations skip releases and restore older backups, so a fixed number of releases is not enough.
  Later releases must keep rejecting unsupported historical layouts and direct operators to that intermediate upgrade.

## Limitations

- An old invited-room ledger that fails validation is not moved below `tracking/`, so that entity leaves the rooms it kept on its first room pass after the upgrade.

See [Upgrade and reset limits](../deployment/upgrades.md#upgrade-and-reset-limits) for the operator view.
