"""Which event-journal database one install is allowed to open.

Pointing a running install at a different journal is silent and unrecoverable:
turn deduplication, delivery ownership, and recovery ownership all live in the
database, and a stranger's database answers every question confidently and
wrongly. These tests pin the refusal that stops it, the one command that
deliberately overrides the refusal, and the surfaces that tell an operator a
saved ``event_journal`` edit has not taken effect yet.
"""

from __future__ import annotations

import asyncio
import sqlite3
from typing import TYPE_CHECKING
from urllib.parse import parse_qs, urlparse

import pytest
import yaml
from typer.testing import CliRunner

from mindroom.cli.main import app
from mindroom.config.main import load_config
from mindroom.constants import resolve_primary_runtime_paths
from mindroom.event_journal import EventJournalStore
from mindroom.event_journal_open import (
    EventJournalBinding,
    EventJournalBindingError,
    bind_event_journal,
    clear_event_journal_binding,
    describe_event_journal,
    event_journal_binding_path,
    event_journal_sqlite_path,
    open_event_journal_store,
    pending_event_journal_restart,
    read_event_journal_binding,
    record_opened_event_journal,
    write_event_journal_binding,
)
from tests.conftest import postgres_journal_schema_url

if TYPE_CHECKING:
    from pathlib import Path

    from mindroom.constants import RuntimePaths

runner = CliRunner()

BASE_CONFIG: dict[str, object] = {
    "models": {"default": {"provider": "ollama", "id": "test-model"}},
    "agents": {"probe": {"display_name": "Probe", "role": "A probe agent"}},
}


# Obviously fake, and distinctive enough that a leak into a file or a message
# is found by searching for it.
URI_PASSWORD = "hunter2-not-a-real-password"
QUERY_PASSWORD = "certsecret-not-a-real-password"


def _runtime_paths(tmp_path: Path) -> RuntimePaths:
    config_path = tmp_path / "config.yaml"
    config_path.write_text(yaml.dump(BASE_CONFIG), encoding="utf-8")
    return resolve_primary_runtime_paths(
        config_path=config_path,
        storage_path=tmp_path / "storage",
        process_env={},
    )


def _postgres_runtime_paths(tmp_path: Path, database_url: str) -> RuntimePaths:
    config_path = tmp_path / "config.yaml"
    authored = dict(BASE_CONFIG)
    authored["event_journal"] = {"backend": "postgres", "database_url": database_url}
    config_path.write_text(yaml.dump(authored), encoding="utf-8")
    return resolve_primary_runtime_paths(
        config_path=config_path,
        storage_path=tmp_path / "storage",
        process_env={},
    )


async def _open_and_bind(runtime_paths: RuntimePaths) -> str:
    """Open the configured journal and bind this install to it, as startup does."""
    config = load_config(runtime_paths)
    store = open_event_journal_store(
        config.event_journal,
        runtime_paths=runtime_paths,
        storage_path=runtime_paths.storage_root,
    )
    try:
        return await bind_event_journal(
            store,
            journal_config=config.event_journal,
            runtime_paths=runtime_paths,
            storage_path=runtime_paths.storage_root,
        )
    finally:
        await store.close()


