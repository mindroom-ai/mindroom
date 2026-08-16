# Read-Only Usage Statistics Tool Design

Date: 2026-08-16

Status: Proposed

## Summary

MindRoom will add a `usage_stats` tool that reads persisted Agno session databases and returns aggregate model-usage statistics without modifying runtime state.
The normal capability reports only the current agent's usage attributable to the current human requester.
An agent can be configured with `admin_scope: true` to expose an additional all-entities query, but every admin call must also come from a requester in `authorization.global_users` after alias resolution.
The implementation will open SQLite databases in enforced read-only mode, parse retained run metrics, aggregate in memory, and return only statistical fields.

## Goals

- Let an agent inspect its own token usage, run counts, model mix, recorded cost, and observation range for the current requester.
- Make the self query work for shared agents and requester-private agents.
- Let an explicitly configured admin agent inspect aggregate usage across configured agents, private instances, and team execution scopes.
- Keep every tool operation read only at the filesystem and database layers.
- Preserve user isolation even when several users interact with a shared agent or team.
- Return honest completeness information when compaction or malformed legacy data prevents exact historical reporting.

## Non-Goals

- This version will not create a new usage ledger, materialized view, migration, cache, or scheduled aggregation job.
- This version will not call provider billing APIs or claim that recorded cost equals an invoice.
- This version will not count usage that is not represented in Agno run metrics, such as standalone embedding, speech-to-text, or provider activity outside a persisted run.
- This version will not expose prompts, responses, tool arguments, session IDs, room IDs, thread IDs, or Matrix event IDs.
- This version will not promise complete time-series history after destructive history compaction.

## Considered Approaches

### Read Agno Runs On Demand

This approach opens canonical session databases with SQLite read-only connections and aggregates run JSON in memory.
It is the selected approach because it satisfies the read-only requirement, works with the existing storage model, and permits run-level requester filtering.
Its main limitation is that compacted runs are no longer available for exact time-window attribution.

### Use Agno's Metrics Table

Agno provides `calculate_metrics()` and `get_metrics()` APIs.
The calculation API writes an `agno_metrics` table, which violates the requirement that tool operations remain read only.
The current calculation also buckets a session's cumulative metrics by the session creation date and cannot enforce MindRoom's run-level requester boundary.
This approach is rejected.

### Add an Append-Only Usage Ledger

A new ledger written at response completion would provide exact historical time ranges after compaction and faster queries.
It would add a write path, migration, reconciliation logic, and a second source of truth.
This approach is deferred until retained-run coverage proves insufficient in practice.

## Tool Surface

The feature is one registered tool named `usage_stats`.
The tool always registers `get_my_usage`.
The tool registers `get_all_usage` only when its authored `admin_scope` override is true.

Example configuration:

```yaml
agents:
  assistant:
    tools: [usage_stats]

  usage_admin:
    tools:
      - usage_stats:
          admin_scope: true
```

The `admin_scope` flag controls which function is visible to the model, but it does not grant human authorization.
The flag will be declared as a non-secret boolean tool configuration field and an agent override field.
The tool will always run in the primary runtime and will be added to the local-only tool set so a worker cannot receive broad storage visibility.

### `get_my_usage`

The function accepts optional ISO 8601 `start` and `end` values and a `group_by` value of `day` or `model`.
An omitted `start` means the beginning of retained run history.
An omitted `end` means the query start time of the tool call.
The start boundary is inclusive and the end boundary is exclusive.
Date-only values use the configured MindRoom timezone, while timestamp values must include an offset or use `Z`.
The function cannot select another agent or requester.

The query includes direct runs for the current agent and matching nested member runs stored in team sessions.
Private agents resolve only the exact requester-specific runtime root produced by the existing runtime resolver.
Private agents cannot enumerate sibling requester roots.

### `get_all_usage`

The function accepts the same time boundaries plus a single `group_by` value of `entity`, `requester`, `model`, or `day`.
It also accepts optional `entity_names` and `requester_ids` filters.
The default breakdown is `entity`.
Requester filters are canonicalized through the configured Matrix alias map.

