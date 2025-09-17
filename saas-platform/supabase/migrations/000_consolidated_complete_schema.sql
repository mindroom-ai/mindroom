-- MindRoom SaaS Platform - Complete Consolidated Schema
-- This single migration file contains all schema definitions and security fixes
-- Use this for fresh installations or complete resets
--
-- IMPORTANT: Service role keys automatically bypass RLS - no policies needed for them!
-- Date: 2025-09-16
-- Consolidates: 000_complete_schema + all security fixes + soft delete

-- ============================================================================
-- EXTENSIONS
-- ============================================================================
CREATE EXTENSION IF NOT EXISTS "uuid-ossp";

-- Global sequence for numeric instance IDs
DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_class WHERE relkind = 'S' AND relname = 'instance_id_seq'
    ) THEN
        CREATE SEQUENCE instance_id_seq START 1;
    END IF;
END$$;

-- ============================================================================
-- ACCOUNTS TABLE (Linked to auth.users)
-- ============================================================================
-- The accounts.id is the SAME as auth.users.id for perfect linking
CREATE TABLE accounts (
    id UUID PRIMARY KEY REFERENCES auth.users(id) ON DELETE CASCADE,
    email TEXT UNIQUE NOT NULL,
    full_name TEXT,
    company_name TEXT,
    stripe_customer_id TEXT UNIQUE,
    tier TEXT DEFAULT 'free' CHECK (tier IN ('free', 'starter', 'professional', 'enterprise')),
    is_admin BOOLEAN DEFAULT FALSE,
    status TEXT DEFAULT 'active', -- active, suspended, deleted, pending_verification

    -- Soft delete support (GDPR compliance)
    deleted_at TIMESTAMPTZ NULL,
    deletion_reason TEXT NULL,
    deletion_requested_by UUID NULL,
    deletion_requested_at TIMESTAMPTZ NULL,

    -- Consent tracking (GDPR)
    consent_marketing BOOLEAN DEFAULT FALSE,
    consent_analytics BOOLEAN DEFAULT FALSE,
    consent_updated_at TIMESTAMPTZ NULL,

    created_at TIMESTAMPTZ DEFAULT NOW(),
    updated_at TIMESTAMPTZ DEFAULT NOW()
);

CREATE INDEX idx_accounts_email ON accounts(email);
CREATE INDEX idx_accounts_stripe_customer_id ON accounts(stripe_customer_id);
CREATE INDEX idx_accounts_is_admin ON accounts(is_admin) WHERE is_admin = TRUE;
CREATE INDEX idx_accounts_status ON accounts(status);
CREATE INDEX idx_accounts_tier ON accounts(tier);
CREATE INDEX idx_accounts_deleted_at ON accounts(deleted_at);

-- ============================================================================
-- SUBSCRIPTIONS TABLE
-- ============================================================================
CREATE TABLE subscriptions (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    account_id UUID NOT NULL REFERENCES accounts(id) ON DELETE CASCADE,
    subscription_id TEXT UNIQUE, -- External subscription ID (e.g., from Stripe)
    stripe_subscription_id TEXT UNIQUE,
    stripe_price_id TEXT,
    tier TEXT NOT NULL DEFAULT 'free' CHECK (tier IN ('free', 'starter', 'professional', 'enterprise')),
    status TEXT NOT NULL DEFAULT 'trialing' CHECK (status IN ('trialing', 'active', 'cancelled', 'past_due', 'paused')),

    -- Limits based on tier
    max_agents INTEGER DEFAULT 1,
    max_messages_per_day INTEGER DEFAULT 100,

    -- Billing periods
    trial_ends_at TIMESTAMPTZ,
    current_period_start TIMESTAMPTZ,
    current_period_end TIMESTAMPTZ,
    cancelled_at TIMESTAMPTZ,

    created_at TIMESTAMPTZ DEFAULT NOW(),
    updated_at TIMESTAMPTZ DEFAULT NOW()
);