class TestBindingRefusal:
    """A journal that is not this install's is refused rather than opened."""

    pytestmark = pytest.mark.asyncio

    async def test_the_first_bind_records_the_generation_the_database_was_born_with(
        self,
        tmp_path: Path,
    ) -> None:
        """An unbound install adopts what it is configured with, and remembers it."""
        runtime_paths = _runtime_paths(tmp_path)
        assert read_event_journal_binding(runtime_paths.storage_root) is None

        generation = await _open_and_bind(runtime_paths)

        binding = read_event_journal_binding(runtime_paths.storage_root)
        assert binding is not None
        assert binding.generation == generation
        assert binding.database == "sqlite tracking/event_journal.db"
        # Binding again is what every later start does, and must be a no-op.
        assert await _open_and_bind(runtime_paths) == generation

    async def test_a_populated_but_different_journal_is_refused(self, tmp_path: Path) -> None:
        """Emptiness cannot be the test: a stranger's journal is full and still wrong.

        A database holding another install's turns answers every dedupe,
        delivery, and recovery question confidently, and every answer is about
        somebody else's history. Nothing raises on its own, so the only place
        this can be caught is before the first read.
        """
        runtime_paths = _runtime_paths(tmp_path)
        await _open_and_bind(runtime_paths)

        # Replace the file with a different, already-used database.
        journal_file = runtime_paths.storage_root / "tracking" / "event_journal.db"
        journal_file.unlink()
        stranger = EventJournalStore.open_sqlite(journal_file)
        try:
            stranger_generation = await stranger.generation(new_generation="another-install")
        finally:
            await stranger.close()
        assert stranger_generation == "another-install"

        with pytest.raises(EventJournalBindingError) as exc_info:
            await _open_and_bind(runtime_paths)

        assert "different journal" in str(exc_info.value)

    async def test_a_journal_that_has_never_been_used_is_refused(self, tmp_path: Path) -> None:
        """A fresh database is its own operator problem, and gets its own message."""
        runtime_paths = _runtime_paths(tmp_path)
        await _open_and_bind(runtime_paths)

        (runtime_paths.storage_root / "tracking" / "event_journal.db").unlink()

        with pytest.raises(EventJournalBindingError) as exc_info:
            await _open_and_bind(runtime_paths)

        message = str(exc_info.value)
        assert "never been used by this install" in message
        assert "different journal" not in message, "the two refusals must be told apart"

    async def test_copying_the_database_keeps_the_binding_valid(self, tmp_path: Path) -> None:
        """Moving a journal the supported way carries the generation, so it is not a stranger."""
        runtime_paths = _runtime_paths(tmp_path)
        generation = await _open_and_bind(runtime_paths)

        journal_file = runtime_paths.storage_root / "tracking" / "event_journal.db"
        moved = tmp_path / "moved.db"
        moved.write_bytes(journal_file.read_bytes())
        journal_file.unlink()
        journal_file.write_bytes(moved.read_bytes())

        assert await _open_and_bind(runtime_paths) == generation

    async def test_an_unreadable_binding_is_refused_rather_than_ignored(self, tmp_path: Path) -> None:
        """Treating a corrupt binding as absent would adopt whatever is configured."""
        runtime_paths = _runtime_paths(tmp_path)
        await _open_and_bind(runtime_paths)
        event_journal_binding_path(runtime_paths.storage_root).write_text("{not json", encoding="utf-8")

        with pytest.raises(EventJournalBindingError):
            await _open_and_bind(runtime_paths)

    async def test_a_binding_without_a_generation_is_refused(self, tmp_path: Path) -> None:
        """A binding that names no generation cannot certify anything."""
        runtime_paths = _runtime_paths(tmp_path)
        await _open_and_bind(runtime_paths)
        event_journal_binding_path(runtime_paths.storage_root).write_text(
            '{"database": "sqlite tracking/event_journal.db"}',
            encoding="utf-8",
        )

        with pytest.raises(EventJournalBindingError):
            await _open_and_bind(runtime_paths)


def _identity_only_sqlite(database_path: Path, generation: str) -> None:
    """Write a database that carries a journal identity and nothing else.

    This is what a freshly minted journal on a server that is not this
    install's looks like, and it is the shape that made the damage visible:
    one table before the probe, twelve after it.
    """
    connection = sqlite3.connect(database_path)
    try:
        connection.execute(
            "CREATE TABLE journal_identity ("
            "singleton BOOLEAN NOT NULL PRIMARY KEY, generation TEXT NOT NULL, created_at_ns BIGINT NOT NULL)",
        )
        connection.execute("INSERT INTO journal_identity VALUES (1, ?, 0)", (generation,))
        connection.commit()
    finally:
        connection.close()


def _sqlite_tables(database_path: Path) -> set[str]:
    connection = sqlite3.connect(f"{database_path.as_uri()}?mode=ro", uri=True)
    try:
        return {str(row[0]) for row in connection.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}
    finally:
        connection.close()