The function is authorized only when `admin_scope` is true and the canonical current requester is present in `authorization.global_users`.
An unauthorized call returns a structured error without scanning any database.
The admin query includes configured agents and team execution scopes because agent work performed inside a team otherwise exists only in the team database.
Router-only and non-Agno provider usage remains outside the first version.

## Authorization And Privacy

The tool obtains `agent_name`, `requester_id`, configuration, runtime paths, and execution identity from `ToolRuntimeContext`.
It never accepts a requester identity for the self query.
It resolves the current requester and persisted requester identities through `AuthorizationConfig.resolve_alias` before comparison.

Run ownership is determined from `run.metadata.requester_id` when present and falls back to `run.user_id` for older data.
A nested team member run inherits the verified requester identity of its parent only when the child does not carry one.
A self query excludes a run when no requester identity can be established.
An admin query includes such a run under an `unknown` requester bucket and increments the missing-attribution diagnostic.

Row-level `session.user_id` is not sufficient for self authorization because one durable Matrix conversation can involve more than one human requester.
The self boundary is therefore enforced for every retained run before metrics are added to an aggregate.
The result serializer exposes only aggregate dimensions and counters.

## Storage Discovery

The self query resolves the current agent's canonical `session_state_root` with `resolve_agent_runtime` and the live execution identity.
A shared agent therefore reaches its shared session database, while a private agent reaches only its requester-specific private instance.
The self query may also read team databases, but it retains only nested runs whose entity and canonical requester both match the current call.

The admin query maps the canonical storage root through `resolve_session_state_root` before deriving shared agent, private instance, and team sources.
This preserves the relative storage shape when `MINDROOM_SESSION_STORAGE_PATH` redirects Agno sessions away from the main storage root.
It discovers private instance databases only below the fixed `private_instances/<worker-dir-name>/<agent>/sessions/<agent>.db` layout, where the worker directory is the normalized one-way name produced by existing worker-routing helpers.
Both queries discover team databases only below the canonical fixed `teams/<storage-name>/sessions/<storage-name>.db` layout because configured and ad hoc team runs can contain the only persisted copy of an agent member run.
Each team source requires the exact `<storage-name>_sessions` table, but the storage name is never treated as an entity ID because it contains a normalized scope plus a digest.
Discovery skips symlinks, requires every resolved candidate to remain below the effective session storage root, and validates persisted agent and configured-team IDs against the current configuration before aggregation.
Orphaned databases for removed entities are ignored in the first version.

Each source is represented by a typed immutable record containing the database path, exact expected session table, storage scope, optional direct-agent identity, and separate allowed agent and configured-team IDs.
No requester-provided string is interpolated into a filesystem path or SQL identifier.

## Read-Only Database Access

The reader uses the standard-library `sqlite3` module so importing the tool does not expand the slim import surface with an Agno or SQLAlchemy dependency.
Each database is opened through a URI containing `mode=ro`.
The connection also enables `PRAGMA query_only=ON`, uses a private page cache, and uses a bounded busy timeout.
The reader does not use Agno's `SqliteDb` constructor or metrics APIs because those APIs are not an enforceable read-only boundary.
The reader queries `sqlite_master` to confirm the expected table and validates the required session columns before selecting data.
SQLite may create WAL sidecars even for a `mode=ro` connection when the containing directory is writable.
The reader therefore inspects the database header without mutation and opens a WAL database only when its existing non-symlink `-wal` and `-shm` files are readable; otherwise that source is unavailable.
The reader does not use `immutable=1` because Agno can still be writing the database.
WAL-backed databases that satisfy this preflight remain readable while the runtime is writing, and each database query observes its own SQLite snapshot.
The response includes one `as_of` timestamp and does not claim a globally atomic snapshot across files.

Missing databases produce empty sources rather than creating files.
An unsupported schema or corrupt JSON produces a source diagnostic and does not abort other admin sources.
Source paths and storage-scope labels remain internal, and public diagnostics expose only bounded category counts.
The self query returns a partial result when safe retained records remain and an error when its only source cannot be read.

## Run Extraction And Attribution

The reader processes one session row at a time and never retains a complete source result set in memory.
It rejects oversized encoded JSON before decoding, applies explicit nesting-depth and extracted-node limits, and converts accepted rows into field-selective immutable records.
Those records contain only metrics, attribution, time, model, status, cost, deduplication, and nested-member fields.
Messages, media, summaries, prompts, tool payloads, and other run metadata are discarded during extraction and never enter the response model.

