-- Apply function EXECUTE hardening to databases that already ran the baseline schema.
-- Fresh installs get the same privileges from 000_consolidated_complete_schema.sql.
--
-- PostgreSQL grants EXECUTE on new functions to PUBLIC, Supabase also grants it
-- on new public functions to anon and authenticated, and PostgREST exposes every
-- executable public function as /rest/v1/rpc/<name>. REVOKE ... FROM PUBLIC
-- leaves those explicit anon and authenticated grants in place.

-- exec_sql ran arbitrary SQL as its owner for anyone holding the anon key.
DROP FUNCTION IF EXISTS exec_sql(TEXT);

REVOKE EXECUTE ON FUNCTION
    update_updated_at_column(),
    set_subdomain_from_instance_id(),
    handle_new_user(),
    is_admin(),
    get_current_account_id(),
    soft_delete_account(UUID, TEXT, UUID),
    restore_account(UUID),
    hard_delete_account(UUID)
FROM PUBLIC, anon, authenticated;

-- RLS policies call is_admin() as the querying role.
GRANT EXECUTE ON FUNCTION is_admin() TO anon, authenticated;

ALTER DEFAULT PRIVILEGES REVOKE EXECUTE ON FUNCTIONS FROM PUBLIC;
ALTER DEFAULT PRIVILEGES IN SCHEMA public REVOKE EXECUTE ON FUNCTIONS FROM anon, authenticated;
