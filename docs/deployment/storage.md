---
icon: lucide/database
---

# Data Storage & Journal

## Data Persistence

MindRoom stores data in the `mindroom_data` directory by default:

- `agents/*/sessions/` and `teams/*/sessions/` - Conversation history (SQLite), optionally rooted at `MINDROOM_SESSION_STORAGE_PATH`
- `agents/*/learning/` - Per-agent Agno Learning state when enabled (SQLite, persistent across restarts)
- `agents/*/chroma/` - Per-agent Mem0 ChromaDB storage
- `knowledge_db/` - Knowledge base vector stores
- `tracking/` - Durable response, callback-obligation, and lifecycle-hook state used to prevent duplicate work across restarts
- `credentials/` - Synchronized secrets from `.env`
- `logs/` - Application logs
- `matrix_state.yaml` - Matrix connection state
- `encryption_keys/` - Matrix E2EE keys (if enabled)

These agent paths describe ordinary shared agents; private agents use their resolved private state roots.
`MINDROOM_SESSION_STORAGE_PATH` relocates session storage only, leaving learning and memory at their agent state roots.

Keep `tracking/` on persistent storage and include it in backups.
Include the primary storage directory in backups, with any `learning/` and Mem0 `chroma/` directories under shared-agent or resolved private-instance state roots.
When `MINDROOM_SESSION_STORAGE_PATH` is set in a container, mount that path on persistent storage and include it in backups too.

Before opening an owned agent or team session database, MindRoom checks whether its session table contains the columns required by the installed Agno version.
If required session columns are missing, MindRoom renames the complete `sessions/` directory to a unique sibling `sessions.incompatible-<id>/`, preserving the database and SQLite sidecars, and starts a fresh session store.
These archives are retained for inspection or manual recovery; include them in backups and remove them only when no longer needed.
Compatible history stays in place, including older readable run blobs alongside current run rows, extra columns, and stores awaiting lazy table creation.
This check does not validate existing runs-table schemas or archive databases on permission, locking, I/O, or corruption errors.
Learning, workspaces, credentials, encryption keys, custom stores, and durable journal state are outside this session recovery boundary.

Dispatch-obligation databases retain one compact terminal row per settled callback except successful invites, whose synthetic obligations are deleted so later re-invites can run.
The retained terminal rows have no automatic retention window because deleting them weakens replay deduplication.
Pending rows temporarily retain the full event replay payload and should represent only actively deferred or retry-owned work, not completed ignore paths.
Checkpoint invalidation can force a no-`since` limited sync that backfills older events, and opaque Matrix tokens provide no safe ordering frontier for pruning those exact keys.
Size and monitor the volume for lifetime callback growth, and use the inspection and corruption-remediation guidance in [Bot Runtime Architecture](../architecture/bot-runtime.md#durable-dispatch-boundary).

## Event Journal

`event_journal.backend` defaults to `sqlite`, which stores the Matrix event journal at `<storage>/tracking/event_journal.db` with no separate path setting.
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

Changes to the event journal apply after restarting MindRoom.
See the [journal binding and migration commands](#journal) before moving, restoring, or adopting a journal.

## journal

Inspect and rebind the durable event journal.
See [Event Journal configuration](#event-journal) for backend selection, the SQLite location, and PostgreSQL URL resolution.

The event journal is the database that holds turn deduplication, delivery ownership, and recovery ownership.
Every install is bound to exactly one, and MindRoom refuses to start against any other one, because using a stranger's journal does not fail — it answers every question confidently and about somebody else's history.

An install is bound the first time it opens a journal.
The database mints a generation when it is first used and never rewrites it, so the generation names the database rather than the process, and the binding recorded in `<storage>/tracking/event_journal_binding.json` names that generation.
A later start reads the configured database's generation before it opens the store and refuses when the two do not match.
Refusal happens before anything is created, so a database that gets refused is left exactly as it was found.

Each refusal is a different problem and says so:

| Message | What happened | What to do |
| --- | --- | --- |
| `has never been used by this install` | The configured database carries no generation at all. | Usually a connection pointing somewhere new. Point `event_journal` back, or adopt deliberately. |
| `is a different journal from the one this install is bound to` | The configured database carries someone else's generation. | Usually a connection pointing at another install. Point `event_journal` back, or adopt deliberately. |
| `could not be read` | The binding file itself is corrupt or truncated. | Repair or delete `<storage>/tracking/event_journal_binding.json`, then adopt. |

## journal adopt

Bind this install to the event-journal database that is configured right now.

This is the deliberate override of the startup refusal, and the only repair for an install whose binding has been lost.
Adopting gives up the deduplication, delivery, and recovery history held in the previously bound journal, so it asks for confirmation unless `--yes` is passed.

Stop MindRoom before adopting.
A running MindRoom keeps writing to the database it opened at startup, so adopting under it does not move the running install — it splits the install's history across two databases, and nothing will ever read the older one again.
A process that has the journal open holds an advisory claim on `<storage>/tracking/event_journal_store.lock` for as long as it has it open, and adoption refuses while that claim is held.
The claim ends when the store is closed, and the operating system withdraws it if the process dies, so a crashed MindRoom leaves nothing to clean up.
`--force` adopts anyway, for the case where the claim cannot be trusted: it is advisory, and it does not travel between hosts sharing one storage root over a network filesystem.

Adoption keeps the old binding until the new one is ready.
If the candidate cannot be opened — an unreachable server, a bad DSN, a full disk — the command fails with the previous binding still in place, and the install starts exactly as it did before.

### Moving a journal safely

Copying a database the supported way carries its generation with it, so a copy is accepted by the same binding and needs no adoption.
That cuts both ways: a stale clone taken weeks ago carries the same generation as the live database and will be accepted without complaint, even though every turn since the clone was taken is missing from it.
The generation proves the database is the same lineage, not that it is up to date, and nothing else checks.

For a quiesced migration:

1. Stop MindRoom, and any `mindroom threads export --watch` running against the same storage root.
2. Copy or dump-and-restore the database in full.
3. Configure the destination PostgreSQL backend and URL, or move the SQLite journal with its storage root; SQLite always uses `<storage>/tracking/event_journal.db`.
4. Start MindRoom. No adoption is needed, because the generation travelled with the data.

Adopt instead of copying only when you accept beginning the journal's history fresh.

### Recovering from a failure

An install refuses to start and you did not move anything.
Check `event_journal` and the environment variable named by `event_journal.database_url_env` before adopting: a DSN that has drifted to a fresh database is the common cause, and adopting would throw the real journal's history away rather than find it.

An install refuses to start with `could not be read`.
The binding file is corrupt. Delete it and run `mindroom journal adopt` against the database you actually want; there is nothing recoverable inside it that the database does not already know.

Adoption refuses because the journal is in use.
Stop MindRoom and try again. Use `--force` only when you are certain nothing is running, for example after a host has been rebooted with a stale storage root on a network filesystem.