Each top-level agent run contributes its own metrics to that agent.
Each top-level team run contributes its leader metrics only when its persisted `team_id` names a currently configured team.
Top-level leader metrics for ad hoc teams are excluded from version one because their hashed storage name is not a configured entity identity.
Nested member responses are traversed recursively and contribute their metrics to their own agent or nested team entity.
The traversal deduplicates records by entity kind, entity ID, and stable run ID so a member response stored in more than one structural location is counted once.
Records without a stable run ID use a source-local structural key and increment a diagnostic.

The run's `created_at` timestamp controls time-window inclusion.
A nested response may inherit its parent's timestamp only when its own timestamp is absent.
A bounded query excludes a record whose timestamp cannot be established and reports that exclusion.

## Aggregation Model

The aggregator sums the persisted Agno fields `input_tokens`, `output_tokens`, `total_tokens`, `audio_input_tokens`, `audio_output_tokens`, `audio_total_tokens`, `cache_read_tokens`, `cache_write_tokens`, and `reasoning_tokens`.
The aggregator does not reinterpret provider-specific cache semantics or synthesize a missing provider total.
Per-model detail preserves model type, provider, and model ID when Agno stored those values.

The result includes top-level retained turn count, metered run count, contributing session count, first and last observed timestamps, status counts, and token counters.
Recorded cost is returned as a known subtotal together with counts of metered runs with and without a cost value.
The output never labels a partial recorded-cost subtotal as a complete bill.

One request may return at most 200 breakdown rows.
Rows are sorted by descending total tokens with a stable dimension tie-breaker.
The response reports `breakdown_truncated: true` and `breakdown_omitted` when the cap applies.

## Completeness Contract

MindRoom compaction removes old runs from Agno session history while Agno's cumulative `session_data.session_metrics` can remain larger than the retained-run sum.
The tool uses cumulative session metrics only as a coverage signal and never uses them to bypass requester filtering.
Cumulative-versus-retained comparisons are permitted only for an authorized admin query or a self query over physically requester-isolated private storage.
A shared-agent self query does not read or expose cumulative deltas because those session totals are not requester-attributable.

The response contains a `coverage` object with scanned source, session, and run counts; malformed record counts; missing requester and timestamp counts; and compacted-history information where it can be established without crossing the requester boundary.
The primary totals are explicitly named retained usage.
If cumulative metrics exceed retained metrics, the response states that time-series and requester attribution before compaction are unavailable from the read-only source.
A shared-agent self query reports historical completeness as unknown instead of exposing a cross-requester comparison.
The current in-flight tool-using run is naturally excluded because Agno persists it only after the tool returns.

## Response Shape

Both functions return the existing JSON custom-tool envelope.
A successful response has this conceptual shape:

```json
{
  "status": "ok",
  "tool": "usage_stats",
  "scope": "self",
  "as_of": "2026-08-16T18:00:00Z",
  "window": {
    "start": null,
    "end": "2026-08-16T18:00:00Z",
    "timezone": "America/Los_Angeles"
  },
  "retained_usage": {
    "turns": 42,
    "metered_runs": 42,
    "sessions": 7,
    "first_observed_at": "2026-08-01T10:30:00Z",
    "last_observed_at": "2026-08-16T17:45:00Z",
    "status_counts": {
      "completed": 40,
      "error": 2
    },
    "input_tokens": 120000,
    "output_tokens": 9000,
    "total_tokens": 129000,
    "cache_read_tokens": 70000,
    "reasoning_tokens": 1200,
    "recorded_cost": {
      "known": "1.42",
      "runs_with_cost": 38,
      "runs_without_cost": 4
    }
  },
  "breakdown": [],
  "coverage": {
    "status": "complete_retained",
    "scanned_sources": 1,
    "partial_sources": 0,
    "scanned_sessions": 7,
    "retained_runs": 42,
    "skipped_runs": 0,
    "malformed_runs": 0,
    "missing_requester_runs": 0,
    "missing_timestamp_runs": 0,
    "compacted_sessions": 0,
    "note": "No retained-history gap was detected."
  },
  "breakdown_truncated": false,
  "breakdown_omitted": 0
}
```