CREATE INDEX idx_subscriptions_account_id ON subscriptions(account_id);
CREATE INDEX idx_subscriptions_status ON subscriptions(status);
CREATE INDEX idx_subscriptions_subscription_id ON subscriptions(subscription_id);
CREATE INDEX idx_subscriptions_stripe_subscription_id ON subscriptions(stripe_subscription_id);

-- ============================================================================
-- INSTANCES TABLE
-- ============================================================================
CREATE TABLE instances (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    account_id UUID REFERENCES accounts(id) ON DELETE CASCADE,
    subscription_id UUID NOT NULL REFERENCES subscriptions(id) ON DELETE CASCADE,

    -- Instance identification
    instance_id INTEGER UNIQUE NOT NULL DEFAULT nextval('instance_id_seq'), -- Numeric K8s instance id
    subdomain TEXT UNIQUE NOT NULL,   -- Customer subdomain (defaults to instance_id as text)
    name TEXT, -- Display name for the instance

    -- Instance details
    status TEXT NOT NULL DEFAULT 'provisioning' CHECK (status IN ('provisioning', 'running', 'stopped', 'error', 'deprovisioned', 'restarting')),
    tier TEXT DEFAULT 'free', -- Copy of subscription tier for quick access

    -- URLs
    instance_url TEXT, -- Main instance URL
    frontend_url TEXT,
    backend_url TEXT,
    api_url TEXT,
    matrix_url TEXT, -- Synapse Matrix server URL
    matrix_server_url TEXT, -- Alias for compatibility

    -- Resource limits
    memory_limit_mb INTEGER DEFAULT 512,
    cpu_limit DECIMAL(3,2) DEFAULT 0.5,
    agent_count INTEGER DEFAULT 0,

    -- Configuration
    config JSONB DEFAULT '{}'::jsonb,

    -- Kubernetes sync tracking
    kubernetes_synced_at TIMESTAMPTZ,

    -- Lifecycle timestamps
    deprovisioned_at TIMESTAMPTZ,
    created_at TIMESTAMPTZ DEFAULT NOW(),
    updated_at TIMESTAMPTZ DEFAULT NOW()
);

CREATE INDEX idx_instances_account_id ON instances(account_id);
CREATE INDEX idx_instances_subscription_id ON instances(subscription_id);
CREATE INDEX idx_instances_status ON instances(status);
CREATE INDEX idx_instances_subdomain ON instances(subdomain);
CREATE INDEX idx_instances_instance_id ON instances(instance_id);
CREATE INDEX idx_instances_kubernetes_synced_at ON instances(kubernetes_synced_at);

-- ============================================================================
-- USAGE METRICS TABLE
-- ============================================================================
CREATE TABLE usage_metrics (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    subscription_id UUID NOT NULL REFERENCES subscriptions(id) ON DELETE CASCADE,
    metric_date DATE NOT NULL,

    -- Basic metrics
    messages_sent INTEGER DEFAULT 0,
    agents_used INTEGER DEFAULT 0,
    storage_used_gb DECIMAL(10,2) DEFAULT 0,

    created_at TIMESTAMPTZ DEFAULT NOW(),

    UNIQUE(subscription_id, metric_date)
);

CREATE INDEX idx_usage_metrics_subscription_date ON usage_metrics(subscription_id, metric_date DESC);

-- ============================================================================
-- PAYMENTS TABLE (with tenant isolation)
-- ============================================================================
CREATE TABLE payments (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    account_id UUID REFERENCES accounts(id), -- For tenant isolation
    invoice_id TEXT UNIQUE,
    subscription_id TEXT,
    customer_id TEXT,
    amount DECIMAL(10,2),
    currency TEXT DEFAULT 'USD',
    status TEXT,
    created_at TIMESTAMPTZ DEFAULT NOW()
);

CREATE INDEX idx_payments_subscription_id ON payments(subscription_id);
CREATE INDEX idx_payments_customer_id ON payments(customer_id);
CREATE INDEX idx_payments_account_id ON payments(account_id);

