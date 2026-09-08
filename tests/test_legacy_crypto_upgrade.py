"""Legacy transport retirement preserves the original Matrix encryption identity."""

from __future__ import annotations

import sqlite3
from contextlib import closing
from pathlib import Path
from uuid import uuid4

import pytest
from nio import LocalProtocolError
from nio.crypto import InboundGroupSession, OlmAccount, OutboundGroupSession
from nio.durable import DurableSyncConfig, SlidingSyncConfig
from nio.store import DefaultStore
from nio.store._sqlite_lease import FileLease

from mindroom.event_journal import EventJournalStore
from mindroom.matrix._owned_session import MatrixCredentials, open_owned_matrix_session
from mindroom.matrix.client_session import olm_store_dir
from mindroom.matrix.legacy_crypto_upgrade import retire_legacy_crypto_recovery
from tests.test_matrix_agent_manager import _runtime_paths

ACCOUNT = "@bot:example.org"
ROOM = "!room:example.org"
DEVICE = "DEVICE"
_SCHEMA = Path(__file__).parent / "fixtures" / "pre_nio1_recovery.sql"


def _legacy_crypto(
    path: Path,
    recovery: str,
    *,
    pickle_key: str = "DEFAULT_KEY",
) -> tuple[dict[str, str], InboundGroupSession, str]:
    """Persist real Olm/Megolm keys beside frozen released recovery tables."""
    path.mkdir(parents=True, exist_ok=True)
    store = DefaultStore(ACCOUNT, DEVICE, str(path), pickle_key=pickle_key)
    account = OlmAccount()
    store.save_account(account)
    outbound = OutboundGroupSession()
    inbound = InboundGroupSession(
        outbound.session_key,
        account.identity_keys["ed25519"],
        account.identity_keys["curve25519"],
        ROOM,
    )
    store.save_inbound_group_session(inbound)
    store.database.close()
    outbound.mark_as_shared()
    ciphertext = outbound.encrypt("retained encrypted history")
    with closing(sqlite3.connect(path / f"{ACCOUNT}_{DEVICE}.db")) as connection:
        connection.executescript(_SCHEMA.read_text())
        if recovery == "pending":
            connection.execute("""
                INSERT INTO pendingtimelineevents VALUES (
                    1, '!room:example.org', 2, 1, '$old', X'00', 1, 1, 0, 0, 'live', 1,
                    (SELECT id FROM accounts LIMIT 1))
            """)
        elif recovery == "gap":
            connection.execute("""
                INSERT INTO syncrecoverygaps VALUES (
                    1, '!room:example.org', 2, 'old-target', 'old-cursor', 0,
                    (SELECT id FROM accounts LIMIT 1))
            """)
        elif recovery == "abandoned":
            connection.execute("""
                INSERT INTO syncrecoveryabandonedrooms VALUES (
                    1, '!room:example.org', 'unknown', (SELECT id FROM accounts LIMIT 1))
            """)
        connection.commit()
    return account.identity_keys, inbound, ciphertext


@pytest.mark.asyncio
@pytest.mark.parametrize("recovery", ["pending", "gap", "abandoned", "clean"])
async def test_owned_startup_retires_legacy_recovery_without_changing_keys(tmp_path: Path, recovery: str) -> None:
    """Old pending transport work cannot block deployment or replace retained crypto keys."""
    runtime = _runtime_paths(tmp_path)
    keys, inbound, ciphertext = _legacy_crypto(olm_store_dir(ACCOUNT, runtime), recovery)
    journal = EventJournalStore.open_sqlite(tmp_path / "journal.db")
    principal = journal.principal(ACCOUNT)
    first_consumer = None
    try:
        for _ in range(2):
            opened = await open_owned_matrix_session(
                "https://example.org",
                MatrixCredentials(ACCOUNT, DEVICE, "test-token"),
                runtime,
                consumer_store=principal,
                new_consumer_generation=uuid4(),
                config=DurableSyncConfig(),
            )
            try:
                assert opened.client.device_id == DEVICE
                assert opened.client.olm is not None
                assert opened.client.olm.account.identity_keys == keys
                retained = opened.client.olm.inbound_group_store.get(ROOM, inbound.sender_key, inbound.id)
                assert retained is not None
                assert retained.decrypt(ciphertext)[0] == "retained encrypted history"
                if first_consumer is None:
                    first_consumer = opened.consumer
                else:
                    assert opened.consumer == first_consumer
            finally:
                await opened.session.close()
                await opened.client.close()
    finally:
        await journal.close()


