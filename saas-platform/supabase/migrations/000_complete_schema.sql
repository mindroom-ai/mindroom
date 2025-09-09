-- MindRoom SaaS Platform - Fixed Consolidated Schema
-- This migration creates a properly linked schema where accounts.id = auth.users.id
-- ensuring perfect synchronization between authentication and application data
--
-- IMPORTANT: Service role keys automatically bypass RLS - no policies needed for them!

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
    created_at TIMESTAMPTZ DEFAULT NOW(),
    updated_at TIMESTAMPTZ DEFAULT NOW()
);

CREATE INDEX idx_accounts_email ON accounts(email);
CREATE INDEX idx_accounts_stripe_customer_id ON accounts(stripe_customer_id);
CREATE INDEX idx_accounts_is_admin ON accounts(is_admin) WHERE is_admin = TRUE;
CREATE INDEX idx_accounts_status ON accounts(status);
CREATE INDEX idx_accounts_tier ON accounts(tier);

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
-- PAYMENTS TABLE
-- ============================================================================
CREATE TABLE payments (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
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

-- ============================================================================
-- WEBHOOK EVENTS TABLE
-- ============================================================================
CREATE TABLE webhook_events (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    stripe_event_id TEXT UNIQUE NOT NULL,
    event_type TEXT NOT NULL,
    payload JSONB NOT NULL,
    processed_at TIMESTAMPTZ,
    error TEXT,
    created_at TIMESTAMPTZ DEFAULT NOW()
);

CREATE INDEX idx_webhook_events_stripe_event_id ON webhook_events(stripe_event_id);
CREATE INDEX idx_webhook_events_processed_at ON webhook_events(processed_at);

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
-- UPDATE TRIGGERS
-- ============================================================================
CREATE OR REPLACE FUNCTION update_updated_at_column()
RETURNS TRIGGER AS $$
BEGIN
    NEW.updated_at = NOW();
    RETURN NEW;
END;
$$ language 'plpgsql';

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

DROP TRIGGER IF EXISTS trg_set_subdomain_from_instance_id ON instances;
CREATE TRIGGER trg_set_subdomain_from_instance_id
BEFORE INSERT ON instances
FOR EACH ROW EXECUTE PROCEDURE set_subdomain_from_instance_id();

-- ============================================================================
-- AUTOMATIC ACCOUNT CREATION ON SIGNUP
-- ============================================================================
-- This function automatically creates an account record when a new user signs up
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
$$ language 'plpgsql' SECURITY DEFINER;

-- Trigger that fires when a new user is created in auth.users
CREATE TRIGGER on_auth_user_created
    AFTER INSERT ON auth.users
    FOR EACH ROW EXECUTE FUNCTION handle_new_user();

-- ============================================================================
-- ADMIN CHECK FUNCTION (to avoid recursion in RLS policies)
-- ============================================================================
CREATE OR REPLACE FUNCTION is_admin()
RETURNS boolean AS $$
BEGIN
    -- Use SECURITY DEFINER to bypass RLS when checking admin status
    RETURN EXISTS (
        SELECT 1 FROM accounts
        WHERE id = auth.uid()
        AND is_admin = TRUE
    );
END;
$$ LANGUAGE plpgsql SECURITY DEFINER;

-- ============================================================================
-- ROW LEVEL SECURITY POLICIES
-- ============================================================================
-- IMPORTANT: Service role keys automatically bypass RLS - DO NOT create policies for them!
-- The service_role key uses BYPASSRLS attribute and will skip all policies.

-- Enable RLS on all tables
ALTER TABLE accounts ENABLE ROW LEVEL SECURITY;
ALTER TABLE subscriptions ENABLE ROW LEVEL SECURITY;
ALTER TABLE instances ENABLE ROW LEVEL SECURITY;
ALTER TABLE usage_metrics ENABLE ROW LEVEL SECURITY;
ALTER TABLE payments ENABLE ROW LEVEL SECURITY;
ALTER TABLE webhook_events ENABLE ROW LEVEL SECURITY;
ALTER TABLE audit_logs ENABLE ROW LEVEL SECURITY;

-- ============================================================================
-- USER POLICIES
-- ============================================================================

-- Accounts table policies
CREATE POLICY "Users can view own account" ON accounts
    FOR SELECT USING (auth.uid() = id OR is_admin());

CREATE POLICY "Users can update own account" ON accounts
    FOR UPDATE USING (auth.uid() = id)
    WITH CHECK (auth.uid() = id AND NOT is_admin); -- Prevent users from making themselves admin

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

-- Usage metrics table policies
CREATE POLICY "Users can view own usage" ON usage_metrics
    FOR SELECT USING (
        subscription_id IN (SELECT id FROM subscriptions WHERE account_id = auth.uid()) OR
        is_admin()
    );

-- Payments table policies
CREATE POLICY "Users can view own payments" ON payments
    FOR SELECT USING (
        customer_id IN (SELECT stripe_customer_id FROM accounts WHERE id = auth.uid()) OR
        is_admin()
    );

-- Audit logs - only admins can view
CREATE POLICY "Only admins can view audit logs" ON audit_logs
    FOR SELECT USING (is_admin());

-- Webhook events - only service role can access (no policy needed, service role bypasses RLS)

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
    FOR INSERT WITH CHECK (is_admin());

CREATE POLICY "Admins can delete subscriptions" ON subscriptions
    FOR DELETE USING (is_admin());

-- Admins can manage instances
CREATE POLICY "Admins can update instances" ON instances
    FOR UPDATE USING (is_admin())
    WITH CHECK (is_admin());

CREATE POLICY "Admins can insert instances" ON instances
    FOR INSERT WITH CHECK (is_admin());

CREATE POLICY "Admins can delete instances" ON instances
    FOR DELETE USING (is_admin());

-- ============================================================================
-- COMMENTS FOR DOCUMENTATION
-- ============================================================================
COMMENT ON TABLE accounts IS 'Customer accounts linked directly to auth.users by ID';
COMMENT ON TABLE subscriptions IS 'Subscription records linked to accounts and Stripe billing';
COMMENT ON TABLE instances IS 'MindRoom K8s instance deployments';
COMMENT ON TABLE usage_metrics IS 'Daily usage metrics for billing and monitoring';
COMMENT ON TABLE payments IS 'Payment records from Stripe';
COMMENT ON TABLE webhook_events IS 'Stripe webhook event processing';
COMMENT ON TABLE audit_logs IS 'Audit trail for significant events';

COMMENT ON COLUMN accounts.id IS 'Same as auth.users.id for perfect linking';
COMMENT ON COLUMN accounts.tier IS 'Account tier - determines features and limits';
COMMENT ON COLUMN instances.instance_id IS 'Unique numeric identifier for the Kubernetes instance (e.g., 1, 2)';
COMMENT ON COLUMN instances.config IS 'JSON configuration for the instance';
COMMENT ON COLUMN accounts.is_admin IS 'Whether this account has admin privileges for the platform';

-- ============================================================================
-- GRANT PERMISSIONS TO SUPABASE ROLES
-- ============================================================================
-- CRITICAL: This must be done for tables created via SQL editor!

-- Grant all permissions to service_role
GRANT ALL ON ALL TABLES IN SCHEMA public TO service_role;
GRANT ALL ON ALL SEQUENCES IN SCHEMA public TO service_role;
GRANT ALL ON ALL FUNCTIONS IN SCHEMA public TO service_role;

-- Grant appropriate permissions to anon role (for RLS to work)
GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA public TO anon;
GRANT USAGE ON ALL SEQUENCES IN SCHEMA public TO anon;

-- Grant permissions to authenticated role
GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA public TO authenticated;
GRANT USAGE ON ALL SEQUENCES IN SCHEMA public TO authenticated;

-- Make sure future tables also get permissions
ALTER DEFAULT PRIVILEGES IN SCHEMA public GRANT ALL ON TABLES TO service_role;
ALTER DEFAULT PRIVILEGES IN SCHEMA public GRANT ALL ON SEQUENCES TO service_role;
ALTER DEFAULT PRIVILEGES IN SCHEMA public GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES TO anon;
ALTER DEFAULT PRIVILEGES IN SCHEMA public GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES TO authenticated;

-- ============================================================================
-- INITIAL DATA SETUP
-- ============================================================================
-- After running this migration, manually update your user to be admin:
-- UPDATE accounts SET is_admin = TRUE WHERE email = 'basnijholt@gmail.com';