-- ============================================================================
-- WEBHOOK EVENTS TABLE (with tenant isolation)
-- ============================================================================
CREATE TABLE webhook_events (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    account_id UUID REFERENCES accounts(id), -- For tenant isolation
    stripe_event_id TEXT UNIQUE NOT NULL,
    event_type TEXT NOT NULL,
    payload JSONB NOT NULL,
    processed_at TIMESTAMPTZ,
    error TEXT,
    created_at TIMESTAMPTZ DEFAULT NOW()
);

CREATE INDEX idx_webhook_events_stripe_event_id ON webhook_events(stripe_event_id);
CREATE INDEX idx_webhook_events_processed_at ON webhook_events(processed_at);
CREATE INDEX idx_webhook_events_account_id ON webhook_events(account_id);

-- ============================================================================
-- AUDIT LOGS TABLE
-- ============================================================================
CREATE TABLE audit_logs (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    account_id UUID REFERENCES accounts(id) ON DELETE SET NULL,
    action TEXT NOT NULL,
    resource_type TEXT,
    resource_id TEXT,
    details JSONB DEFAULT '{}'::jsonb,
    ip_address INET,
    success BOOLEAN DEFAULT true,
    error_message TEXT,
    created_at TIMESTAMPTZ DEFAULT NOW()
);

CREATE INDEX idx_audit_logs_account_id ON audit_logs(account_id);
CREATE INDEX idx_audit_logs_created_at ON audit_logs(created_at DESC);
CREATE INDEX idx_audit_logs_action ON audit_logs(action);

-- ============================================================================
-- USAGE TABLE (for tracking)
-- ============================================================================
CREATE TABLE usage (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    subscription_id UUID REFERENCES subscriptions(id) ON DELETE CASCADE,
    instance_id INTEGER,
    metric_type TEXT NOT NULL,
    value DECIMAL(20,2) NOT NULL,
    unit TEXT,
    metadata JSONB DEFAULT '{}'::jsonb,
    recorded_at TIMESTAMPTZ DEFAULT NOW(),
    created_at TIMESTAMPTZ DEFAULT NOW()
);

CREATE INDEX idx_usage_subscription_id ON usage(subscription_id);
CREATE INDEX idx_usage_instance_id ON usage(instance_id);
CREATE INDEX idx_usage_recorded_at ON usage(recorded_at DESC);

-- ============================================================================
-- UPDATE TRIGGERS
-- ============================================================================
CREATE OR REPLACE FUNCTION update_updated_at_column()
RETURNS TRIGGER AS $$
BEGIN
    NEW.updated_at = NOW();
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

CREATE TRIGGER update_accounts_updated_at BEFORE UPDATE ON accounts
    FOR EACH ROW EXECUTE FUNCTION update_updated_at_column();

CREATE TRIGGER update_subscriptions_updated_at BEFORE UPDATE ON subscriptions
    FOR EACH ROW EXECUTE FUNCTION update_updated_at_column();

CREATE TRIGGER update_instances_updated_at BEFORE UPDATE ON instances
    FOR EACH ROW EXECUTE FUNCTION update_updated_at_column();

-- Subdomain default trigger to mirror numeric instance_id
CREATE OR REPLACE FUNCTION set_subdomain_from_instance_id()
RETURNS TRIGGER AS $$
BEGIN
    IF NEW.subdomain IS NULL OR NEW.subdomain = '' THEN
        NEW.subdomain := NEW.instance_id::text;
    END IF;
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

CREATE TRIGGER trg_set_subdomain_from_instance_id
BEFORE INSERT ON instances
FOR EACH ROW EXECUTE FUNCTION set_subdomain_from_instance_id();

-- ============================================================================
-- HELPER FUNCTIONS
-- ============================================================================

-- Function to automatically create an account record when a new user signs up
CREATE OR REPLACE FUNCTION handle_new_user()
RETURNS TRIGGER AS $$
BEGIN
    INSERT INTO public.accounts (id, email, full_name, tier)
    VALUES (
        NEW.id,
        NEW.email,
        COALESCE(NEW.raw_user_meta_data->>'full_name', ''),
        'free'
    );
    RETURN NEW;
END;
$$ LANGUAGE plpgsql SECURITY DEFINER;