Numeric cost values are serialized as decimal strings to avoid presenting binary floating-point artifacts.
Error responses use the same envelope and include a stable error code plus a human-readable message.

## Components

`src/mindroom/usage_stats_storage.py` will own typed storage sources, symlink-safe source discovery, schema validation, and enforced read-only SQLite access.
`src/mindroom/usage_stats.py` will own typed queries, requester attribution, run traversal, aggregation, coverage analysis, and result dataclasses.
`src/mindroom/custom_tools/usage_stats.py` will own the Agno toolkit surface, runtime-context lookup, authorization gates, asynchronous thread offload, and JSON envelope construction.
`src/mindroom/tools/usage_stats.py` will own declarative registration metadata and the `admin_scope` override.
`src/mindroom/tool_system/worker_routing.py` will mark `usage_stats` as primary-runtime-only.
The generated tool metadata snapshot and tool documentation will be updated through the repository's existing generation workflow.

The core reader will depend on configuration and runtime-path leaf interfaces but will not depend on bot, orchestrator, Matrix client, or Agno runtime objects.
Synchronous SQLite work will run through `asyncio.to_thread` so a large read cannot block the Matrix event loop.

## Error Handling

Invalid time bounds, an end before a start, an unsupported breakdown, or an unknown admin filter returns a validation error without opening databases.
Missing runtime context returns a context-unavailable error.
An unauthorized admin call returns an authorization error before source discovery.
A busy or corrupt database contributes a bounded diagnostic and allows independent sources to continue.
A self query returns a source-unavailable error when every existing expected source is unreadable, while an all-absent source set remains a valid empty retained-history result.
Unreadability produces partial success only when at least one source was read safely and another existing source could not be read.
A proven retained-versus-cumulative compaction gap independently produces partial coverage even when every source is readable.
Unexpected programming errors are logged by the runtime and are not converted into fabricated zero usage.

## Test Strategy

- Build minimal Agno-compatible SQLite fixtures with agent, team, nested member, and cumulative session metrics.
- Assert that self queries include only the current canonical requester and current entity across direct and team-owned runs.
- Assert that aliases resolve to the canonical requester on both the live and persisted sides.
- Assert that private Alice resolves only Alice's session state root and cannot observe Bob's private instance.
- Assert that an admin-enabled agent still rejects a requester outside `authorization.global_users` before any database open.
- Assert that an authorized admin sees configured shared agents, private instances, and team-member usage without receiving message or session identifiers.
- Assert recursive team attribution and stable-run deduplication.
- Assert time-window boundary behavior, timezone handling, model breakdown, cost coverage, sorting, and truncation.
- Assert model breakdown rows preserve model type, provider, and model ID as separate fields.
- Assert compaction diagnostics when cumulative session metrics exceed retained metrics.
- Assert that a shared-agent self query never returns cumulative totals or deltas from a multi-requester session.
- Assert malformed JSON, missing tables, unsupported schemas, SQLite busy errors, and partial multi-source results.
- Assert a busy or corrupt sole existing self source returns an error while an all-absent self source returns empty retained usage.
- Assert WAL and rollback-journal reads create, remove, or modify no database-related directory entry or sidecar.
- Assert oversized, deeply nested, and high-node-count run payloads produce bounded diagnostics without retaining content.
- Assert that symlinked or escaping private-instance candidates are skipped.
- Assert that the database file bytes and modification timestamp do not change after every tool query.
- Assert that missing database paths remain absent after a query.
- Assert that a deliberate write through the reader connection fails under both `mode=ro` and `query_only`.
- Assert that the admin function is absent from the toolkit unless `admin_scope` is enabled.
- Run tool metadata, import-graph, configuration, and private-runtime regression tests in addition to the focused suite.

## Rollout And Future Extension

The first release should describe its totals as retained usage in model-facing tool descriptions and user documentation.
Operational feedback should determine whether compaction gaps justify a separate append-only ledger.
If a ledger is added later, the public tool response can retain the same aggregate contract while changing the internal source and upgrading coverage to exact historical usage.
