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

CREATE_FUNCTION = re.compile(r'CREATE\s+(?:OR\s+REPLACE\s+)?FUNCTION\s+(?:"?public"?\.)?"?(\w+)', re.IGNORECASE)
# Covers single-object grants, schema-wide grants, and default-privilege grants.
GRANT_EXECUTE = re.compile(
    r"GRANT\s+(?:EXECUTE|ALL(?:\s+PRIVILEGES)?)\s+ON\s+"
    r"(?:FUNCTIONS?|ROUTINES?|PROCEDURES?|ALL\s+(?:FUNCTIONS|ROUTINES|PROCEDURES)\s+IN\s+SCHEMA)"
    r"(?:\s+(.+?))?\s+TO\s+(.+?);",
    re.IGNORECASE | re.DOTALL,
)


def _names(sql_list: str) -> set[str]:
    without_arguments = re.sub(r"\([^)]*\)", "", sql_list).replace('"', "")
    return {name.strip().lower().removeprefix("public.") for name in without_arguments.split(",")}


def test_baseline_migration_makes_functions_private_before_creating_them() -> None:
    """Fresh databases create every function without PUBLIC, anon, or authenticated EXECUTE."""
    sql = BASELINE_MIGRATION_SQL.read_text(encoding="utf-8")
    first_function = CREATE_FUNCTION.search(sql)
    assert first_function is not None
    for statement in DEFAULT_PRIVILEGE_REVOKES:
        assert 0 <= sql.find(statement) < first_function.start()


def test_function_execute_migration_hardens_existing_databases() -> None:
    """Existing databases drop exec_sql, lose end-user EXECUTE on every function, and verify the result."""
    sql = FUNCTION_EXECUTE_MIGRATION_SQL.read_text(encoding="utf-8")
    assert "DROP FUNCTION IF EXISTS exec_sql(TEXT);" in sql
    assert "REVOKE EXECUTE ON ALL FUNCTIONS IN SCHEMA public FROM PUBLIC, anon, authenticated;" in sql
    for statement in DEFAULT_PRIVILEGE_REVOKES:
        assert statement in sql
    assert "has_function_privilege('anon', p.oid, 'EXECUTE')" in sql
    assert "has_function_privilege('authenticated', p.oid, 'EXECUTE')" in sql
    assert "RAISE EXCEPTION" in sql


def test_migrations_grant_end_users_only_allow_listed_functions() -> None:
    """No migration defines exec_sql or grants other functions to PUBLIC, anon, or authenticated."""
    for path in sorted(MIGRATIONS_DIR.glob("*.sql")):
        sql = path.read_text(encoding="utf-8")
        assert "exec_sql" not in {name.lower() for name in CREATE_FUNCTION.findall(sql)}, path.name
        for functions, roles in GRANT_EXECUTE.findall(sql):
            if _names(roles) & END_USER_ROLES:
                assert functions and _names(functions) <= END_USER_FUNCTIONS, path.name