-- Trigger that fires when a new user is created in auth.users
CREATE TRIGGER on_auth_user_created
    AFTER INSERT ON auth.users
    FOR EACH ROW EXECUTE FUNCTION handle_new_user();

-- Function to check if current user is admin
CREATE OR REPLACE FUNCTION is_admin()
RETURNS BOOLEAN AS $$
BEGIN
    RETURN EXISTS (
        SELECT 1 FROM accounts
        WHERE id = auth.uid()
        AND is_admin = TRUE
        AND deleted_at IS NULL
    );
END;
$$ LANGUAGE plpgsql SECURITY DEFINER;

-- Function to get current account_id
CREATE OR REPLACE FUNCTION get_current_account_id()
RETURNS UUID AS $$
BEGIN
    RETURN auth.uid();
END;
$$ LANGUAGE plpgsql SECURITY DEFINER;

-- ============================================================================
-- SOFT DELETE FUNCTIONS (GDPR Compliance)
-- ============================================================================

-- Soft delete function for accounts
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

-- ============================================================================
-- ROW LEVEL SECURITY (RLS)
-- ============================================================================

-- Enable RLS on all tables
ALTER TABLE accounts ENABLE ROW LEVEL SECURITY;
ALTER TABLE subscriptions ENABLE ROW LEVEL SECURITY;
ALTER TABLE instances ENABLE ROW LEVEL SECURITY;
ALTER TABLE usage_metrics ENABLE ROW LEVEL SECURITY;
ALTER TABLE payments ENABLE ROW LEVEL SECURITY;
ALTER TABLE webhook_events ENABLE ROW LEVEL SECURITY;
ALTER TABLE audit_logs ENABLE ROW LEVEL SECURITY;
ALTER TABLE usage ENABLE ROW LEVEL SECURITY;

-- ============================================================================
-- RLS POLICIES
-- ============================================================================

-- Accounts table policies
CREATE POLICY "Users can view own active account" ON accounts
    FOR SELECT USING (auth.uid() = id AND deleted_at IS NULL);

CREATE POLICY "Users can update own account" ON accounts
    FOR UPDATE USING (auth.uid() = id AND deleted_at IS NULL)
    WITH CHECK (auth.uid() = id AND deleted_at IS NULL);

CREATE POLICY "Service role bypass" ON accounts
    FOR ALL USING (auth.jwt()->>'role' = 'service_role');

-- Subscriptions table policies
CREATE POLICY "Users can view own subscriptions" ON subscriptions
    FOR SELECT USING (account_id = auth.uid() OR is_admin());

-- Instances table policies
CREATE POLICY "Users can view own instances" ON instances
    FOR SELECT USING (
        account_id = auth.uid() OR
        subscription_id IN (SELECT id FROM subscriptions WHERE account_id = auth.uid()) OR
        is_admin()
    );

-- Usage metrics - users can view their own
CREATE POLICY "Users can view own usage" ON usage_metrics
    FOR SELECT USING (
        subscription_id IN (SELECT id FROM subscriptions WHERE account_id = auth.uid()) OR
        is_admin()
    );

-- Payments table policies (with account_id for better isolation)
CREATE POLICY "Users can view own payments" ON payments
    FOR SELECT USING (
        account_id = auth.uid() OR
        is_admin()
    );

-- Webhook events - users can view their own
CREATE POLICY "Users can view own webhook events" ON webhook_events
    FOR SELECT USING (
        account_id = auth.uid() OR
        is_admin()
    );

-- Audit logs - only admins can view
CREATE POLICY "Only admins can view audit logs" ON audit_logs
    FOR SELECT USING (is_admin());

-- Usage table - users can view their own
CREATE POLICY "Users can view own usage data" ON usage
    FOR SELECT USING (
        subscription_id IN (SELECT id FROM subscriptions WHERE account_id = auth.uid()) OR
        is_admin()
    );

-- ============================================================================
-- ADMIN POLICIES
-- ============================================================================

-- Admins can update all accounts
CREATE POLICY "Admins can update all accounts" ON accounts
    FOR UPDATE USING (is_admin())
    WITH CHECK (is_admin());

