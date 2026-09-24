-- Apply function EXECUTE hardening to databases that already ran the baseline schema.
-- Fresh installs get the same privileges from 000_consolidated_complete_schema.sql.
--
-- PostgreSQL grants EXECUTE on new functions to PUBLIC, Supabase also grants it
-- on new public functions to anon and authenticated, and PostgREST exposes every
-- executable public function as /rest/v1/rpc/<name>. REVOKE ... FROM PUBLIC
-- leaves those explicit anon and authenticated grants in place.

-- exec_sql ran arbitrary SQL as its owner for anyone holding the anon key.
DROP FUNCTION IF EXISTS exec_sql(TEXT);

-- Cover every function, including ones only older schema versions created.
REVOKE EXECUTE ON ALL FUNCTIONS IN SCHEMA public FROM PUBLIC, anon, authenticated;

-- RLS policies call is_admin() as the querying role.
GRANT EXECUTE ON FUNCTION is_admin() TO anon, authenticated;

ALTER DEFAULT PRIVILEGES REVOKE EXECUTE ON FUNCTIONS FROM PUBLIC;
ALTER DEFAULT PRIVILEGES IN SCHEMA public REVOKE EXECUTE ON FUNCTIONS FROM anon, authenticated;

-- REVOKE only warns for functions another role owns, so fail if any stayed exposed.
DO $$
DECLARE
    exposed TEXT;
BEGIN
    SELECT string_agg(p.oid::regprocedure::TEXT, ', ') INTO exposed
    FROM pg_proc p
    JOIN pg_namespace n ON n.oid = p.pronamespace
    WHERE n.nspname = 'public'
        AND p.oid <> 'is_admin()'::regprocedure
        AND NOT EXISTS (
            SELECT 1 FROM pg_depend d
            WHERE d.classid = 'pg_proc'::regclass AND d.objid = p.oid AND d.deptype = 'e'
        )
        AND (
            has_function_privilege('anon', p.oid, 'EXECUTE')
            OR has_function_privilege('authenticated', p.oid, 'EXECUTE')
        );
    IF exposed IS NOT NULL THEN
        RAISE EXCEPTION 'public functions still executable by anon or authenticated: %', exposed;
    END IF;
END$$;
