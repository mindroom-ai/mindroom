-- Account deletion changes only the account, can be cancelled only until cleanup claims it, and keeps
-- payment and webhook records without their account link.
-- Before applying it, list the accounts whose grace period it restarts (see the comment above the column below):
--
--   SELECT id, email, deleted_at FROM accounts WHERE deleted_at < NOW() - INTERVAL '7 days';
-- Fresh installs get the same schema from 000_consolidated_complete_schema.sql.
--
-- Safe to paste into the Supabase SQL editor: it runs in one transaction and every
-- statement is idempotent, so a partial or repeated run leaves the same end state.

BEGIN;

-- Set once the nightly cleanup starts tearing a deleted account down; from then on it can no longer be restored.
-- The first run of this migration also restarts the 7-day grace period of every account whose deletion was
-- requested longer ago. The soft delete of older releases left their instances running and their billing live,
-- and these accounts are still here because their hard delete failed on payment or webhook references or never
-- ran, so their owners could still cancel until now; without a new grace period the first cleanup after the
-- upgrade would tear them down with no chance to cancel.
-- Adding the column is what records the first run, so rerunning the migration never restarts a grace period.
DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM information_schema.columns
        WHERE table_schema = 'public' AND table_name = 'accounts' AND column_name = 'hard_delete_started_at'
    ) THEN
        ALTER TABLE accounts ADD COLUMN hard_delete_started_at TIMESTAMPTZ NULL;
        UPDATE accounts SET deleted_at = NOW() WHERE deleted_at < NOW() - INTERVAL '7 days';
    END IF;
END$$;

-- Payment records and webhook idempotency keys outlive the account; only their account link goes,
-- like audit_logs. Drop every existing account foreign key on them, whatever an older schema named it.
DO $$
DECLARE
    account_fk RECORD;
BEGIN
    FOR account_fk IN
        SELECT conrelid::regclass AS table_name, conname FROM pg_constraint
        WHERE contype = 'f'
            AND confrelid = 'accounts'::regclass
            AND conrelid IN ('payments'::regclass, 'webhook_events'::regclass)
    LOOP
        EXECUTE format('ALTER TABLE %s DROP CONSTRAINT %I', account_fk.table_name, account_fk.conname);
    END LOOP;
END$$;
ALTER TABLE payments
    ADD CONSTRAINT payments_account_id_fkey FOREIGN KEY (account_id) REFERENCES accounts(id) ON DELETE SET NULL;
ALTER TABLE webhook_events
    ADD CONSTRAINT webhook_events_account_id_fkey FOREIGN KEY (account_id) REFERENCES accounts(id) ON DELETE SET NULL;

-- Soft delete and restore no longer rewrite subscription or instance status.
-- The backend lets Stripe billing end with the paid period and the instance lifecycle stops the account's
-- instances when deletion is requested; restoring the account resumes them only while Stripe or the
-- lifecycle says the subscription is entitled. Restore is refused after the 7-day grace period and once
-- cleanup claimed the account, because cleanup then uninstalls the account's instances before deleting its
-- rows, and it only undoes what soft delete set, so a suspension that lands meanwhile is never lifted.
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
    AND deleted_at > NOW() - INTERVAL '7 days'
    AND hard_delete_started_at IS NULL;

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

-- Claim an account whose grace period ended for teardown, using the database clock like restore_account.
-- A claimed account can no longer be restored, and it stays claimed so a failed teardown is simply retried.
CREATE OR REPLACE FUNCTION claim_account_hard_delete(
    target_account_id UUID
) RETURNS BOOLEAN AS $$
BEGIN
    UPDATE accounts
    SET hard_delete_started_at = COALESCE(hard_delete_started_at, NOW())
    WHERE id = target_account_id
    AND deleted_at IS NOT NULL
    AND deleted_at <= NOW() - INTERVAL '7 days';
    RETURN FOUND;
END;
$$ LANGUAGE plpgsql SECURITY DEFINER;

REVOKE EXECUTE ON FUNCTION claim_account_hard_delete(UUID) FROM PUBLIC, anon, authenticated;
GRANT EXECUTE ON FUNCTION claim_account_hard_delete(UUID) TO service_role;

-- Hard delete only an account cleanup claimed, so a restored account keeps its rows. It keeps the accounts row,
-- which goes with the auth user the backend deletes last.
CREATE OR REPLACE FUNCTION hard_delete_account(
    target_account_id UUID
) RETURNS VOID AS $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM accounts WHERE id = target_account_id AND hard_delete_started_at IS NOT NULL
    ) THEN
        RETURN;
    END IF;

    -- Delete related data (cascade will handle most)
    DELETE FROM instances WHERE account_id = target_account_id;
    DELETE FROM subscriptions WHERE account_id = target_account_id;
    DELETE FROM audit_logs WHERE account_id = target_account_id;

    -- The accounts row goes last, with its auth user (ON DELETE CASCADE), which the backend deletes through the
    -- Supabase admin API; until then the claimed row is what lets the next cleanup run finish the deletion.

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

COMMIT;