-- Admins can manage subscriptions
CREATE POLICY "Admins can update subscriptions" ON subscriptions
    FOR UPDATE USING (is_admin())
    WITH CHECK (is_admin());

CREATE POLICY "Admins can insert subscriptions" ON subscriptions
    FOR INSERT WITH CHECK (is_admin() OR auth.jwt()->>'role' = 'service_role');

CREATE POLICY "Admins can delete subscriptions" ON subscriptions
    FOR DELETE USING (is_admin());

-- Admins can manage instances
CREATE POLICY "Admins can update instances" ON instances
    FOR UPDATE USING (is_admin())
    WITH CHECK (is_admin());

CREATE POLICY "Admins can insert instances" ON instances
    FOR INSERT WITH CHECK (is_admin() OR auth.jwt()->>'role' = 'service_role');

CREATE POLICY "Admins can delete instances" ON instances
    FOR DELETE USING (is_admin());

-- Admins can manage all payments
CREATE POLICY "Admins can manage all payments" ON payments
    FOR ALL USING (is_admin())
    WITH CHECK (is_admin());

-- Admins can manage all webhook events
CREATE POLICY "Admins can manage all webhook events" ON webhook_events
    FOR ALL USING (is_admin())
    WITH CHECK (is_admin());

GRANT EXECUTE ON FUNCTION soft_delete_account TO authenticated, service_role;
GRANT EXECUTE ON FUNCTION restore_account TO service_role;
GRANT EXECUTE ON FUNCTION hard_delete_account TO service_role;

GRANT ALL ON TABLE accounts TO service_role;
GRANT ALL ON TABLE subscriptions TO service_role;
GRANT ALL ON TABLE instances TO service_role;
GRANT ALL ON TABLE usage_metrics TO service_role;
GRANT ALL ON TABLE payments TO service_role;
GRANT ALL ON TABLE webhook_events TO service_role;
GRANT ALL ON TABLE audit_logs TO service_role;
GRANT ALL ON TABLE usage TO service_role;

-- Grant sequence permissions (required for instance_id generation)
-- Only USAGE is required for nextval(), SELECT allows currval()
GRANT USAGE, SELECT ON SEQUENCE instance_id_seq TO authenticated;
GRANT USAGE ON SEQUENCE instance_id_seq TO anon;  -- anon doesn't need UPDATE
GRANT USAGE, SELECT, UPDATE ON SEQUENCE instance_id_seq TO service_role;  -- service role needs full access

-- Helper to run privileged SQL via service role (used by tooling scripts)
CREATE OR REPLACE FUNCTION exec_sql(query TEXT)
RETURNS VOID
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = public
AS $$
BEGIN
    EXECUTE query;
END;
$$;

REVOKE ALL ON FUNCTION exec_sql(TEXT) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION exec_sql(TEXT) TO service_role;

GRANT SELECT, INSERT, UPDATE ON TABLE accounts TO authenticated;
GRANT SELECT, INSERT, UPDATE ON TABLE subscriptions TO authenticated;
GRANT SELECT, INSERT, UPDATE ON TABLE instances TO authenticated;

GRANT SELECT ON TABLE accounts TO anon;
GRANT SELECT ON TABLE subscriptions TO anon;
GRANT SELECT ON TABLE instances TO anon;
GRANT SELECT ON TABLE usage_metrics TO anon;
GRANT SELECT ON TABLE payments TO anon;
GRANT SELECT ON TABLE webhook_events TO anon;

-- ============================================================================
-- COMMENTS
-- ============================================================================

COMMENT ON TABLE payments IS
'Stores payment records from Stripe. Tenant-isolated via account_id and RLS policies.';

COMMENT ON COLUMN payments.account_id IS
'Account ID for tenant isolation. Required for all new payment records to ensure financial data segregation.';

COMMENT ON TABLE webhook_events IS
'Stores Stripe webhook events. Service role can insert/update. Users can only view their own events via RLS.';

COMMENT ON COLUMN webhook_events.account_id IS
'Account ID for tenant isolation. Required for all new webhook events to ensure proper data segregation.';

-- ============================================================================
-- END OF SCHEMA
-- ============================================================================
