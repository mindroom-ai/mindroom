"""Account deletion SQL, run against a throwaway PostgreSQL with stand-ins for the Supabase `auth` objects.

The tests need the PostgreSQL server binaries (`initdb`, `pg_ctl`, `psql`). They use the directory in
`POSTGRES_BIN_DIR`, or the one holding `initdb` on PATH, and `POSTGRES_SHARE_DIR` when `initdb` cannot find its
share directory; without a working installation they are skipped. The server listens only on a Unix socket.
"""

from __future__ import annotations

import os
import shutil
import subprocess
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path

import pytest

MIGRATIONS_DIR = Path(__file__).resolve().parents[2] / "supabase/migrations"
BASELINE = MIGRATIONS_DIR / "000_consolidated_complete_schema.sql"
ACCOUNT_DELETION = MIGRATIONS_DIR / "005_account_deletion.sql"
ONE_INSTANCE = MIGRATIONS_DIR / "007_one_instance_per_subscription.sql"

SUPABASE_STANDINS = """
DO $$ BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'anon') THEN CREATE ROLE anon NOLOGIN; END IF;
    IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'authenticated') THEN CREATE ROLE authenticated NOLOGIN; END IF;
    IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'service_role') THEN CREATE ROLE service_role NOLOGIN; END IF;
END $$;
CREATE SCHEMA auth;
CREATE TABLE auth.users (id uuid PRIMARY KEY, email text, raw_user_meta_data jsonb DEFAULT '{}');
CREATE FUNCTION auth.uid() RETURNS uuid LANGUAGE sql AS $$ SELECT NULL::uuid $$;
CREATE FUNCTION auth.jwt() RETURNS jsonb LANGUAGE sql AS $$ SELECT '{}'::jsonb $$;
"""

ACCOUNT_A = "00000000-0000-0000-0000-00000000000a"
ACCOUNT_B = "00000000-0000-0000-0000-00000000000b"

DELETION_LIFECYCLE = f"""
INSERT INTO auth.users (id, email) VALUES ('{ACCOUNT_A}', 'a@example.com'), ('{ACCOUNT_B}', 'b@example.com');
INSERT INTO subscriptions (id, account_id, tier, status, stripe_subscription_id)
    VALUES ('10000000-0000-0000-0000-00000000000a', '{ACCOUNT_A}', 'pro', 'incomplete', 'sub_a');
INSERT INTO instances (account_id, subscription_id, status)
    VALUES ('{ACCOUNT_A}', '10000000-0000-0000-0000-00000000000a', 'running');
INSERT INTO payments (account_id, invoice_id, subscription_id, customer_id, amount)
    VALUES ('{ACCOUNT_A}', 'in_1', 'sub_a', 'cus_a', 200);
INSERT INTO webhook_events (account_id, stripe_event_id, event_type, payload)
    VALUES ('{ACCOUNT_A}', 'evt_1', 'customer.subscription.updated', '{{}}');
DO $$
DECLARE a UUID := '{ACCOUNT_A}'; b UUID := '{ACCOUNT_B}';
BEGIN
    PERFORM soft_delete_account(a, 'gdpr_request', a);
    ASSERT (SELECT status FROM accounts WHERE id = a) = 'deleted', 'soft delete marks the account deleted';
    ASSERT (SELECT status FROM subscriptions WHERE account_id = a) = 'incomplete', 'soft delete keeps subscriptions';
    ASSERT (SELECT status FROM instances WHERE account_id = a) = 'running', 'soft delete keeps instances';
    ASSERT restore_account(a), 'restore within the grace period';
    ASSERT (SELECT status FROM accounts WHERE id = a) = 'active', 'restore reactivates the account';
    ASSERT (SELECT status FROM subscriptions WHERE account_id = a) = 'incomplete', 'restore never activates billing';
    ASSERT NOT restore_account(a), 'nothing to restore';

    UPDATE accounts SET status = 'suspended' WHERE id = b;
    PERFORM soft_delete_account(b);
    ASSERT (SELECT status FROM accounts WHERE id = b) = 'suspended', 'soft delete keeps a suspension';
    ASSERT NOT restore_account(b), 'restore never lifts a suspension';

    PERFORM soft_delete_account(a);
    ASSERT NOT claim_account_hard_delete(a), 'no claim inside the grace period';
    PERFORM hard_delete_account(a);
    ASSERT EXISTS (SELECT 1 FROM instances WHERE account_id = a), 'an unclaimed account keeps its rows';
    UPDATE accounts SET deleted_at = NOW() - INTERVAL '8 days' WHERE id = a;
    ASSERT NOT restore_account(a), 'no restore after the grace period';
    ASSERT claim_account_hard_delete(a), 'claim after the grace period';
    UPDATE accounts SET deleted_at = NOW() WHERE id = a;
    ASSERT NOT restore_account(a), 'no restore once claimed';
    UPDATE accounts SET deleted_at = NOW() - INTERVAL '8 days' WHERE id = a;

    PERFORM hard_delete_account(a);
    ASSERT NOT EXISTS (SELECT 1 FROM instances WHERE account_id = a), 'instances deleted';
    ASSERT NOT EXISTS (SELECT 1 FROM subscriptions WHERE account_id = a), 'subscriptions deleted';
    ASSERT EXISTS (SELECT 1 FROM accounts WHERE id = a), 'the account row waits for its auth user';
    ASSERT (SELECT count(*) FROM audit_logs WHERE action = 'gdpr_account_hard_deleted') = 1, 'one hard-delete record';
    ASSERT claim_account_hard_delete(a), 'a failed auth deletion can be retried';
    PERFORM hard_delete_account(a);
    ASSERT (SELECT count(*) FROM audit_logs WHERE action = 'gdpr_account_hard_deleted') = 1, 'a retry records nothing';

    DELETE FROM auth.users WHERE id = a;  -- What deleting the user through the Supabase admin API does to the row.
    ASSERT NOT EXISTS (SELECT 1 FROM accounts WHERE id = a), 'deleting the auth user removes the account row';
    ASSERT (SELECT account_id FROM payments WHERE invoice_id = 'in_1') IS NULL, 'payment kept without its link';
    ASSERT (SELECT account_id FROM webhook_events WHERE stripe_event_id = 'evt_1') IS NULL, 'event kept without link';

    ASSERT NOT has_function_privilege('anon', 'restore_account(uuid)', 'EXECUTE'), 'anon cannot restore';
    ASSERT NOT has_function_privilege('authenticated', 'claim_account_hard_delete(uuid)', 'EXECUTE'), 'no user claim';
    ASSERT has_function_privilege('service_role', 'claim_account_hard_delete(uuid)', 'EXECUTE'), 'backend claims';
    ASSERT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'instances_subscription_id_key'), 'one instance';
END $$;
"""