def _pinned_schema(database_url: str) -> str:
    """Return the schema a test DSN is pinned to."""
    options = parse_qs(urlparse(database_url).query)["options"][0]
    return options.removeprefix("-csearch_path=")


def _postgres_table_count(database_url: str, schema: str) -> int:
    import psycopg  # noqa: PLC0415 - psycopg ships in the optional postgres extra

    with psycopg.connect(database_url, autocommit=True) as connection:
        row = connection.execute(
            "SELECT count(*) FROM information_schema.tables WHERE table_schema = %s",
            (schema,),
        ).fetchone()
    assert row is not None
    return int(row[0])


class TestRefusalLeavesTheCandidateAlone:
    """Being told no must not be the thing that writes this install into a database."""

    pytestmark = pytest.mark.asyncio

    async def test_a_refused_sqlite_journal_keeps_the_tables_it_arrived_with(self, tmp_path: Path) -> None:
        """Both backends create the whole schema on open, so the check has to come first.

        A candidate that gets refused for carrying somebody else's generation
        used to be opened anyway, one statement before the refusal, and an
        identity-only database came out of the encounter carrying every table
        this install uses.
        """
        runtime_paths = _runtime_paths(tmp_path)
        await _open_and_bind(runtime_paths)

        journal_file = event_journal_sqlite_path(runtime_paths.storage_root)
        journal_file.unlink()
        _identity_only_sqlite(journal_file, "another-install")
        before = journal_file.read_bytes()

        with pytest.raises(EventJournalBindingError) as exc_info:
            await _open_and_bind(runtime_paths)

        assert "different journal" in str(exc_info.value)
        assert _sqlite_tables(journal_file) == {"journal_identity"}
        assert journal_file.read_bytes() == before, "a refused database must come out byte-identical"

    async def test_a_refused_empty_sqlite_file_is_not_turned_into_a_journal(self, tmp_path: Path) -> None:
        """The other refusal has the same duty: an unused database stays unused."""
        runtime_paths = _runtime_paths(tmp_path)
        await _open_and_bind(runtime_paths)

        journal_file = event_journal_sqlite_path(runtime_paths.storage_root)
        journal_file.unlink()
        journal_file.touch()

        with pytest.raises(EventJournalBindingError) as exc_info:
            await _open_and_bind(runtime_paths)

        assert "never been used" in str(exc_info.value)
        assert journal_file.read_bytes() == b""

    async def test_a_refused_postgres_journal_gets_no_schema(
        self,
        tmp_path: Path,
        postgres_journal_url: str,
    ) -> None:
        """The same rule on the backend where the wrong database is somebody else's server."""
        runtime_paths = _postgres_runtime_paths(tmp_path, postgres_journal_schema_url(postgres_journal_url))
        await _open_and_bind(runtime_paths)

        stranger = postgres_journal_schema_url(postgres_journal_url)
        schema = _pinned_schema(stranger)
        assert _postgres_table_count(postgres_journal_url, schema) == 0
        runtime_paths.config_path.write_text(
            yaml.dump({**BASE_CONFIG, "event_journal": {"backend": "postgres", "database_url": stranger}}),
            encoding="utf-8",
        )

        with pytest.raises(EventJournalBindingError) as exc_info:
            await _open_and_bind(runtime_paths)

        assert "never been used" in str(exc_info.value)
        assert _postgres_table_count(postgres_journal_url, schema) == 0


