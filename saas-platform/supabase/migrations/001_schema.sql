-- Clean schema for MindRoom SaaS platform
-- This migration creates only the tables and fields that are actually used

-- Enable necessary extensions
CREATE EXTENSION IF NOT EXISTS "uuid-ossp";

-- ============================================================================
-- ACCOUNTS TABLE
-- ============================================================================
CREATE TABLE accounts (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    email TEXT UNIQUE NOT NULL,
    full_name TEXT,
    company_name TEXT,
    stripe_customer_id TEXT UNIQUE,
    created_at TIMESTAMPTZ DEFAULT NOW(),
    updated_at TIMESTAMPTZ DEFAULT NOW()
);

CREATE INDEX idx_accounts_email ON accounts(email);
CREATE INDEX idx_accounts_stripe_customer_id ON accounts(stripe_customer_id);

-- ============================================================================
-- SUBSCRIPTIONS TABLE
-- ============================================================================
CREATE TABLE subscriptions (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    account_id UUID NOT NULL REFERENCES accounts(id) ON DELETE CASCADE,
    stripe_subscription_id TEXT UNIQUE,
    stripe_price_id TEXT,
    tier TEXT NOT NULL DEFAULT 'free', -- Using TEXT instead of ENUM for simplicity
    status TEXT NOT NULL DEFAULT 'trialing',

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
CREATE INDEX idx_subscriptions_stripe_subscription_id ON subscriptions(stripe_subscription_id);

-- ============================================================================
-- INSTANCES TABLE (Simplified - only used fields)
-- ============================================================================
CREATE TABLE instances (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    subscription_id UUID NOT NULL REFERENCES subscriptions(id) ON DELETE CASCADE,

    -- Instance identification
    instance_id TEXT UNIQUE NOT NULL, -- K8s instance identifier (e.g., "sub1757")
    subdomain TEXT UNIQUE NOT NULL,   -- Customer subdomain

    -- Instance details
    status TEXT NOT NULL DEFAULT 'provisioning',
    frontend_url TEXT,
    backend_url TEXT,

    -- Authentication
    auth_token TEXT, -- Simple auth token for the instance

    -- Resource limits
    memory_limit_mb INTEGER DEFAULT 512,
    cpu_limit DECIMAL(3,2) DEFAULT 0.5,

    -- Lifecycle timestamps
    deprovisioned_at TIMESTAMPTZ,
    created_at TIMESTAMPTZ DEFAULT NOW(),
    updated_at TIMESTAMPTZ DEFAULT NOW()
);

CREATE INDEX idx_instances_subscription_id ON instances(subscription_id);
CREATE INDEX idx_instances_status ON instances(status);
CREATE INDEX idx_instances_subdomain ON instances(subdomain);
CREATE INDEX idx_instances_auth_token ON instances(auth_token);

-- ============================================================================
-- USAGE METRICS TABLE (Simplified)
-- ============================================================================
CREATE TABLE usage_metrics (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    subscription_id UUID NOT NULL REFERENCES subscriptions(id) ON DELETE CASCADE,
    date DATE NOT NULL,

    -- Basic metrics
    messages_sent INTEGER DEFAULT 0,
    agents_active INTEGER DEFAULT 0,
    api_calls INTEGER DEFAULT 0,

    created_at TIMESTAMPTZ DEFAULT NOW(),

    UNIQUE(subscription_id, date)
);

CREATE INDEX idx_usage_metrics_subscription_date ON usage_metrics(subscription_id, date DESC);

-- ============================================================================
-- WEBHOOK EVENTS TABLE (For Stripe webhook processing)
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
-- AUDIT LOGS TABLE (Simplified)
-- ============================================================================
CREATE TABLE audit_logs (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    account_id UUID REFERENCES accounts(id) ON DELETE SET NULL,
    action TEXT NOT NULL,
    details JSONB DEFAULT '{}'::jsonb,
    ip_address INET,
    success BOOLEAN DEFAULT true,
    error_message TEXT,
    created_at TIMESTAMPTZ DEFAULT NOW()
);

CREATE INDEX idx_audit_logs_account_id ON audit_logs(account_id);
CREATE INDEX idx_audit_logs_created_at ON audit_logs(created_at DESC);

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

-- ============================================================================
-- COMMENTS FOR DOCUMENTATION
-- ============================================================================
COMMENT ON TABLE accounts IS 'Customer accounts for the MindRoom SaaS platform';
COMMENT ON TABLE subscriptions IS 'Subscription records linked to Stripe billing';
COMMENT ON TABLE instances IS 'MindRoom K8s instance deployments';
COMMENT ON TABLE usage_metrics IS 'Daily usage metrics for billing and monitoring';
COMMENT ON TABLE webhook_events IS 'Stripe webhook event processing';
COMMENT ON TABLE audit_logs IS 'Audit trail for significant events';

COMMENT ON COLUMN instances.instance_id IS 'Unique identifier for the Kubernetes instance (e.g., sub1757)';
COMMENT ON COLUMN instances.auth_token IS 'Bearer token for authenticating with the instance';
