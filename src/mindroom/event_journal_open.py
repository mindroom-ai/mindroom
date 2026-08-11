"""Which event-journal database this install opens, and whether it is allowed to.

Two callers open the same store: the bot runtime, which writes the projection
from sync, and one thread-export pass, which reads it -- and, to read a
conversation nobody has walked yet, writes it too. Hydration is how the
projection acquires a thread in the first place, so an export that installs
nothing has nothing to page over; the export is a second writer by
construction, not by accident. What that costs the bot is the SQLite backend's
to explain.

They have to agree on which file or DSN that is. A disagreement does not fail
-- it produces an empty store, and an export that reports every room as having
no history at all.

Worse than an empty database is a populated wrong one. Opening another
install's journal loses turn deduplication, delivery ownership, and recovery
ownership all at once, and loses them silently: nothing raises, and the damage
surfaces later as answers sent twice or never sent at all. Emptiness cannot be
the test, because a populated stranger sails straight through it. The database's
own generation can: it is minted once when the database is first opened and
never rewritten, so it names the database rather than the process, and copying
the database the supported way carries it along. This install records the
generation it is bound to, and a database whose generation is missing or
different is refused instead of opened.

Nothing here refuses a *config* change, because a config change cannot move the
opened store. ``event_journal`` is read exactly once per process, at the moment
the store is opened: the orchestrator caches the store and hands the same one to
every bot, and the update planner has no journal case at all. An edit to the
field is therefore inert until a restart. The honest response is to save it and
say so, which is what :func:`pending_event_journal_restart` is for -- and then
to refuse at the restart if the newly named database is not this install's,
which is what :func:`bind_event_journal` is for. Refusing at the restart is also
the only guard that covers an edit made with a text editor, which no write-path
rule can see.
"""

from __future__ import annotations

import json
import threading
import uuid
from dataclasses import dataclass
from typing import TYPE_CHECKING

from mindroom.durable_write import write_json_file_durable
from mindroom.event_journal import EventJournalStore
from mindroom.event_journal.probe import probe_postgres_generation, probe_sqlite_generation
from mindroom.file_locks import (
    acquire_shared_file_lock,
    async_exclusive_file_lock,
    file_lock_is_held,
    release_file_lock,
)
from mindroom.logging_config import get_logger

if TYPE_CHECKING:
    from pathlib import Path
    from typing import TextIO

    from mindroom.config.main import Config
    from mindroom.config.matrix import EventJournalConfig
    from mindroom.constants import RuntimePaths

logger = get_logger(__name__)

BINDING_FILENAME = "event_journal_binding.json"
BINDING_LOCK_FILENAME = "event_journal_binding.lock"
IN_USE_LOCK_FILENAME = "event_journal_store.lock"

# Keyed by config path rather than by the whole runtime context: the runtime
# context carries the resolution environment, which is not part of which
# database got opened, and two authorities resolving the same config file
# independently must land on the same key.
_OPENED_DATABASES: dict[Path, tuple[str, str | None]] = {}
_OPENED_DATABASES_LOCK = threading.Lock()

# The only connection parameters that go into a description. An allowlist that
# is missing a field costs an operator some detail; a denylist that is missing
# one publishes a password.
_DESCRIBED_CONNECTION_FIELDS = ("host", "port", "dbname")


def event_journal_in_use_lock_path(storage_path: Path) -> Path:
    """Return the lock a process holds while it has this install's journal open."""
    return storage_path / "tracking" / IN_USE_LOCK_FILENAME


def _opened_database(
    journal_config: EventJournalConfig,
    runtime_paths: RuntimePaths,
) -> tuple[str, str | None]:
    """Return which database :func:`open_event_journal` would open for this config.

    Only what that function reads can make two configs name different stores.
    It branches on the backend and, for PostgreSQL, on the resolved DSN; the
    SQLite path comes from the runtime storage root and reads no
    ``event_journal`` field at all. So editing ``database_url`` while the
    backend stays ``sqlite`` opens the very same file and is not a move.

    An unresolvable PostgreSQL DSN gets its own identity: no database can be
    opened from it, so it is not the one currently open, and it is the same
    non-database as any other config that cannot resolve one either.
    """
    if journal_config.backend != "postgres":
        return ("sqlite", None)
    try:
        return ("postgres", journal_config.resolve_postgres_database_url(runtime_paths))
    except ValueError:
        return ("postgres", None)


