# Data Storage & Journal

This page covers where MindRoom keeps its data, what to back up, how to configure the event journal, and how to move or rebind the journal with `mindroom journal adopt`.

## Data Persistence

MindRoom stores data in `mindroom_data/` next to `config.yaml` by default; set `MINDROOM_STORAGE_PATH` to use another directory.

- `agents/*/sessions/` and `teams/*/sessions/` - Conversation history (SQLite)
- `agents/*/learning/` - Per-agent Agno Learning state when learning is enabled
- `agents/*/chroma/` - Per-agent Mem0 ChromaDB storage
- `private_instances/` - The same per-agent directories for private agents, one tree per requester scope
- `knowledge_db/` - Knowledge base vector stores
- `tracking/` - Durable response and callback state, including the SQLite event journal, that prevents duplicate replies across restarts
- `credentials/` - Secrets synchronized from `.env`
- `logs/` - Application logs
- `matrix_state.yaml` - Matrix connection state
- `encryption_keys/` - Matrix E2EE keys (if enabled)

Set `MINDROOM_SESSION_STORAGE_PATH` to move agent and team session databases to a separate root; learning and memory stay under the storage directory.

### Backups

Back up the whole storage directory, and keep `tracking/` on persistent storage.
When `MINDROOM_SESSION_STORAGE_PATH` is set in a container, mount that path on persistent storage and back it up too.

`tracking/` keeps a small record of every handled event so restarts and resyncs never answer the same message twice.
These records are never pruned automatically, so size and monitor the volume for growth over the install's lifetime.
See [Bot Runtime Architecture](https://docs.mindroom.chat/architecture/bot-runtime/#durable-dispatch-boundary) to inspect the records or remediate a corrupted one.

### Incompatible session databases

If an upgraded Agno version requires session columns that an agent's or team's session database lacks, MindRoom moves that `sessions/` directory aside to `sessions.incompatible-<id>/` and starts a fresh conversation history.
The archive is kept intact for inspection or manual recovery; back it up and delete it when you no longer need it.

## Event Journal

The event journal records which Matrix events were already handled and delivered, so restarts do not produce duplicate replies.
By default it is SQLite at `<storage>/tracking/event_journal.db`, a location that cannot be changed separately.
To use PostgreSQL, install the `postgres` extra (for example `uvx --from 'mindroom[postgres]' mindroom run`, or `--extra postgres` when syncing a source checkout), then select the backend and provide a connection URL:

```yaml
event_journal:
  backend: postgres
  database_url_env: MINDROOM_EVENT_CACHE_DATABASE_URL
```

| Field | Type | Default | Meaning |
| --- | --- | --- | --- |
| `backend` | `sqlite` or `postgres` | `sqlite` | Journal storage backend; a URL alone does not switch to PostgreSQL |
| `database_url_env` | string | `MINDROOM_EVENT_CACHE_DATABASE_URL` | Variable holding the PostgreSQL URL, read from the process environment or the config-adjacent `.env` (process environment wins); custom names must be `DATABASE_URL` or end in `_DATABASE_URL` |
| `database_url` | string or `null` | `null` | Inline PostgreSQL URL that takes precedence over the variable |

Changes to `event_journal` apply after restarting MindRoom.
Read [Journal binding](#journal) before pointing an existing install at a different database.



## Journal binding

Each install is bound to one event-journal database the first time it opens one, and records that binding in `<storage>/tracking/event_journal_binding.json`.
MindRoom refuses to start against any other journal, because another database would silently lose turn deduplication, delivery ownership, and recovery ownership.
A refused database is left untouched.

| Startup error contains | Cause | Fix |
| --- | --- | --- |
| `has never been used by this install` | The configured database is new or empty, usually because the connection URL points somewhere new. | Check `event_journal` and the variable named by `database_url_env` and point them back at the bound database; adopt only if you want to start the journal's history fresh. |
| `is a different journal from the one this install is bound to` | The configured database belongs to another install, usually because the connection URL points at it. | Point `event_journal` back at the bound database, or adopt deliberately. |
| `could not be read` or `does not name a generation` | The binding file is corrupt or truncated. | Delete `<storage>/tracking/event_journal_binding.json`, then run `mindroom journal adopt` against the database you want; the file holds nothing the database does not. |

Do not adopt just because startup refused: a connection URL that drifted to a fresh database is the common cause, and adopting would abandon the real journal's history instead of finding it.



## Adopting a journal

`mindroom journal adopt` binds the install to the event-journal database configured right now.
It is the deliberate override of the startup refusal and the only fix for a lost or corrupt binding file.
Adopting the database the install was already using keeps its history, but adopting a different database abandons the deduplication, delivery, and recovery history in the previously bound journal.
It asks for confirmation when the install is already bound, unless `--yes` is passed.

Stop MindRoom before adopting, because a running MindRoom keeps writing to the journal it started with and adopting under it would split the install's history across two databases.
Adoption refuses with `Another process still has this install's event journal open` while MindRoom is running; a crashed MindRoom does not block it.
`--force` adopts anyway; use it only when you are certain nothing is running, for example when several hosts share one storage root over a network filesystem, where the running check does not work across hosts.

If adoption fails, for example because the database is unreachable, the previous binding stays in place.

### Moving a journal safely

A full copy of a journal database keeps its identity, so the binding accepts it without adoption.
This also means a stale copy is accepted without complaint, even though every turn since it was taken is missing, so copy only from a stopped install.

1. Stop MindRoom, and any `mindroom threads export --watch` running against the same storage root.
2. Copy or dump-and-restore the database in full.
3. Configure the destination PostgreSQL backend and URL, or move the SQLite journal together with its storage root.
4. Start MindRoom.

Adopt instead of copying only when you accept beginning the journal's history fresh.
