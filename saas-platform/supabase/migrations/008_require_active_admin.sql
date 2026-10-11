-- Only active administrators pass is_admin(), which every admin RLS policy calls, matching the backend's admin check.
-- Fresh installs get the same function from 000_consolidated_complete_schema.sql.
--
-- Safe to paste into the Supabase SQL editor: it runs in one transaction and every
-- statement is idempotent, so a partial or repeated run leaves the same end state.
-- Replacing the function keeps its owner and its EXECUTE grants.

BEGIN;

CREATE OR REPLACE FUNCTION is_admin()
RETURNS BOOLEAN AS $$
BEGIN
    RETURN EXISTS (
        SELECT 1 FROM accounts
        WHERE id = auth.uid()
        AND is_admin = TRUE
        AND status = 'active'
        AND deleted_at IS NULL
    );
END;
$$ LANGUAGE plpgsql SECURITY DEFINER;

COMMIT;
