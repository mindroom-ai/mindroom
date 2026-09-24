-- Restrict the SECURITY DEFINER account lifecycle RPCs and exec_sql to the platform backend.
-- Fresh installs get the same definitions and grants from 000_consolidated_complete_schema.sql.
--
-- soft_delete_account, restore_account and hard_delete_account bypass RLS on accounts,
-- subscriptions, instances and audit_logs while acting on a caller-supplied target_account_id.
-- soft_delete_account was granted to authenticated, and Supabase's default privileges grant
-- EXECUTE on every new public function directly to anon and authenticated (the earlier
-- REVOKE ... FROM PUBLIC does not remove those grants), so any holder of the public anon key could
-- soft delete, restore or hard delete an arbitrary account through PostgREST
-- (POST /rest/v1/rpc/<name>), or run arbitrary SQL through exec_sql.
-- The revokes below remove those grants, and the redefined bodies refuse callers without platform
-- privileges, so widening a lifecycle grant later cannot reopen the bypass on its own.

CREATE OR REPLACE FUNCTION has_platform_privileges()
RETURNS BOOLEAN AS $$
DECLARE
    jwt_claims JSONB := NULLIF(current_setting('request.jwt.claims', TRUE), '')::JSONB;
BEGIN
    -- IS NOT DISTINCT FROM keeps the result non-NULL for claims without a role (e.g. '{}'),
    -- because callers test NOT has_platform_privileges() and NOT NULL would skip their RAISE.
    RETURN jwt_claims IS NULL
        OR jwt_claims->>'role' IS NOT DISTINCT FROM 'service_role'
        OR is_admin();
END;
$$ LANGUAGE plpgsql SECURITY DEFINER;

CREATE OR REPLACE FUNCTION soft_delete_account(
    target_account_id UUID,
    reason TEXT DEFAULT 'user_request',
    requested_by UUID DEFAULT NULL
) RETURNS VOID AS $$
BEGIN
    -- These statements bypass RLS, so deletion stays a platform operation even if EXECUTE is
    -- ever granted more widely again; users go through the backend's confirmed GDPR flow.
    IF NOT has_platform_privileges() THEN
        RAISE EXCEPTION 'soft_delete_account requires platform privileges' USING ERRCODE = 'insufficient_privilege';
    END IF;

    -- Mark account as deleted
    UPDATE accounts
    SET
        deleted_at = NOW(),
        deletion_reason = reason,
        deletion_requested_by = COALESCE(requested_by, target_account_id),
        deletion_requested_at = NOW(),
        status = 'deleted',
        updated_at = NOW()
    WHERE id = target_account_id
    AND deleted_at IS NULL;

    -- Log the deletion
    INSERT INTO audit_logs (account_id, action, resource_type, resource_id, details, success)
    VALUES (
        target_account_id,
        'gdpr_deletion_scheduled',
        'account',
        target_account_id::text,
        jsonb_build_object(
            'reason', reason,
            'requested_by', COALESCE(requested_by, target_account_id)
        ),
        TRUE
    );

    -- Mark related data while avoiding unnecessary churn
    UPDATE subscriptions
    SET status = 'cancelled', updated_at = NOW()
    WHERE account_id = target_account_id
    AND status != 'cancelled';

    UPDATE instances
    SET status = 'deprovisioned', updated_at = NOW()
    WHERE account_id = target_account_id
    AND status NOT IN ('deprovisioned', 'stopped');
END;
$$ LANGUAGE plpgsql SECURITY DEFINER;

CREATE OR REPLACE FUNCTION restore_account(
    target_account_id UUID
) RETURNS VOID AS $$
BEGIN
    -- These statements bypass RLS, so restore stays a platform operation.
    IF NOT has_platform_privileges() THEN
        RAISE EXCEPTION 'restore_account requires platform privileges' USING ERRCODE = 'insufficient_privilege';
    END IF;

    -- Restore account
    UPDATE accounts
    SET
        deleted_at = NULL,
        deletion_reason = NULL,
        deletion_requested_by = NULL,
        deletion_requested_at = NULL,
        status = 'active',
        updated_at = NOW()
    WHERE id = target_account_id
    AND deleted_at IS NOT NULL;

    -- Restore related data that was cancelled/deprovisioned during soft delete
    UPDATE subscriptions
    SET
        status = 'active',
        updated_at = NOW()
    WHERE account_id = target_account_id
    AND status = 'cancelled';

    UPDATE instances
    SET
        status = 'running',
        updated_at = NOW()
    WHERE account_id = target_account_id
    AND status = 'deprovisioned';

    -- Audit log entry
    INSERT INTO audit_logs (account_id, action, resource_type, resource_id, details, success)
    VALUES (
        target_account_id,
        'gdpr_deletion_cancelled',
        'account',
        target_account_id::text,
        jsonb_build_object('status', 'restored'),
        TRUE
    );
END;
$$ LANGUAGE plpgsql SECURITY DEFINER;

CREATE OR REPLACE FUNCTION hard_delete_account(
    target_account_id UUID
) RETURNS VOID AS $$
BEGIN
    -- These statements bypass RLS and are irreversible, so keep them platform-only.
    IF NOT has_platform_privileges() THEN
        RAISE EXCEPTION 'hard_delete_account requires platform privileges' USING ERRCODE = 'insufficient_privilege';
    END IF;

    -- Delete related data (cascade will handle most)
    DELETE FROM instances WHERE account_id = target_account_id;
    DELETE FROM subscriptions WHERE account_id = target_account_id;
    DELETE FROM audit_logs WHERE account_id = target_account_id;

    -- Finally delete the account
    DELETE FROM accounts WHERE id = target_account_id;

    -- Audit entry for hard delete (system action)
    INSERT INTO audit_logs (action, resource_type, resource_id, details, success)
    VALUES (
        'gdpr_account_hard_deleted',
        'account',
        target_account_id::text,
        jsonb_build_object('source', 'hard_delete_account'),
        TRUE
    );
END;
$$ LANGUAGE plpgsql SECURITY DEFINER;

REVOKE EXECUTE ON FUNCTION has_platform_privileges FROM PUBLIC, anon, authenticated;
REVOKE EXECUTE ON FUNCTION soft_delete_account FROM PUBLIC, anon, authenticated;
REVOKE EXECUTE ON FUNCTION restore_account FROM PUBLIC, anon, authenticated;
REVOKE EXECUTE ON FUNCTION hard_delete_account FROM PUBLIC, anon, authenticated;
GRANT EXECUTE ON FUNCTION soft_delete_account TO service_role;
GRANT EXECUTE ON FUNCTION restore_account TO service_role;
GRANT EXECUTE ON FUNCTION hard_delete_account TO service_role;

REVOKE ALL ON FUNCTION exec_sql(TEXT) FROM PUBLIC, anon, authenticated;
GRANT EXECUTE ON FUNCTION exec_sql(TEXT) TO service_role;