@dataclass(frozen=True)
class Postgres:
    """A running throwaway server and the binaries that talk to it."""

    bin_dir: Path
    socket_dir: Path

    def create_database(self, name: str) -> None:
        subprocess.run([self.bin_dir / "createdb", "-h", self.socket_dir, "-U", "postgres", name], check=True)
        self.run(name, SUPABASE_STANDINS)

    def run(self, database: str, sql: str) -> subprocess.CompletedProcess[str]:
        """Run SQL in one psql session that stops at the first error."""
        command = [self.bin_dir / "psql", "-h", self.socket_dir, "-U", "postgres", "-d", database]
        return subprocess.run(
            [*command, "-v", "ON_ERROR_STOP=1", "-q", "-X"], input=sql, capture_output=True, text=True, check=False
        )

    def apply(self, database: str, *migrations: Path) -> subprocess.CompletedProcess[str]:
        return self.run(database, "\n".join(migration.read_text(encoding="utf-8") for migration in migrations))

    def value(self, database: str, sql: str) -> str:
        command = [self.bin_dir / "psql", "-h", self.socket_dir, "-U", "postgres", "-d", database, "-AtX", "-c", sql]
        return subprocess.run(command, capture_output=True, text=True, check=True).stdout.strip()


def _bin_dir() -> Path | None:
    if configured := os.environ.get("POSTGRES_BIN_DIR"):
        return Path(configured)
    initdb = shutil.which("initdb")
    return Path(initdb).parent if initdb else None


