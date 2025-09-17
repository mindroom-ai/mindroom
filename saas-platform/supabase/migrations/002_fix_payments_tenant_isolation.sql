-- Migration: Fix payments table tenant isolation
-- Date: 2025-09-11
-- Purpose: Add tenant association to payments table and create RLS policy
-- Security Issue: Related to SECURITY_REVIEW_02_MULTITENANCY.md - Ensure all financial data is tenant-isolated

-- Step 1: Add account_id column to payments table
ALTER TABLE payments
ADD COLUMN IF NOT EXISTS account_id UUID REFERENCES accounts(id);

-- Step 2: Create index for performance
CREATE INDEX IF NOT EXISTS idx_payments_account_id ON payments(account_id);

-- Step 3: Enable RLS on payments table (if not already enabled)
ALTER TABLE payments ENABLE ROW LEVEL SECURITY;

-- Step 4: Drop old policy if it exists (using customer_id) and create new one with account_id
DROP POLICY IF EXISTS "Users can view own payments" ON payments;
CREATE POLICY "Users can view own payments" ON payments
    FOR SELECT USING (
        account_id = auth.uid() OR
        is_admin()
    );

-- Step 5: Allow service role to insert payments (for webhook processing)
-- Service role bypasses RLS by default

-- Step 6: Backfill account_id for existing payments using customer_id
UPDATE payments p
SET account_id = a.id
FROM accounts a
WHERE p.customer_id = a.stripe_customer_id
  AND p.account_id IS NULL;

-- Step 7: Backfill any remaining payments using subscription_id
UPDATE payments p
SET account_id = s.account_id
FROM subscriptions s
WHERE p.subscription_id = s.subscription_id
  AND p.account_id IS NULL;

-- Step 8: Add RLS policy for admins to manage all payments
CREATE POLICY "Admins can manage all payments" ON payments
    FOR ALL USING (is_admin())
    WITH CHECK (is_admin());

-- Step 9: Add audit log entry for this security fix
INSERT INTO audit_logs (
    action,
    resource,
    resource_id,
    details,
    account_id,
    created_at
) VALUES (
    'security_fix',
    'payments',
    NULL,
    jsonb_build_object(
        'migration', '002_fix_payments_tenant_isolation',
        'issue', 'Added tenant isolation to payments table',
        'severity', 'HIGH'
    ),
    NULL,  -- System action
    NOW()
);

-- Step 10: Document the security fix
COMMENT ON TABLE payments IS
'Stores payment records from Stripe. Tenant-isolated via account_id and RLS policies.';

COMMENT ON COLUMN payments.account_id IS
'Account ID for tenant isolation. Required for all new payment records to ensure financial data segregation.';
