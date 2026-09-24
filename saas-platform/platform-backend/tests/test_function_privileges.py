"""Supabase migrations keep public functions unreachable by end-user roles."""

from pathlib import Path
import re

MIGRATIONS_DIR = Path(__file__).resolve().parents[2] / "supabase/migrations"
BASELINE_MIGRATION_SQL = MIGRATIONS_DIR / "000_consolidated_complete_schema.sql"
FUNCTION_EXECUTE_MIGRATION_SQL = MIGRATIONS_DIR / "003_restrict_function_execute.sql"

# RLS policies call is_admin() as the querying role.
END_USER_FUNCTIONS = {"is_admin"}
END_USER_ROLES = {"public", "anon", "authenticated"}

# The global statement removes PostgreSQL's PUBLIC grant; the per-schema one removes Supabase's
# anon/authenticated grant. A per-schema REVOKE ... FROM PUBLIC would leave the PUBLIC grant in place.
DEFAULT_PRIVILEGE_REVOKES = (
    "ALTER DEFAULT PRIVILEGES REVOKE EXECUTE ON FUNCTIONS FROM PUBLIC;",
    "ALTER DEFAULT PRIVILEGES IN SCHEMA public REVOKE EXECUTE ON FUNCTIONS FROM anon, authenticated;",
)

CREATE_FUNCTION = re.compile(r"CREATE (?:OR REPLACE )?FUNCTION (?:public\.)?(\w+)", re.IGNORECASE)
GRANT_EXECUTE = re.compile(r"GRANT EXECUTE ON FUNCTION\s+(.+?)\s+TO\s+(.+?);", re.IGNORECASE | re.DOTALL)
REVOKE_EXECUTE = re.compile(r"REVOKE EXECUTE ON FUNCTION\s+(.+?)\s+FROM\s+(.+?);", re.IGNORECASE | re.DOTALL)


def _names(sql_list: str) -> set[str]:
    without_arguments = re.sub(r"\([^)]*\)", "", sql_list)
    return {name.strip().lower().removeprefix("public.") for name in without_arguments.split(",")}


def test_baseline_migration_makes_functions_private_before_creating_them() -> None:
    """Fresh databases create every function without PUBLIC, anon, or authenticated EXECUTE."""
    sql = BASELINE_MIGRATION_SQL.read_text(encoding="utf-8")
    first_function = CREATE_FUNCTION.search(sql)
    assert first_function is not None
    for statement in DEFAULT_PRIVILEGE_REVOKES:
        assert 0 <= sql.find(statement) < first_function.start()


def test_function_execute_migration_hardens_existing_databases() -> None:
    """Existing databases drop exec_sql and lose end-user EXECUTE on every baseline function."""
    sql = FUNCTION_EXECUTE_MIGRATION_SQL.read_text(encoding="utf-8")
    assert "DROP FUNCTION IF EXISTS exec_sql(TEXT);" in sql
    for statement in DEFAULT_PRIVILEGE_REVOKES:
        assert statement in sql

    revoked = {
        name
        for functions, roles in REVOKE_EXECUTE.findall(sql)
        if _names(roles) >= END_USER_ROLES
        for name in _names(functions)
    }
    baseline_sql = BASELINE_MIGRATION_SQL.read_text(encoding="utf-8")
    assert {name.lower() for name in CREATE_FUNCTION.findall(baseline_sql)} <= revoked


def test_migrations_grant_end_users_only_allow_listed_functions() -> None:
    """No migration defines exec_sql or grants other functions to PUBLIC, anon, or authenticated."""
    for path in sorted(MIGRATIONS_DIR.glob("*.sql")):
        sql = path.read_text(encoding="utf-8")
        assert "exec_sql" not in {name.lower() for name in CREATE_FUNCTION.findall(sql)}, path.name
        for functions, roles in GRANT_EXECUTE.findall(sql):
            if _names(roles) & END_USER_ROLES:
                assert _names(functions) <= END_USER_FUNCTIONS, path.name
