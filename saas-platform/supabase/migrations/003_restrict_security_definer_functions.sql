-- Restrict the RLS-bypassing SECURITY DEFINER RPCs in the public schema to the platform backend.
-- Fresh installs get the same definitions and grants from 000_consolidated_complete_schema.sql.
-- Each function now rejects callers other than the service role before touching data.

-- Soft delete function for accounts
CREATE OR REPLACE FUNCTION soft_delete_account(
    target_account_id UUID,
    reason TEXT DEFAULT 'user_request',
    requested_by UUID DEFAULT NULL
) RETURNS VOID AS $$
BEGIN
    IF auth.jwt()->>'role' IS DISTINCT FROM 'service_role' THEN
        RAISE EXCEPTION 'permission denied for function soft_delete_account' USING ERRCODE = 'insufficient_privilege';
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

-- Restore function (for accidental deletions within grace period)
CREATE OR REPLACE FUNCTION restore_account(
    target_account_id UUID
) RETURNS VOID AS $$
BEGIN
    IF auth.jwt()->>'role' IS DISTINCT FROM 'service_role' THEN
        RAISE EXCEPTION 'permission denied for function restore_account' USING ERRCODE = 'insufficient_privilege';
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

-- Hard delete function (for permanent deletion after grace period)
CREATE OR REPLACE FUNCTION hard_delete_account(
    target_account_id UUID
) RETURNS VOID AS $$
BEGIN
    IF auth.jwt()->>'role' IS DISTINCT FROM 'service_role' THEN
        RAISE EXCEPTION 'permission denied for function hard_delete_account' USING ERRCODE = 'insufficient_privilege';
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

-- CREATE FUNCTION grants EXECUTE to PUBLIC, and Supabase default privileges grant it to anon and authenticated.
REVOKE ALL ON FUNCTION soft_delete_account(UUID, TEXT, UUID) FROM PUBLIC, anon, authenticated;
REVOKE ALL ON FUNCTION restore_account(UUID) FROM PUBLIC, anon, authenticated;
REVOKE ALL ON FUNCTION hard_delete_account(UUID) FROM PUBLIC, anon, authenticated;
GRANT EXECUTE ON FUNCTION soft_delete_account(UUID, TEXT, UUID) TO service_role;
GRANT EXECUTE ON FUNCTION restore_account(UUID) TO service_role;
GRANT EXECUTE ON FUNCTION hard_delete_account(UUID) TO service_role;

-- Helper to run privileged SQL via service role (used by tooling scripts)
CREATE OR REPLACE FUNCTION exec_sql(query TEXT)
RETURNS VOID
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = public
AS $$
BEGIN
    IF auth.jwt()->>'role' IS DISTINCT FROM 'service_role' THEN
        RAISE EXCEPTION 'permission denied for function exec_sql' USING ERRCODE = 'insufficient_privilege';
    END IF;

    EXECUTE query;
END;
$$;

-- Supabase default privileges grant EXECUTE to anon and authenticated, so revoking PUBLIC alone is not enough.
REVOKE ALL ON FUNCTION exec_sql(TEXT) FROM PUBLIC, anon, authenticated;
GRANT EXECUTE ON FUNCTION exec_sql(TEXT) TO service_role;