class TestAdoptCommand:
    """The one deliberate override, without which the refusal is a trap."""

    pytestmark = pytest.mark.asyncio

    async def test_adopt_binds_the_configured_journal_and_startup_then_succeeds(
        self,
        tmp_path: Path,
    ) -> None:
        """An operator who meant it says so once, and the next start goes through."""
        runtime_paths = _runtime_paths(tmp_path)
        await _open_and_bind(runtime_paths)
        (runtime_paths.storage_root / "tracking" / "event_journal.db").unlink()
        with pytest.raises(EventJournalBindingError):
            await _open_and_bind(runtime_paths)

        # The command owns its own event loop, so it runs off this one.
        result = await asyncio.to_thread(
            runner.invoke,
            app,
            [
                "journal",
                "adopt",
                "--config",
                str(runtime_paths.config_path),
                "--storage-path",
                str(runtime_paths.storage_root),
                "--yes",
            ],
        )

        assert result.exit_code == 0, result.output
        assert "Bound" in result.output
        # The refusal is gone, and the newly adopted database is now the bound one.
        adopted = await _open_and_bind(runtime_paths)
        binding = read_event_journal_binding(runtime_paths.storage_root)
        assert binding is not None
        assert binding.generation == adopted

    async def test_clearing_the_binding_lets_the_next_start_adopt(self, tmp_path: Path) -> None:
        """The reset primitive under the command, on its own."""
        runtime_paths = _runtime_paths(tmp_path)
        await _open_and_bind(runtime_paths)

        assert clear_event_journal_binding(runtime_paths.storage_root) is True
        assert clear_event_journal_binding(runtime_paths.storage_root) is False
        assert read_event_journal_binding(runtime_paths.storage_root) is None

        (runtime_paths.storage_root / "tracking" / "event_journal.db").unlink()
        await _open_and_bind(runtime_paths)


class TestNonSecretDescription:
    """What gets written to disk and printed in refusals must not carry a password."""

    @pytest.mark.parametrize(
        ("database_url", "expected"),
        [
            (
                f"postgresql://journal_user:{URI_PASSWORD}@db.example:5432/journal",
                "postgres host=db.example port=5432 dbname=journal",
            ),
            (
                f"postgresql://journal_user@db.example:5432/journal?password={URI_PASSWORD}",
                "postgres host=db.example port=5432 dbname=journal",
            ),
            (
                f"postgresql://db.example/journal?sslpassword={QUERY_PASSWORD}",
                "postgres host=db.example dbname=journal",
            ),
            (
                f"host=db.example dbname=journal user=journal_user password={URI_PASSWORD}",
                "postgres host=db.example dbname=journal",
            ),
            ("postgresql://db.example/journal", "postgres host=db.example dbname=journal"),
        ],
        ids=["userinfo", "password-query", "sslpassword-query", "keyword-string", "no-credentials"],
    )
    def test_no_spelling_of_a_password_survives_the_description(
        self,
        tmp_path: Path,
        database_url: str,
        expected: str,
    ) -> None:
        """A password reaches a PostgreSQL DSN four ways, across two different grammars.

        URI userinfo, a ``password`` query parameter, ``sslpassword``, and a
        ``password=`` keyword are not variations on one spelling -- the last is
        a different grammar entirely. Any rule that removes the spellings
        somebody happened to think of publishes the one they did not, so the
        description is built by naming the fields that may be shown. Compared
        for equality rather than for absence: that is the only assertion that
        also catches a field nobody has thought to forbid yet.
        """
        runtime_paths = _postgres_runtime_paths(tmp_path, database_url)

        description = describe_event_journal(load_config(runtime_paths).event_journal, runtime_paths)

        assert description == expected

    def test_a_written_binding_round_trips(self, tmp_path: Path) -> None:
        """The binding file is read back by the next process, so it has to survive the trip."""
        storage_path = tmp_path / "storage"
        binding = EventJournalBinding(generation="abc123", database="postgres host=db.example dbname=journal")

        write_event_journal_binding(storage_path, binding)

        assert read_event_journal_binding(storage_path) == binding
        assert "db.example" in event_journal_binding_path(storage_path).read_text(encoding="utf-8")


