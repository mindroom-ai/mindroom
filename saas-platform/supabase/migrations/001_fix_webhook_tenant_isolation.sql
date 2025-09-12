-- Migration: Fix webhook events tenant isolation
-- Date: 2025-09-11
-- Purpose: Add tenant association to webhook_events table and create RLS policy
-- Security Issue: SECURITY_REVIEW_02_MULTITENANCY.md - Webhook Events Lack Tenant Isolation

-- Step 1: Add account_id column to webhook_events table
-- This associates each webhook event with a specific tenant account
ALTER TABLE webhook_events
ADD COLUMN IF NOT EXISTS account_id UUID REFERENCES accounts(id);

-- Step 2: Create index for performance
CREATE INDEX IF NOT EXISTS idx_webhook_events_account_id ON webhook_events(account_id);

-- Step 3: Enable RLS on webhook_events table
ALTER TABLE webhook_events ENABLE ROW LEVEL SECURITY;

-- Step 4: Create RLS policy for webhook_events
-- Users can only view webhook events associated with their account
CREATE POLICY "Users can view own webhook events" ON webhook_events
    FOR SELECT USING (
        account_id = auth.uid() OR
        is_admin()
    );

-- Step 5: Allow service role to insert/update webhook events
-- Service role bypasses RLS by default, but we document the intended access pattern
COMMENT ON TABLE webhook_events IS
'Stores Stripe webhook events. Service role can insert/update. Users can only view their own events via RLS.';

-- Step 6: Backfill account_id for existing webhook events
-- This associates orphaned webhook events with the correct account based on subscription data
UPDATE webhook_events we
SET account_id = s.account_id
FROM (
    SELECT DISTINCT
        (payload->>'subscription')::text as subscription_id,
        sub.account_id
    FROM webhook_events
    JOIN subscriptions sub ON sub.subscription_id = (payload->>'subscription')::text
    WHERE payload->>'subscription' IS NOT NULL
) s
WHERE we.payload->>'subscription' = s.subscription_id
  AND we.account_id IS NULL;

-- Step 7: For payment events, backfill using customer_id
UPDATE webhook_events we
SET account_id = a.id
FROM (
    SELECT DISTINCT
        (payload->>'customer')::text as customer_id,
        acc.id
    FROM webhook_events
    JOIN accounts acc ON acc.stripe_customer_id = (payload->>'customer')::text
    WHERE payload->>'customer' IS NOT NULL
) a
WHERE we.payload->>'customer' = a.customer_id
  AND we.account_id IS NULL;

-- Step 8: Add audit log entry for this security fix
INSERT INTO audit_logs (
    action,
    resource,
    resource_id,
    details,
    account_id,
    created_at
) VALUES (
    'security_fix',
    'webhook_events',
    NULL,
    jsonb_build_object(
        'migration', '001_fix_webhook_tenant_isolation',
        'issue', 'Added tenant isolation to webhook_events table',
        'severity', 'HIGH'
    ),
    NULL,  -- System action
    NOW()
);

-- Step 9: Add RLS policy for admins to manage all webhook events
CREATE POLICY "Admins can manage all webhook events" ON webhook_events
    FOR ALL USING (is_admin())
    WITH CHECK (is_admin());

-- Step 10: Document the security fix
COMMENT ON COLUMN webhook_events.account_id IS
'Account ID for tenant isolation. Required for all new webhook events to ensure proper data segregation.';