def _describe_postgres_connection(database_url: str) -> str:
    """Return the safe-to-show part of one PostgreSQL connection string.

    Built from an allowlist of parsed fields rather than by removing the parts
    that look secret. A PostgreSQL password has at least four spellings -- URI
    userinfo, a ``password`` query parameter, ``sslpassword``, and a
    ``password=`` keyword in the non-URI grammar -- and those are two different
    grammars, not one with variations. Subtracting the spellings somebody
    thought of is how the fourth one gets written to disk.
    """
    try:
        import psycopg  # noqa: PLC0415 - psycopg ships with the optional postgres extra
        from psycopg.conninfo import conninfo_to_dict  # noqa: PLC0415 - and so does its parser
    except ImportError:
        return "postgres (install the postgres extra to read the connection URL)"
    try:
        parsed = conninfo_to_dict(database_url)
    except psycopg.Error:
        return "postgres (a connection URL that could not be parsed)"
    shown = " ".join(
        f"{field}={parsed[field]}" for field in _DESCRIBED_CONNECTION_FIELDS if isinstance(parsed.get(field), str)
    )
    return f"postgres {shown}" if shown else "postgres (a connection URL naming no host or database)"


def describe_event_journal(
    journal_config: EventJournalConfig,
    runtime_paths: RuntimePaths,
) -> str:
    """Return a non-secret description of the database this config opens.

    Written to the binding file and quoted in refusals, so it must never carry
    a password. Host, port, and database name survive, because a refusal an
    operator cannot act on is not worth printing.
    """
    backend, database_url = _opened_database(journal_config, runtime_paths)
    if backend != "postgres":
        return "sqlite tracking/event_journal.db"
    if database_url is None:
        return "postgres (no connection URL could be resolved)"
    return _describe_postgres_connection(database_url)


def record_opened_event_journal(
    journal_config: EventJournalConfig,
    *,
    runtime_paths: RuntimePaths,
) -> None:
    """Record the database this process just opened for ``runtime_paths``."""
    with _OPENED_DATABASES_LOCK:
        _OPENED_DATABASES[runtime_paths.config_path] = _opened_database(journal_config, runtime_paths)


def pending_event_journal_restart(config: Config, runtime_paths: RuntimePaths) -> bool:
    """Return whether ``config`` names a different database than the one already open.

    Compares opened databases rather than authored fields: under
    ``backend: sqlite`` the path comes from the storage root and nothing in
    ``event_journal`` is read to build it, so an edit there changes no database
    and must not raise a restart notice.

    ``False`` before anything is open. There is no in-force database to differ
    from, and the config being asked about is the one that will be used.
    """
    with _OPENED_DATABASES_LOCK:
        opened = _OPENED_DATABASES.get(runtime_paths.config_path)
    if opened is None:
        return False
    return opened != _opened_database(config.event_journal, runtime_paths)


class EventJournalBindingError(RuntimeError):
    """Startup refused because the configured database is not this install's journal."""


@dataclass(frozen=True)
class EventJournalBinding:
    """The event-journal database one install is bound to."""

    generation: str
    database: str


def event_journal_binding_path(storage_path: Path) -> Path:
    """Return where one install records the journal it is bound to."""
    return storage_path / "tracking" / BINDING_FILENAME


def event_journal_binding_lock_path(storage_path: Path) -> Path:
    """Return the lock that makes deciding and publishing a binding one step.

    Reading the binding, minting a generation, and publishing it are three
    operations against two different stores, and a first bind awaits a database
    write in the middle. Two processes interleaving there both come back
    successful, each certain of a different generation, and only the binding
    written last survives -- so the loser spends its life writing into a
    database this install is not bound to.
    """
    return storage_path / "tracking" / BINDING_LOCK_FILENAME


