Summary: No meaningful duplication found in `src/mindroom/matrix/cache/agent_message_snapshot.py`.
`AgentMessageSnapshot` and `AgentMessageSnapshotUnavailable` are single-purpose public contract symbols shared by the SQLite/Postgres cache readers, the cache protocol, bot hook wiring, and hook context.
The backend reader modules contain parallel snapshot-loading behavior, but this primary file does not duplicate that behavior.

```coverage-tsv
qualname	kind	lines	status	search_terms	candidates_checked
AgentMessageSnapshot	class	lines 10-14	none-found	AgentMessageSnapshot, latest visible cached message, content origin_server_ts, get_latest_agent_message_snapshot	src/mindroom/matrix/cache/sqlite_agent_message_snapshot.py:14; src/mindroom/matrix/cache/sqlite_agent_message_snapshot.py:109; src/mindroom/matrix/cache/postgres_agent_message_snapshot.py:15; src/mindroom/matrix/cache/postgres_agent_message_snapshot.py:114; src/mindroom/matrix/cache/event_cache.py:75; src/mindroom/hooks/context.py:49; src/mindroom/hooks/context.py:343
AgentMessageSnapshotUnavailable	class	lines 17-18	none-found	AgentMessageSnapshotUnavailable, snapshot unavailable, cache snapshot corrupt, failed to read Matrix event cache snapshot	src/mindroom/matrix/cache/sqlite_agent_message_snapshot.py:14; src/mindroom/matrix/cache/sqlite_agent_message_snapshot.py:73; src/mindroom/matrix/cache/sqlite_agent_message_snapshot.py:209; src/mindroom/matrix/cache/sqlite_agent_message_snapshot.py:212; src/mindroom/matrix/cache/postgres_agent_message_snapshot.py:15; src/mindroom/matrix/cache/postgres_agent_message_snapshot.py:76; src/mindroom/matrix/cache/postgres_agent_message_snapshot.py:221; src/mindroom/matrix/cache/postgres_agent_message_snapshot.py:224; src/mindroom/matrix/cache/event_cache.py:23
```

Findings:

No real duplication was found for the two symbols in the primary file.
`AgentMessageSnapshot` is the canonical DTO for hook-facing latest-message snapshots, and other files import or instantiate it rather than redefining an equivalent shape.
`AgentMessageSnapshotUnavailable` is the canonical exception for logical snapshot-read failure, and the closest related exception, `EventCacheBackendUnavailableError` in `src/mindroom/matrix/cache/event_cache.py:23`, has a different contract for temporary backend unavailability.

Related-only observation outside the primary file: `src/mindroom/matrix/cache/sqlite_agent_message_snapshot.py:21-212` and `src/mindroom/matrix/cache/postgres_agent_message_snapshot.py:22-224` have near-identical lookup flow for latest agent message snapshots.
That duplication belongs to the backend reader implementations, not to `agent_message_snapshot.py`.
Differences to preserve include connection/cursor types, namespace handling for Postgres, SQL placeholder syntax, table names, ordering columns, and backend-specific database exceptions.

Proposed generalization:

No refactor recommended for `src/mindroom/matrix/cache/agent_message_snapshot.py`.
If the backend reader duplication is audited under either reader module, a minimal shared pure helper could extract event-level logic such as visible content selection, scope matching, timestamp validation, and `_SnapshotLookupResult`, while leaving SQL and database exception handling backend-specific.

Risk/tests:

No production change is recommended, so there is no immediate behavior risk.
If the related backend-reader duplication is later refactored, tests should cover room-scope vs thread-scope lookup, edit replacement handling, runtime-start cutoff behavior, corrupt JSON errors, thread-cache rejection mapping, and backend-specific read failures for both SQLite and Postgres.