class TestNoSecretReachesDiskOrAMessage:
    """The two surfaces a real PostgreSQL password would actually escape through."""

    pytestmark = pytest.mark.asyncio

    @staticmethod
    def _leaky_dsn(database_url: str) -> str:
        """Return a connectable DSN carrying a secret that must never be echoed.

        ``sslpassword`` is accepted by libpq, ignored when no client key needs
        decrypting, and is one of the spellings the old redaction missed -- so
        it is both harmless to the connection and the exact thing being tested.
        """
        separator = "&" if "?" in database_url else "?"
        return f"{database_url}{separator}sslpassword={QUERY_PASSWORD}"

    async def test_the_binding_file_holds_only_the_fields_that_may_be_shown(
        self,
        tmp_path: Path,
        postgres_journal_url: str,
    ) -> None:
        """The binding is persisted, so a password in it outlives the process that wrote it."""
        dsn = self._leaky_dsn(postgres_journal_schema_url(postgres_journal_url))
        runtime_paths = _postgres_runtime_paths(tmp_path, dsn)

        await _open_and_bind(runtime_paths)

        written = event_journal_binding_path(runtime_paths.storage_root).read_bytes()
        assert QUERY_PASSWORD.encode() not in written
        assert b"sslpassword" not in written
        binding = read_event_journal_binding(runtime_paths.storage_root)
        assert binding is not None
        assert binding.database.startswith("postgres host=")
        assert "dbname=" in binding.database

    async def test_a_refusal_names_the_database_without_quoting_its_password(
        self,
        tmp_path: Path,
        postgres_journal_url: str,
    ) -> None:
        """The refusal is the other place the description is rendered, and it reaches logs."""
        bound = self._leaky_dsn(postgres_journal_schema_url(postgres_journal_url))
        runtime_paths = _postgres_runtime_paths(tmp_path, bound)
        await _open_and_bind(runtime_paths)

        stranger = self._leaky_dsn(postgres_journal_schema_url(postgres_journal_url))
        runtime_paths.config_path.write_text(
            yaml.dump({**BASE_CONFIG, "event_journal": {"backend": "postgres", "database_url": stranger}}),
            encoding="utf-8",
        )

        with pytest.raises(EventJournalBindingError) as exc_info:
            await _open_and_bind(runtime_paths)

        message = str(exc_info.value)
        assert QUERY_PASSWORD not in message
        assert "host=" in message, "a refusal the operator cannot act on is not worth printing"


class TestPendingRestart:
    """An edit to ``event_journal`` is saved and then does nothing until a restart."""

    def test_nothing_is_pending_before_a_journal_is_open(self, tmp_path: Path) -> None:
        """With no in-force database there is nothing for the config to differ from."""
        runtime_paths = _runtime_paths(tmp_path)
        assert pending_event_journal_restart(load_config(runtime_paths), runtime_paths) is False

    def test_a_backend_change_is_pending_once_a_journal_is_open(self, tmp_path: Path) -> None:
        """The store was opened at startup and every bot shares it, so this waits for a restart."""
        runtime_paths = _runtime_paths(tmp_path)
        record_opened_event_journal(load_config(runtime_paths).event_journal, runtime_paths=runtime_paths)

        moved = dict(BASE_CONFIG)
        moved["event_journal"] = {"backend": "postgres", "database_url": "postgresql://journal.invalid/moved"}
        runtime_paths.config_path.write_text(yaml.dump(moved), encoding="utf-8")

        assert pending_event_journal_restart(load_config(runtime_paths), runtime_paths) is True

    def test_a_sqlite_field_edit_opens_the_same_file_and_is_not_pending(self, tmp_path: Path) -> None:
        """Under sqlite the path comes from the storage root and no field is read to build it.

        Reporting a restart for an edit that changes no database would train the
        operator to ignore the notice.
        """
        runtime_paths = _runtime_paths(tmp_path)
        record_opened_event_journal(load_config(runtime_paths).event_journal, runtime_paths=runtime_paths)

        same_store = dict(BASE_CONFIG)
        same_store["event_journal"] = {
            "backend": "sqlite",
            "database_url": "postgresql://journal.invalid/never-opened-under-sqlite",
            "database_url_env": "OTHER_DATABASE_URL",
        }
        runtime_paths.config_path.write_text(yaml.dump(same_store), encoding="utf-8")

        assert pending_event_journal_restart(load_config(runtime_paths), runtime_paths) is False
