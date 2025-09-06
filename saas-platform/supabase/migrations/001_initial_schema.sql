-- Initial schema for MindRoom SaaS platform
-- This migration creates the core tables for managing customer accounts,
-- subscriptions, instances, and usage metrics

-- Enable necessary extensions
CREATE EXTENSION IF NOT EXISTS "uuid-ossp";

-- Accounts table for customer accounts
CREATE TABLE accounts (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    email TEXT UNIQUE NOT NULL,
    full_name TEXT,
    company_name TEXT,
    stripe_customer_id TEXT UNIQUE,
    created_at TIMESTAMPTZ DEFAULT NOW(),
    updated_at TIMESTAMPTZ DEFAULT NOW()
);

-- Create index for faster lookups
CREATE INDEX idx_accounts_email ON accounts(email);
CREATE INDEX idx_accounts_stripe_customer_id ON accounts(stripe_customer_id);

-- Subscription tiers enum
CREATE TYPE subscription_tier AS ENUM ('free', 'starter', 'professional', 'enterprise');
CREATE TYPE subscription_status AS ENUM ('trialing', 'active', 'cancelled', 'past_due', 'incomplete', 'paused');
CREATE TYPE instance_status AS ENUM ('provisioning', 'running', 'stopped', 'failed', 'deprovisioning', 'maintenance');

-- Subscriptions table
CREATE TABLE subscriptions (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    account_id UUID NOT NULL REFERENCES accounts(id) ON DELETE CASCADE,
    stripe_subscription_id TEXT UNIQUE,
    stripe_price_id TEXT,
    tier subscription_tier NOT NULL DEFAULT 'free',
    status subscription_status NOT NULL DEFAULT 'trialing',

    -- Limits based on tier
    max_agents INTEGER DEFAULT 1,
    max_messages_per_day INTEGER DEFAULT 100,
    max_storage_gb INTEGER DEFAULT 1,
    max_platforms INTEGER DEFAULT 1, -- Number of platforms they can bridge to
    max_team_members INTEGER DEFAULT 1, -- For shared workspaces

    -- Usage tracking (reset daily)
    current_messages_today INTEGER DEFAULT 0,
    current_storage_gb DECIMAL(10,3) DEFAULT 0,
    last_reset_at DATE DEFAULT CURRENT_DATE,

    -- Billing periods
    trial_ends_at TIMESTAMPTZ,
    current_period_start TIMESTAMPTZ,
    current_period_end TIMESTAMPTZ,
    cancelled_at TIMESTAMPTZ,

    -- Features flags
    features JSONB DEFAULT '{
        "custom_agents": false,
        "api_access": false,
        "priority_support": false,
        "advanced_memory": false,
        "voice_messages": false,
        "file_uploads": false,
        "team_collaboration": false
    }'::jsonb,

    created_at TIMESTAMPTZ DEFAULT NOW(),
    updated_at TIMESTAMPTZ DEFAULT NOW(),

    CONSTRAINT unique_active_subscription UNIQUE (account_id, status)
        DEFERRABLE INITIALLY DEFERRED
);

-- Create indexes for faster queries
CREATE INDEX idx_subscriptions_account_id ON subscriptions(account_id);
CREATE INDEX idx_subscriptions_status ON subscriptions(status);
CREATE INDEX idx_subscriptions_stripe_subscription_id ON subscriptions(stripe_subscription_id);

-- Instances table (MindRoom deployments)
CREATE TABLE instances (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    subscription_id UUID NOT NULL REFERENCES subscriptions(id) ON DELETE CASCADE,

    -- Dokku configuration
    dokku_app_name TEXT UNIQUE NOT NULL, -- e.g., "mindroom-customer-abc123"
    subdomain TEXT UNIQUE NOT NULL, -- e.g., "customer-abc123"

    -- Instance details
    status instance_status NOT NULL DEFAULT 'provisioning',
    backend_url TEXT,
    frontend_url TEXT,
    matrix_server_url TEXT,
    matrix_admin_token TEXT, -- For managing the Matrix server

    -- Configuration from MindRoom
    config JSONB DEFAULT '{
        "agents": {},
        "teams": {},
        "tools": {},
        "models": {},
        "rooms": []
    }'::jsonb,

    -- Environment variables (encrypted in production)
    environment_vars JSONB DEFAULT '{}'::jsonb,

    -- Resource limits (enforced by Dokku)
    memory_limit_mb INTEGER DEFAULT 512,
    cpu_limit DECIMAL(3,2) DEFAULT 0.5, -- 0.5 = half a CPU core
    disk_limit_gb INTEGER DEFAULT 5,

    -- Health tracking
    last_health_check TIMESTAMPTZ,
    health_status TEXT DEFAULT 'unknown',
    health_details JSONB DEFAULT '{}'::jsonb,
    error_message TEXT,
    uptime_percentage DECIMAL(5,2) DEFAULT 100.00,

    -- Lifecycle timestamps
    provisioned_at TIMESTAMPTZ,
    last_started_at TIMESTAMPTZ,
    last_stopped_at TIMESTAMPTZ,
    deprovisioned_at TIMESTAMPTZ,
    created_at TIMESTAMPTZ DEFAULT NOW(),
    updated_at TIMESTAMPTZ DEFAULT NOW()
);

-- Create indexes
CREATE INDEX idx_instances_subscription_id ON instances(subscription_id);
CREATE INDEX idx_instances_status ON instances(status);
CREATE INDEX idx_instances_subdomain ON instances(subdomain);