@pytest.mark.parametrize("failure", ["identity", "lease", "transaction"])
def test_failed_crypto_upgrade_preserves_old_recovery(tmp_path: Path, failure: str) -> None:
    """Wrong ownership or an interrupted transaction cannot partially clear recovery."""
    _legacy_crypto(tmp_path, "pending")
    path = tmp_path / f"{ACCOUNT}_{DEVICE}.db"
    if failure == "identity":
        with pytest.raises(LocalProtocolError, match="identity mismatch"):
            retire_legacy_crypto_recovery(path, user_id="@different:example.org", device_id=DEVICE)
    elif failure == "lease":
        with closing(FileLease(path)), pytest.raises(LocalProtocolError, match="lease is already held"):
            retire_legacy_crypto_recovery(path, user_id=ACCOUNT, device_id=DEVICE)
    else:
        with closing(sqlite3.connect(path)) as connection:
            connection.executescript("""
                INSERT INTO syncrecoverygaps VALUES (
                    1, '!room:example.org', 2, 'target', NULL, 0, (SELECT id FROM accounts LIMIT 1));
                CREATE TRIGGER fail_upgrade BEFORE DELETE ON syncrecoverygaps
                BEGIN SELECT RAISE(ABORT, 'interrupted upgrade'); END;
            """)
        with pytest.raises(sqlite3.IntegrityError, match="interrupted upgrade"):
            retire_legacy_crypto_recovery(path, user_id=ACCOUNT, device_id=DEVICE)
    with closing(sqlite3.connect(path)) as connection:
        assert connection.execute("SELECT COUNT(*) FROM pendingtimelineevents").fetchone() == (1,)


@pytest.mark.asyncio
async def test_durable_crypto_store_is_never_migrated_again(tmp_path: Path) -> None:
    """The durable marker protects retained transport state from later upgrades."""
    runtime = _runtime_paths(tmp_path)
    path = olm_store_dir(ACCOUNT, runtime)
    _legacy_crypto(path, "clean")
    journal = EventJournalStore.open_sqlite(tmp_path / "journal.db")
    try:
        opened = await open_owned_matrix_session(
            "https://example.org",
            MatrixCredentials(ACCOUNT, DEVICE, "test-token"),
            runtime,
            consumer_store=journal.principal(ACCOUNT),
            new_consumer_generation=uuid4(),
            config=DurableSyncConfig(),
        )
        await opened.session.close()
        await opened.client.close()
        database_path = path / f"{ACCOUNT}_{DEVICE}.db"
        with closing(sqlite3.connect(database_path)) as connection:
            connection.execute("""
                INSERT INTO syncrecoverygaps VALUES (
                    1, '!room:example.org', 2, 'target', NULL, 0, (SELECT id FROM accounts LIMIT 1))
            """)
            connection.execute("UPDATE NioDurableMeta SET cursor = 'retained-durable-checkpoint'")
            connection.commit()
        retire_legacy_crypto_recovery(database_path, user_id=ACCOUNT, device_id=DEVICE)
        with closing(sqlite3.connect(database_path)) as connection:
            assert connection.execute("SELECT COUNT(*) FROM syncrecoverygaps").fetchone() == (1,)
            assert connection.execute("SELECT cursor FROM NioDurableMeta").fetchone() == (
                "retained-durable-checkpoint",
            )
    finally:
        await journal.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("sliding", [False, True])