def read_event_journal_binding(storage_path: Path) -> EventJournalBinding | None:
    """Return the recorded binding, or ``None`` when this install has never bound one.

    An unreadable or malformed binding is refused rather than treated as
    absent: silently re-binding would adopt whatever database happened to be
    configured, which is the outcome the binding exists to prevent. Bytes that
    are not UTF-8 are one of those cases and not a crash -- a truncated write
    can leave a partial multi-byte sequence, which is exactly the corruption
    this is here to catch.
    """
    path = event_journal_binding_path(storage_path)
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return None
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        msg = (
            f"The event-journal binding at {path} could not be read ({exc}). "
            "Repair or remove it, then run `mindroom journal adopt` to bind the configured database."
        )
        raise EventJournalBindingError(msg) from exc
    generation = raw.get("generation") if isinstance(raw, dict) else None
    database = raw.get("database") if isinstance(raw, dict) else None
    if not isinstance(generation, str) or not generation or not isinstance(database, str):
        msg = (
            f"The event-journal binding at {path} does not name a generation. "
            "Remove it, then run `mindroom journal adopt` to bind the configured database."
        )
        raise EventJournalBindingError(msg)
    return EventJournalBinding(generation=generation, database=database)


def current_binding_description(storage_path: Path) -> str | None:
    """Return what this install is bound to now, or ``None`` when nothing is recorded.

    An unreadable binding is not nothing. It is a binding whose content has
    been lost, which is a repair job -- but reporting it as absent would drop
    every "are you sure" that depends on there having been one, and the wording
    that warns about giving up history is exactly the one that depends on it.
    A corrupted file would then buy a gentler prompt than an intact one.
    """
    try:
        binding = read_event_journal_binding(storage_path)
    except EventJournalBindingError:
        return f"a binding this install can no longer read ({event_journal_binding_path(storage_path)})"
    return None if binding is None else binding.database


def write_event_journal_binding(storage_path: Path, binding: EventJournalBinding) -> None:
    """Publish the journal this install is bound to, replacing any previous binding.

    Durable rather than merely renamed. A binding that comes back from a crash
    truncated is refused at startup, which turns a power cut during a bind into
    an install that will not start, so the content reaches the disk before the
    rename that publishes it and the rename reaches the disk before this
    returns. The temporary file gets a unique name for the same reason a lock
    exists: a fixed one is shared state, and two processes publishing at once
    would each rename whatever the other had half-written into it.

    Assumes the storage root behaves like a POSIX filesystem: ``rename`` over
    an existing name is atomic, and ``fsync`` reaches stable storage. That
    holds for local disks and for an NFSv4 mount. It does not assume more than
    one machine writes this file -- the advisory lock that serializes
    read-decide-publish is per host, so two hosts sharing one storage root over
    a network filesystem are outside what this guarantees.
    """
    write_json_file_durable(
        event_journal_binding_path(storage_path),
        {"generation": binding.generation, "database": binding.database},
        indent=2,
        trailing_newline=True,
    )


def event_journal_sqlite_path(storage_path: Path) -> Path:
    """Return the SQLite journal one storage root uses."""
    return storage_path / "tracking" / "event_journal.db"


def _probe_generation(
    journal_config: EventJournalConfig,
    *,
    runtime_paths: RuntimePaths,
    storage_path: Path,
) -> str | None:
    """Return the configured database's generation without creating anything in it."""
    if journal_config.backend == "postgres":
        return probe_postgres_generation(journal_config.resolve_postgres_database_url(runtime_paths))
    return probe_sqlite_generation(event_journal_sqlite_path(storage_path))


