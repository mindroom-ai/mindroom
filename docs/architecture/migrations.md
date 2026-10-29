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
- `knowledge/indexing_config.py` owns knowledge index compatibility, including the vector store's schema version, so an index of another version is rebuilt rather than migrated.
- Mem0 owns its history-table rewrite; `memory/config.py` puts each storage root's history database beside its Chroma store.
  The shared default-path history database that earlier releases left to Mem0 is not carried over: MindRoom never reads its operation history, and its recent-message rows are keyed by agent or team scope without the requester, so they cannot be assigned to one private instance.

## Decisions and invariants

- An incompatible format is not a locked database, permission failure, missing key, full disk, or corrupt current schema; migration owners reject those failures instead of converting them into deletion, and reset is an explicit owner policy, never a generic exception fallback.
- Sparse readers may ignore unknown fields or drop malformed records that can be rebuilt, but conversation databases are not rebuildable caches, because they hold the conversation history the models see, so an incompatible one is archived.
- Conversation databases convert one way from the Agno layout to storage v2, with a byte-for-byte backup beside each that MindRoom never deletes and no supported downgrade, because keeping both layouts readable needed triggers and a reconversion that could not tell which conversations an older release wrote (see `docs/dev/2026-10-25-conversation-storage-rows.md`).
  A startup sweep converts every agent's, team's, and private instance's database and the system usage database before any bot or the API starts, so no reply waits on a large conversion and ready means converted, and every store open runs the same entry, which converts a database the sweep skipped and a restored backup, at the cost of reading its layout version on a converted one.
  The sweep logs and skips a database it cannot read, lock, or convert, because worker code can write session directories and one database must not stop startup; that database's own open then fails with the error.
  The earlier one-time steps (usage-ledger seeding, summary-row adoption, and Agno 2 run blobs and positions) run only inside the conversion, behind its format marker, because they recognize an old database by a missing table, and a converted one lacks the Agno tables too.
- A step that changes an already converted database bumps the layout version in `*_log_meta`, and every store open and the startup sweep run it, because each knows whose database it opens.
  Layout version 3 is such a step: it sets an agent's session rows and their runs to name the agent, since the database alone cannot tell an agent's old display-name id from the owner that a team's agent rows or the system ledger's rows rightly name.
  Layout version 4 records that owner once, in the meta row, and drops the session rows' Agno kind and owner columns, because each database has exactly one owner, while per-row owners let a row of another kind, such as the agent row a team bot's interrupted first reply created, hide a conversation from its team and from the team's usage.
  The startup sweep takes a team database's owner from the team its rows name whose digest gives the database's name, since a team directory's name does not reverse to its scope; a database no row names holds no conversation, and converts at that scope's next store open instead.
  The same step moves `session_state` out of `session_data` into its own column, so a state write and a usage-totals write no longer each read and rewrite the other's object; it backs each database up first, as storage v2 did, because an older release started on the converted file archives its sessions directory.
- A historical shape the conversion can rewrite, such as combined tool results, stored tool schemas, history copies, and legacy tool-call shapes, is normalized once by it instead of being read at every load; a shape it cannot rewrite stays a runtime reader, such as a summary inside a paused run's saved system message, which a resume reuses byte for byte.
- Upgrades run while no reply is in flight, so reply records adopt no stream, queued delivery, or resume an earlier release left.
  Pending approvals cannot be ruled out, so the reply-records upgrade cancels them; no continuation therefore rebuilds historical context, origin, or tool-call visibility.
- The one-row-per-call approval conversion keeps pending approvals instead, because every fact a decision or resume reads already exists in the old rows; only a background job's pending call is denied, as the restart that runs the conversion denies every job still waiting (see `docs/dev/2026-10-27-approval-records-design.md`).
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
- Downgrading below conversation storage v2, or below layout version 4, means restoring each database's backup of that conversion, which loses every write made since the upgrade.

See [Upgrade and reset limits](../deployment/upgrades.md#upgrade-and-reset-limits) for the operator view.