-- Usage metrics table (daily aggregation)
CREATE TABLE usage_metrics (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    instance_id UUID NOT NULL REFERENCES instances(id) ON DELETE CASCADE,
    date DATE NOT NULL,

    -- Message metrics
    messages_sent INTEGER DEFAULT 0,
    messages_received INTEGER DEFAULT 0,

    -- Agent metrics (JSON for flexibility)
    agents_used JSONB DEFAULT '{}'::jsonb, -- {"agent_name": count}

    -- Tool metrics
    tools_used JSONB DEFAULT '{}'::jsonb, -- {"tool_name": count}

    -- Platform metrics
    platforms_active JSONB DEFAULT '{}'::jsonb, -- {"slack": true, "discord": false}

    -- Performance metrics
    average_response_time_ms INTEGER,
    error_count INTEGER DEFAULT 0,

    -- Storage metrics
    storage_used_mb INTEGER DEFAULT 0,

    created_at TIMESTAMPTZ DEFAULT NOW(),

    UNIQUE(instance_id, date)
);

-- Create indexes
CREATE INDEX idx_usage_metrics_instance_id_date ON usage_metrics(instance_id, date DESC);

-- Audit log for important events
CREATE TABLE audit_logs (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    account_id UUID REFERENCES accounts(id) ON DELETE SET NULL,
    instance_id UUID REFERENCES instances(id) ON DELETE SET NULL,
    action TEXT NOT NULL,
    action_category TEXT, -- 'billing', 'instance', 'config', 'security', 'api'
    details JSONB DEFAULT '{}'::jsonb,
    ip_address INET,
    user_agent TEXT,
    success BOOLEAN DEFAULT true,
    error_message TEXT,
    created_at TIMESTAMPTZ DEFAULT NOW()
);

-- Create indexes
CREATE INDEX idx_audit_logs_account_id ON audit_logs(account_id);
CREATE INDEX idx_audit_logs_instance_id ON audit_logs(instance_id);
CREATE INDEX idx_audit_logs_action_category ON audit_logs(action_category);
CREATE INDEX idx_audit_logs_created_at ON audit_logs(created_at DESC);

-- Support tickets table (for priority support)
CREATE TABLE support_tickets (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    account_id UUID NOT NULL REFERENCES accounts(id) ON DELETE CASCADE,
    instance_id UUID REFERENCES instances(id) ON DELETE SET NULL,
    subject TEXT NOT NULL,
    description TEXT NOT NULL,
    priority TEXT DEFAULT 'normal' CHECK (priority IN ('low', 'normal', 'high', 'urgent')),
    status TEXT DEFAULT 'open' CHECK (status IN ('open', 'in_progress', 'resolved', 'closed')),
    assigned_to TEXT,
    resolved_at TIMESTAMPTZ,
    created_at TIMESTAMPTZ DEFAULT NOW(),
    updated_at TIMESTAMPTZ DEFAULT NOW()
);

-- Create indexes
CREATE INDEX idx_support_tickets_account_id ON support_tickets(account_id);
CREATE INDEX idx_support_tickets_status ON support_tickets(status);

-- Instance backups table
CREATE TABLE instance_backups (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    instance_id UUID NOT NULL REFERENCES instances(id) ON DELETE CASCADE,
    backup_type TEXT NOT NULL CHECK (backup_type IN ('manual', 'automatic', 'pre_upgrade')),
    backup_location TEXT NOT NULL, -- S3 URL or file path
    size_mb INTEGER,
    status TEXT DEFAULT 'pending' CHECK (status IN ('pending', 'in_progress', 'completed', 'failed')),
    error_message TEXT,
    retention_days INTEGER DEFAULT 30,
    expires_at TIMESTAMPTZ,
    created_at TIMESTAMPTZ DEFAULT NOW()
);

-- Create indexes
CREATE INDEX idx_instance_backups_instance_id ON instance_backups(instance_id);
CREATE INDEX idx_instance_backups_status ON instance_backups(status);

-- API keys table
CREATE TABLE api_keys (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    account_id UUID NOT NULL REFERENCES accounts(id) ON DELETE CASCADE,
    name TEXT NOT NULL,
    key_hash TEXT NOT NULL UNIQUE, -- Store hashed version only
    key_prefix TEXT NOT NULL, -- First 8 chars for identification
    permissions JSONB DEFAULT '["read"]'::jsonb,
    last_used_at TIMESTAMPTZ,
    expires_at TIMESTAMPTZ,
    is_active BOOLEAN DEFAULT true,
    created_at TIMESTAMPTZ DEFAULT NOW(),
    updated_at TIMESTAMPTZ DEFAULT NOW()
);

-- Create indexes
CREATE INDEX idx_api_keys_account_id ON api_keys(account_id);
CREATE INDEX idx_api_keys_key_prefix ON api_keys(key_prefix);

-- Update timestamp triggers
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

CREATE TRIGGER update_support_tickets_updated_at BEFORE UPDATE ON support_tickets
    FOR EACH ROW EXECUTE FUNCTION update_updated_at_column();

CREATE TRIGGER update_api_keys_updated_at BEFORE UPDATE ON api_keys
    FOR EACH ROW EXECUTE FUNCTION update_updated_at_column();

-- Comments for documentation
COMMENT ON TABLE accounts IS 'Customer accounts for the MindRoom SaaS platform';
COMMENT ON TABLE subscriptions IS 'Subscription records linked to Stripe billing';
COMMENT ON TABLE instances IS 'MindRoom instance deployments managed by Dokku';
COMMENT ON TABLE usage_metrics IS 'Daily usage metrics for billing and monitoring';
COMMENT ON TABLE audit_logs IS 'Audit trail for all significant events';
COMMENT ON TABLE support_tickets IS 'Customer support ticket tracking';
COMMENT ON TABLE instance_backups IS 'Backup records for instance data';
COMMENT ON TABLE api_keys IS 'API keys for programmatic access';