def _refuse_foreign_generation(
    generation: str | None,
    *,
    binding: EventJournalBinding,
    description: str,
) -> str:
    """Return the generation, or raise the refusal it earns.

    The two refusals stay apart because they are different operator problems.
    A database that has never been used is usually a connection pointing
    somewhere new, while a database carrying someone else's generation is
    usually a connection pointing at another install.
    """
    if generation is None:
        msg = (
            f"The configured event journal ({description}) has never been used by this install, "
            f"which is bound to {binding.database}. Point event_journal back at the bound database, "
            "or run `mindroom journal adopt` to bind this one and begin its history fresh."
        )
        raise EventJournalBindingError(msg)
    if generation != binding.generation:
        msg = (
            f"The configured event journal ({description}) is a different journal from the one this "
            f"install is bound to ({binding.database}). Using it would lose turn deduplication, "
            "delivery ownership, and recovery ownership. Point event_journal back at the bound "
            "database, or run `mindroom journal adopt` to bind this one."
        )
        raise EventJournalBindingError(msg)
    return generation


def _open_store(
    journal_config: EventJournalConfig,
    *,
    runtime_paths: RuntimePaths,
    storage_path: Path,
) -> EventJournalStore:
    """Open the store unconditionally, creating or migrating its schema."""
    store = (
        EventJournalStore.open_postgres(journal_config.resolve_postgres_database_url(runtime_paths))
        if journal_config.backend == "postgres"
        else EventJournalStore.open_sqlite(event_journal_sqlite_path(storage_path))
    )
    record_opened_event_journal(journal_config, runtime_paths=runtime_paths)
    return store


@dataclass(frozen=True)
class OpenEventJournal:
    """One open journal, and the claim that says this process is writing to it.

    The claim is true for exactly as long as the store is open, so the two are
    one object rather than two calls a caller has to pair correctly. Closing
    used to take the storage root back as an argument, which let a caller
    release a claim for a root it never opened; the lease it actually took is
    the only thing that can be given back here.
    """

    store: EventJournalStore
    _in_use_lock: TextIO

    async def close(self) -> None:
        """Close the store and withdraw this process's claim on the journal.

        The claim is what stops `mindroom journal adopt` from rebinding out
        from under a running MindRoom, so leaving it behind would make the
        repair command refuse forever. The operating system withdraws it if
        this process dies, so a crash needs no cleanup.
        """
        try:
            await self.store.close()
        finally:
            release_file_lock(self._in_use_lock)


def open_event_journal(
    journal_config: EventJournalConfig,
    *,
    runtime_paths: RuntimePaths,
    storage_path: Path,
) -> OpenEventJournal:
    """Open the durable store this runtime's journal, projection, and outbox share.

    One database can hold every principal in the deployment; each caller
    receives only its own principal-bound view from it.

    An install that is already bound has its candidate checked here, before the
    store exists, because opening one creates or migrates the entire schema.
    Checking afterwards would mean every refused database still ends up
    carrying this install's tables -- pointing a probe at the wrong server and
    being told no would leave twelve tables behind in it. A refusal now leaves
    the candidate exactly as it was found.

    Opening is still not the same as being allowed to use it: an unbound
    install has nothing to check against yet, and gets its binding minted by
    :func:`bind_event_journal`, which every caller must await before reading or
    writing. Minting is a database write, so it cannot happen here -- this is
    the synchronous half, and that is why the two are separate.

    Opening is also what fixes the journal for the rest of the process, so this
    records the identity :func:`pending_event_journal_restart` compares against,
    and takes a shared lease on the storage root so that `mindroom journal
    adopt` running elsewhere can see there is a live writer. Both happen after
    the open succeeds: a store that failed to open is not one anybody is
    writing to.

    Each open takes its own lease rather than sharing a counted one, because a
    bot built outside the orchestrator opens a second store beside the shared
    one. Shared advisory locks on separate descriptors coexist, and the root
    stays claimed until the last of them is released.
    """
    binding = read_event_journal_binding(storage_path)
    if binding is not None:
        _refuse_foreign_generation(
            _probe_generation(journal_config, runtime_paths=runtime_paths, storage_path=storage_path),
            binding=binding,
            description=describe_event_journal(journal_config, runtime_paths),
        )
    store = _open_store(journal_config, runtime_paths=runtime_paths, storage_path=storage_path)
    return OpenEventJournal(
        store=store,
        _in_use_lock=acquire_shared_file_lock(event_journal_in_use_lock_path(storage_path)),
    )


