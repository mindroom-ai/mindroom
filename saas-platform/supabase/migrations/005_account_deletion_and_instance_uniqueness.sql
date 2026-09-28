-- Account deletion changes only the account, and each subscription owns at most one instance.
-- Fresh installs get the same schema from 000_consolidated_complete_schema.sql.
--
-- Safe to paste into the Supabase SQL editor: it runs in one transaction and every
-- statement is idempotent, so a partial or repeated run leaves the same end state.

BEGIN;

-- Soft delete and restore no longer rewrite subscription or instance status.
-- The backend cancels Stripe billing and the instance lifecycle stops the account's instances
-- when deletion is requested; restoring the account resumes them only while Stripe or the
-- lifecycle says the subscription is entitled. Restore is refused after the 7-day grace period,
-- because cleanup then uninstalls the account's instances before deleting its rows, and it only
-- undoes what soft delete set, so a suspension that lands meanwhile is never lifted.
CREATE OR REPLACE FUNCTION soft_delete_account(
    target_account_id UUID,
    reason TEXT DEFAULT 'user_request',
    requested_by UUID DEFAULT NULL
) RETURNS VOID AS $$
BEGIN
    -- Mark account as deleted
    UPDATE accounts
    SET
        deleted_at = NOW(),
        deletion_reason = reason,
        deletion_requested_by = COALESCE(requested_by, target_account_id),
        deletion_requested_at = NOW(),
        -- A suspension outlives the deletion request, so restoring the account cannot lift it.
        status = CASE WHEN status = 'suspended' THEN status ELSE 'deleted' END,
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
END;
$$ LANGUAGE plpgsql SECURITY DEFINER;

-- Restore now reports whether it restored, and CREATE OR REPLACE cannot change a return type.
DROP FUNCTION IF EXISTS restore_account(UUID);
CREATE FUNCTION restore_account(
    target_account_id UUID
) RETURNS BOOLEAN AS $$
BEGIN
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
    AND deleted_at IS NOT NULL
    -- Only what soft delete set is undone; a suspended account stays suspended and pending deletion.
    AND status = 'deleted'
    -- After the grace period, cleanup may already have uninstalled everything the account ran.
    AND deleted_at > NOW() - INTERVAL '7 days';

    IF NOT FOUND THEN
        RETURN FALSE;
    END IF;

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
    RETURN TRUE;
END;
$$ LANGUAGE plpgsql SECURITY DEFINER;

REVOKE EXECUTE ON FUNCTION restore_account(UUID) FROM PUBLIC, anon, authenticated;
GRANT EXECUTE ON FUNCTION restore_account(UUID) TO service_role;

-- Hard delete only an account still pending deletion, so a restored account keeps its rows.
CREATE OR REPLACE FUNCTION hard_delete_account(
    target_account_id UUID
) RETURNS VOID AS $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM accounts WHERE id = target_account_id AND deleted_at IS NOT NULL) THEN
        RETURN;
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

-- One instance per subscription, enforced by the database so concurrent provision requests on
-- different backend replicas cannot both insert. If this fails because a subscription already has
-- several instance rows, resolve them first: each row maps to a live Helm release and OpenRouter key.
DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
        WHERE conrelid = 'instances'::regclass AND conname = 'instances_subscription_id_key'
    ) THEN
        ALTER TABLE instances ADD CONSTRAINT instances_subscription_id_key UNIQUE (subscription_id);
    END IF;
END$$;

-- The unique constraint's index replaces the plain lookup index.
DROP INDEX IF EXISTS idx_instances_subscription_id;

COMMIT;
