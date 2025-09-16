-- Add soft delete support to accounts table
-- KISS principle - simple deleted_at column approach

-- Add soft delete columns to accounts
ALTER TABLE accounts
ADD COLUMN IF NOT EXISTS deleted_at TIMESTAMPTZ NULL,
ADD COLUMN IF NOT EXISTS deletion_reason TEXT NULL,
ADD COLUMN IF NOT EXISTS deletion_requested_by UUID NULL;

-- Add consent tracking columns (for GDPR)
ALTER TABLE accounts
ADD COLUMN IF NOT EXISTS consent_marketing BOOLEAN DEFAULT FALSE,
ADD COLUMN IF NOT EXISTS consent_analytics BOOLEAN DEFAULT FALSE,
ADD COLUMN IF NOT EXISTS consent_updated_at TIMESTAMPTZ NULL,
ADD COLUMN IF NOT EXISTS deletion_requested_at TIMESTAMPTZ NULL;

-- Create index for soft delete queries
CREATE INDEX IF NOT EXISTS idx_accounts_deleted_at ON accounts(deleted_at);

-- Update RLS policies to exclude soft-deleted accounts
DROP POLICY IF EXISTS "Users can view own account" ON accounts;
CREATE POLICY "Users can view own active account" ON accounts
    FOR SELECT USING (auth.uid() = id AND deleted_at IS NULL);

DROP POLICY IF EXISTS "Service role bypass" ON accounts;
CREATE POLICY "Service role bypass" ON accounts
    FOR ALL USING (auth.jwt()->>'role' = 'service_role');

-- Create a simple audit table for deletions
CREATE TABLE IF NOT EXISTS deletion_audit (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    table_name TEXT NOT NULL,
    record_id UUID NOT NULL,
    deletion_reason TEXT,
    requested_by UUID,
    deleted_at TIMESTAMPTZ DEFAULT NOW(),
    restored_at TIMESTAMPTZ NULL,
    hard_deleted_at TIMESTAMPTZ NULL
);

-- Create soft delete function for accounts
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
        status = 'deleted',
        updated_at = NOW()
    WHERE id = target_account_id
    AND deleted_at IS NULL;

    -- Log the deletion
    INSERT INTO deletion_audit (table_name, record_id, deletion_reason, requested_by)
    VALUES ('accounts', target_account_id, reason, COALESCE(requested_by, target_account_id));

    -- Also mark related data
    UPDATE subscriptions
    SET status = 'cancelled', updated_at = NOW()
    WHERE account_id = target_account_id;

    UPDATE instances
    SET status = 'deprovisioned', updated_at = NOW()
    WHERE account_id = target_account_id;
END;
$$ LANGUAGE plpgsql SECURITY DEFINER;

-- Create restore function (for accidental deletions)
CREATE OR REPLACE FUNCTION restore_account(
    target_account_id UUID
) RETURNS VOID AS $$
BEGIN
    -- Restore account
    UPDATE accounts
    SET
        deleted_at = NULL,
        deletion_reason = NULL,
        deletion_requested_by = NULL,
        status = 'active',
        updated_at = NOW()
    WHERE id = target_account_id
    AND deleted_at IS NOT NULL;

    -- Update audit log
    UPDATE deletion_audit
    SET restored_at = NOW()
    WHERE table_name = 'accounts'
    AND record_id = target_account_id
    AND restored_at IS NULL;
END;
$$ LANGUAGE plpgsql SECURITY DEFINER;

-- Create hard delete function (for permanent deletion after grace period)
CREATE OR REPLACE FUNCTION hard_delete_account(
    target_account_id UUID
) RETURNS VOID AS $$
BEGIN
    -- Update audit log first
    UPDATE deletion_audit
    SET hard_deleted_at = NOW()
    WHERE table_name = 'accounts'
    AND record_id = target_account_id
    AND hard_deleted_at IS NULL;

    -- Delete related data (cascade will handle most)
    DELETE FROM instances WHERE account_id = target_account_id;
    DELETE FROM subscriptions WHERE account_id = target_account_id;
    DELETE FROM audit_logs WHERE account_id = target_account_id;

    -- Finally delete the account
    DELETE FROM accounts WHERE id = target_account_id;
END;
$$ LANGUAGE plpgsql SECURITY DEFINER;

-- Create a view for active accounts (convenience)
CREATE OR REPLACE VIEW active_accounts AS
SELECT * FROM accounts WHERE deleted_at IS NULL;

-- Grant permissions
GRANT SELECT ON active_accounts TO anon, authenticated;
GRANT EXECUTE ON FUNCTION soft_delete_account TO authenticated, service_role;
GRANT EXECUTE ON FUNCTION restore_account TO service_role;
GRANT EXECUTE ON FUNCTION hard_delete_account TO service_role;