async def bind_event_journal(
    store: EventJournalStore,
    *,
    journal_config: EventJournalConfig,
    runtime_paths: RuntimePaths,
    storage_path: Path,
) -> str:
    """Return the journal's generation, refusing a database this install is not bound to.

    Idempotent, and meant to be called by every opener: the first call on an
    unbound install mints the generation and records it, and every later call
    compares. Bots that borrow a store somebody else opened reach their first
    async moment here, so this is where they find out.
    """
    description = describe_event_journal(journal_config, runtime_paths)
    async with async_exclusive_file_lock(event_journal_binding_lock_path(storage_path)):
        binding = read_event_journal_binding(storage_path)
        if binding is None:
            generation = await store.generation(new_generation=uuid.uuid4().hex)
            write_event_journal_binding(
                storage_path,
                EventJournalBinding(generation=generation, database=description),
            )
            logger.info("event_journal_bound", database=description)
            return generation
        return _refuse_foreign_generation(
            await store.existing_generation(),
            binding=binding,
            description=description,
        )


async def adopt_event_journal(
    journal_config: EventJournalConfig,
    *,
    runtime_paths: RuntimePaths,
    storage_path: Path,
    force: bool = False,
) -> str:
    """Bind this install to the configured database, whatever it was bound to before.

    The deliberate override of the startup refusal, and the only repair for an
    install whose binding has been lost. So it has to leave the install in a
    usable state no matter how it fails: the previous binding is replaced by
    its successor rather than cleared first and rebuilt. Clearing first turned
    every failure in between -- an unreachable server, a bad DSN, a full disk
    -- into an unbound install, which is worse than the state the operator ran
    this command to leave.

    Refuses while another process has the journal open, because that process
    keeps writing to the store it started on: adopting under it does not move
    the running install, it splits the install's history in two. ``force``
    exists because the claim is advisory and an operator can know better --
    a container that was killed uncleanly leaves no claim, but a shared storage
    root on a network filesystem may not carry one across hosts.
    """
    description = describe_event_journal(journal_config, runtime_paths)
    async with async_exclusive_file_lock(event_journal_binding_lock_path(storage_path)):
        if not force and file_lock_is_held(event_journal_in_use_lock_path(storage_path)):
            msg = (
                "Another process still has this install's event journal open, and it will go on writing "
                "to the database it started with. Adopting now would split this install's history in two. "
                "Stop MindRoom and run this again, or pass --force if you are certain nothing is running."
            )
            raise EventJournalBindingError(msg)
        store = _open_store(journal_config, runtime_paths=runtime_paths, storage_path=storage_path)
        try:
            generation = await store.generation(new_generation=uuid.uuid4().hex)
        finally:
            await store.close()
        write_event_journal_binding(
            storage_path,
            EventJournalBinding(generation=generation, database=description),
        )
    logger.info("event_journal_adopted", database=description)
    return generation


__all__ = [
    "BINDING_FILENAME",
    "BINDING_LOCK_FILENAME",
    "IN_USE_LOCK_FILENAME",
    "EventJournalBinding",
    "EventJournalBindingError",
    "OpenEventJournal",
    "adopt_event_journal",
    "bind_event_journal",
    "current_binding_description",
    "describe_event_journal",
    "event_journal_binding_lock_path",
    "event_journal_binding_path",
    "event_journal_in_use_lock_path",
    "event_journal_sqlite_path",
    "open_event_journal",
    "pending_event_journal_restart",
    "read_event_journal_binding",
    "record_opened_event_journal",
    "write_event_journal_binding",
]