async def test_legacy_sync_checkpoint_is_not_adopted(tmp_path: Path, sliding: bool) -> None:
    """Retired checkpoints cannot select the wrong transport or skip the fresh baseline."""
    runtime = _runtime_paths(tmp_path)
    path = olm_store_dir(ACCOUNT, runtime)
    keys, _, _ = _legacy_crypto(path, "pending")
    store = DefaultStore(ACCOUNT, DEVICE, str(path), pickle_key="DEFAULT_KEY")
    store.save_sync_token("obsolete-checkpoint")
    store.database.close()
    journal = EventJournalStore.open_sqlite(tmp_path / "journal.db")
    try:
        for _ in range(2):
            opened = await open_owned_matrix_session(
                "https://example.org",
                MatrixCredentials(ACCOUNT, DEVICE, "test-token"),
                runtime,
                consumer_store=journal.principal(ACCOUNT),
                new_consumer_generation=uuid4(),
                config=DurableSyncConfig(sliding=SlidingSyncConfig() if sliding else None),
            )
            try:
                assert opened.client.olm is not None
                assert opened.client.olm.account.identity_keys == keys
                with closing(sqlite3.connect(path / f"{ACCOUNT}_{DEVICE}.db")) as connection:
                    assert connection.execute("SELECT cursor FROM NioDurableMeta").fetchone() == (None,)
                    assert connection.execute("SELECT COUNT(*) FROM synctokens").fetchone() == (0,)
            finally:
                await opened.session.close()
                await opened.client.close()
    finally:
        await journal.close()


def test_invalid_crypto_account_keeps_legacy_recovery(tmp_path: Path) -> None:
    """A failed key preflight must leave all recovery rows available for operator repair."""
    _legacy_crypto(tmp_path, "pending")
    path = tmp_path / f"{ACCOUNT}_{DEVICE}.db"
    with closing(sqlite3.connect(path)) as connection:
        connection.execute("UPDATE accounts SET account = ?", (b"invalid-pickle",))
        connection.commit()
    with pytest.raises(ValueError, match=r"(?i)pickle"):
        retire_legacy_crypto_recovery(path, user_id=ACCOUNT, device_id=DEVICE)
    with closing(sqlite3.connect(path)) as connection:
        assert connection.execute("SELECT COUNT(*) FROM pendingtimelineevents").fetchone() == (1,)
        assert connection.execute("SELECT account FROM accounts").fetchone() == (b"invalid-pickle",)


@pytest.mark.parametrize("correct_key", [False, True])
def test_crypto_retirement_authenticates_the_configured_pickle_key(tmp_path: Path, correct_key: bool) -> None:
    """Only the actual configured key permits retirement; wrong keys retain recovery."""
    _legacy_crypto(tmp_path, "pending", pickle_key="custom-pickle-key")
    path = tmp_path / f"{ACCOUNT}_{DEVICE}.db"
    if correct_key:
        retire_legacy_crypto_recovery(path, user_id=ACCOUNT, device_id=DEVICE, pickle_key="custom-pickle-key")
    else:
        with pytest.raises(ValueError, match="pickle couldn't be decrypted"):
            retire_legacy_crypto_recovery(path, user_id=ACCOUNT, device_id=DEVICE, pickle_key="wrong-key")
    with closing(sqlite3.connect(path)) as connection:
        assert connection.execute("SELECT COUNT(*) FROM pendingtimelineevents").fetchone() == (0 if correct_key else 1,)


def test_sync_token_retirement_failure_rolls_back_recovery(tmp_path: Path) -> None:
    """Failure in the last retired table cannot leave earlier recovery partially cleared."""
    _legacy_crypto(tmp_path, "pending")
    store = DefaultStore(ACCOUNT, DEVICE, str(tmp_path), pickle_key="DEFAULT_KEY")
    store.save_sync_token("obsolete-checkpoint")
    store.database.close()
    path = tmp_path / f"{ACCOUNT}_{DEVICE}.db"
    with closing(sqlite3.connect(path)) as connection:
        connection.executescript("""
            CREATE TRIGGER fail_token_retirement BEFORE DELETE ON synctokens
            BEGIN SELECT RAISE(ABORT, 'interrupted token retirement'); END;
        """)
    with pytest.raises(sqlite3.IntegrityError, match="interrupted token retirement"):
        retire_legacy_crypto_recovery(path, user_id=ACCOUNT, device_id=DEVICE)
    with closing(sqlite3.connect(path)) as connection:
        assert connection.execute("SELECT COUNT(*) FROM pendingtimelineevents").fetchone() == (1,)
        assert connection.execute("SELECT COUNT(*) FROM synctokens").fetchone() == (1,)