@pytest.fixture(scope="module")
def postgres(tmp_path_factory: pytest.TempPathFactory) -> Iterator[Postgres]:
    bin_dir = _bin_dir()
    if bin_dir is None or not (bin_dir / "pg_ctl").exists():
        pytest.skip("PostgreSQL server binaries are not installed")
    root = tmp_path_factory.mktemp("postgres")
    data_dir = root / "data"
    share = ["-L", os.environ["POSTGRES_SHARE_DIR"]] if os.environ.get("POSTGRES_SHARE_DIR") else []
    initdb = [bin_dir / "initdb", "-D", data_dir, "-U", "postgres", "--auth=trust", *share]
    initialized = subprocess.run(initdb, capture_output=True, text=True, check=False)
    if initialized.returncode != 0:
        pytest.skip(f"initdb failed: {initialized.stderr.strip()[-300:]}")
    options = f"-c listen_addresses='' -c unix_socket_directories={root}"
    pg_ctl = [bin_dir / "pg_ctl", "-D", data_dir, "-w", "-l", root / "log", "-o", options]
    subprocess.run([*pg_ctl, "start"], check=True, capture_output=True)
    try:
        yield Postgres(bin_dir, root)
    finally:
        subprocess.run([bin_dir / "pg_ctl", "-D", data_dir, "-m", "fast", "stop"], check=False, capture_output=True)


def test_account_deletion_lifecycle_on_a_fresh_install(postgres: Postgres) -> None:
    postgres.create_database("fresh")
    assert postgres.apply("fresh", BASELINE).returncode == 0

    result = postgres.run("fresh", DELETION_LIFECYCLE)

    assert result.returncode == 0, result.stderr


def test_every_incremental_migration_reruns_cleanly_on_the_baseline(postgres: Postgres) -> None:
    postgres.create_database("rerun")
    incremental = sorted(path for path in MIGRATIONS_DIR.glob("*.sql") if path != BASELINE)

    applied = postgres.apply("rerun", BASELINE, *incremental, *incremental)

    assert applied.returncode == 0, applied.stderr
    assert postgres.run("rerun", DELETION_LIFECYCLE).returncode == 0


def test_first_run_of_005_restarts_old_deletion_grace_periods_once(postgres: Postgres) -> None:
    # Older releases left these accounts running and billed, so the first cleanup must not tear them down unwarned.
    postgres.create_database("upgrade")
    assert postgres.apply("upgrade", BASELINE).returncode == 0
    old_request = f"""
        ALTER TABLE accounts DROP COLUMN hard_delete_started_at;  -- The schema before migration 005.
        INSERT INTO auth.users (id, email) VALUES ('{ACCOUNT_A}', 'old@example.com');
        UPDATE accounts SET deleted_at = NOW() - INTERVAL '10 days', status = 'deleted' WHERE id = '{ACCOUNT_A}';
    """
    assert postgres.run("upgrade", old_request).returncode == 0
    fresh_deleted_at = f"SELECT deleted_at > NOW() - INTERVAL '1 minute' FROM accounts WHERE id = '{ACCOUNT_A}'"

    assert postgres.apply("upgrade", ACCOUNT_DELETION).returncode == 0
    assert postgres.value("upgrade", fresh_deleted_at) == "t"

    postgres.run("upgrade", f"UPDATE accounts SET deleted_at = NOW() - INTERVAL '10 days' WHERE id = '{ACCOUNT_A}'")
    assert postgres.apply("upgrade", ACCOUNT_DELETION).returncode == 0
    assert postgres.value("upgrade", fresh_deleted_at) == "f"


def test_007_lists_duplicate_instances_and_changes_nothing(postgres: Postgres) -> None:
    postgres.create_database("duplicates")
    assert postgres.apply("duplicates", BASELINE).returncode == 0
    duplicates = f"""
        ALTER TABLE instances DROP CONSTRAINT instances_subscription_id_key;  -- The schema before migration 007.
        INSERT INTO auth.users (id, email) VALUES ('{ACCOUNT_A}', 'a@example.com');
        INSERT INTO subscriptions (id, account_id) VALUES ('10000000-0000-0000-0000-00000000000a', '{ACCOUNT_A}');
        INSERT INTO instances (account_id, subscription_id, status) VALUES
            ('{ACCOUNT_A}', '10000000-0000-0000-0000-00000000000a', 'deprovisioned'),
            ('{ACCOUNT_A}', '10000000-0000-0000-0000-00000000000a', 'running');
    """
    assert postgres.run("duplicates", duplicates).returncode == 0

    refused = postgres.apply("duplicates", ONE_INSTANCE)

    assert refused.returncode != 0
    assert "subscription 10000000-0000-0000-0000-00000000000a has instances {1,2}" in refused.stderr
    has_constraint = "SELECT count(*) FROM pg_constraint WHERE conname = 'instances_subscription_id_key'"
    assert postgres.value("duplicates", has_constraint) == "0"
