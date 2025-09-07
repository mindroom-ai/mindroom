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
-- Row Level Security (RLS) policies for MindRoom SaaS platform
-- Ensures users can only access their own data

-- Enable RLS on all tables
ALTER TABLE accounts ENABLE ROW LEVEL SECURITY;
ALTER TABLE subscriptions ENABLE ROW LEVEL SECURITY;
ALTER TABLE instances ENABLE ROW LEVEL SECURITY;
ALTER TABLE usage_metrics ENABLE ROW LEVEL SECURITY;
ALTER TABLE audit_logs ENABLE ROW LEVEL SECURITY;
ALTER TABLE support_tickets ENABLE ROW LEVEL SECURITY;
ALTER TABLE instance_backups ENABLE ROW LEVEL SECURITY;
ALTER TABLE api_keys ENABLE ROW LEVEL SECURITY;

-- Helper function to get current user id (using public schema instead of auth schema)
CREATE OR REPLACE FUNCTION public.current_user_id()
RETURNS UUID AS $$
BEGIN
    -- Use auth.uid() which is provided by Supabase
    RETURN auth.uid();
END;
$$ LANGUAGE plpgsql STABLE SECURITY DEFINER;

-- Accounts policies
CREATE POLICY "Users can view own account" ON accounts
    FOR SELECT USING (id = auth.uid());

CREATE POLICY "Users can update own account" ON accounts
    FOR UPDATE USING (id = auth.uid())
    WITH CHECK (id = auth.uid());

CREATE POLICY "Service role can manage all accounts" ON accounts
    FOR ALL USING (auth.jwt()->>'role' = 'service_role');

-- Subscriptions policies
CREATE POLICY "Users can view own subscriptions" ON subscriptions
    FOR SELECT USING (account_id = auth.uid());

CREATE POLICY "Users cannot directly modify subscriptions" ON subscriptions
    FOR UPDATE USING (false);

CREATE POLICY "Users cannot directly insert subscriptions" ON subscriptions
    FOR INSERT WITH CHECK (false);

CREATE POLICY "Users cannot directly delete subscriptions" ON subscriptions
    FOR DELETE USING (false);

CREATE POLICY "Service role can manage all subscriptions" ON subscriptions
    FOR ALL USING (auth.jwt()->>'role' = 'service_role');

-- Instances policies
CREATE POLICY "Users can view own instances" ON instances
    FOR SELECT USING (
        subscription_id IN (
            SELECT id FROM subscriptions WHERE account_id = auth.uid()
        )
    );

CREATE POLICY "Users can update own instance config" ON instances
    FOR UPDATE USING (
        subscription_id IN (
            SELECT id FROM subscriptions WHERE account_id = auth.uid()
        )
    )
    WITH CHECK (
        subscription_id IN (
            SELECT id FROM subscriptions WHERE account_id = auth.uid()
        )
    );

CREATE POLICY "Users cannot directly insert instances" ON instances
    FOR INSERT WITH CHECK (false);

CREATE POLICY "Users cannot directly delete instances" ON instances
    FOR DELETE USING (false);

CREATE POLICY "Service role can manage all instances" ON instances
    FOR ALL USING (auth.jwt()->>'role' = 'service_role');

-- Usage metrics policies
CREATE POLICY "Users can view own usage" ON usage_metrics
    FOR SELECT USING (
        instance_id IN (
            SELECT i.id FROM instances i
            JOIN subscriptions s ON i.subscription_id = s.id
            WHERE s.account_id = auth.uid()
        )
    );

CREATE POLICY "Users cannot modify usage metrics" ON usage_metrics
    FOR UPDATE USING (false);

CREATE POLICY "Users cannot insert usage metrics" ON usage_metrics
    FOR INSERT WITH CHECK (false);

CREATE POLICY "Service role can manage all usage metrics" ON usage_metrics
    FOR ALL USING (auth.jwt()->>'role' = 'service_role');

-- Audit logs policies
CREATE POLICY "Users can view own audit logs" ON audit_logs
    FOR SELECT USING (account_id = auth.uid());

CREATE POLICY "Users cannot modify audit logs" ON audit_logs
    FOR UPDATE USING (false);

CREATE POLICY "Users cannot insert audit logs" ON audit_logs
    FOR INSERT WITH CHECK (false);

CREATE POLICY "Users cannot delete audit logs" ON audit_logs
    FOR DELETE USING (false);

CREATE POLICY "Service role can manage all audit logs" ON audit_logs
    FOR ALL USING (auth.jwt()->>'role' = 'service_role');

-- Support tickets policies
CREATE POLICY "Users can view own tickets" ON support_tickets
    FOR SELECT USING (account_id = auth.uid());

CREATE POLICY "Users can create tickets" ON support_tickets
    FOR INSERT WITH CHECK (account_id = auth.uid());

CREATE POLICY "Users can update own tickets" ON support_tickets
    FOR UPDATE USING (account_id = auth.uid())
    WITH CHECK (account_id = auth.uid());

CREATE POLICY "Service role can manage all tickets" ON support_tickets
    FOR ALL USING (auth.jwt()->>'role' = 'service_role');

-- Instance backups policies
CREATE POLICY "Users can view own backups" ON instance_backups
    FOR SELECT USING (
        instance_id IN (
            SELECT i.id FROM instances i
            JOIN subscriptions s ON i.subscription_id = s.id
            WHERE s.account_id = auth.uid()
        )
    );

CREATE POLICY "Users can request backups for own instances" ON instance_backups
    FOR INSERT WITH CHECK (
        instance_id IN (
            SELECT i.id FROM instances i
            JOIN subscriptions s ON i.subscription_id = s.id
            WHERE s.account_id = auth.uid()
        )
    );

CREATE POLICY "Service role can manage all backups" ON instance_backups
    FOR ALL USING (auth.jwt()->>'role' = 'service_role');

-- API keys policies
CREATE POLICY "Users can view own API keys" ON api_keys
    FOR SELECT USING (account_id = auth.uid());

CREATE POLICY "Users can create own API keys" ON api_keys
    FOR INSERT WITH CHECK (account_id = auth.uid());

CREATE POLICY "Users can update own API keys" ON api_keys
    FOR UPDATE USING (account_id = auth.uid())
    WITH CHECK (account_id = auth.uid());

CREATE POLICY "Users can delete own API keys" ON api_keys
    FOR DELETE USING (account_id = auth.uid());

CREATE POLICY "Service role can manage all API keys" ON api_keys
    FOR ALL USING (auth.jwt()->>'role' = 'service_role');

-- Additional security functions (in public schema)

-- Function to check if user has active subscription
CREATE OR REPLACE FUNCTION public.has_active_subscription(user_id UUID)
RETURNS BOOLEAN AS $$
BEGIN
    RETURN EXISTS (
        SELECT 1 FROM subscriptions
        WHERE account_id = user_id
        AND status = 'active'
    );
END;
$$ LANGUAGE plpgsql SECURITY DEFINER;

-- Function to check if user has permission for specific action
CREATE OR REPLACE FUNCTION public.check_user_permission(user_id UUID, permission TEXT)
RETURNS BOOLEAN AS $$
DECLARE
    sub_tier subscription_tier;
    features JSONB;
BEGIN
    SELECT s.tier, s.features INTO sub_tier, features
    FROM subscriptions s
    WHERE s.account_id = user_id
    AND s.status = 'active'
    ORDER BY s.created_at DESC
    LIMIT 1;

    IF sub_tier IS NULL THEN
        RETURN false;
    END IF;

    -- Check feature flags
    IF features ? permission THEN
        RETURN (features->permission)::boolean;
    END IF;

    -- Default permissions based on tier
    CASE permission
        WHEN 'custom_agents' THEN
            RETURN sub_tier IN ('professional', 'enterprise');
        WHEN 'api_access' THEN
            RETURN sub_tier IN ('starter', 'professional', 'enterprise');
        WHEN 'priority_support' THEN
            RETURN sub_tier IN ('professional', 'enterprise');
        WHEN 'team_collaboration' THEN
            RETURN sub_tier = 'enterprise';
        ELSE
            RETURN false;
    END CASE;
END;
$$ LANGUAGE plpgsql SECURITY DEFINER;

-- Comments for documentation
COMMENT ON POLICY "Users can view own account" ON accounts IS 'Users can only see their own account information';
COMMENT ON POLICY "Users can view own subscriptions" ON subscriptions IS 'Users can view their subscription details but cannot modify them directly (handled by Stripe webhooks)';
COMMENT ON POLICY "Users can view own instances" ON instances IS 'Users can see instances associated with their subscriptions';
COMMENT ON POLICY "Users can view own usage" ON usage_metrics IS 'Users can view usage metrics for their instances';
COMMENT ON FUNCTION public.has_active_subscription IS 'Check if a user has an active subscription';
COMMENT ON FUNCTION public.check_user_permission IS 'Check if a user has permission for a specific feature based on their subscription';
-- Database functions for MindRoom SaaS platform
-- Provides utility functions for common operations

-- Function to get user's active instance
CREATE OR REPLACE FUNCTION get_user_instance(user_id UUID)
RETURNS TABLE (
    instance_id UUID,
    subdomain TEXT,
    frontend_url TEXT,
    backend_url TEXT,
    matrix_server_url TEXT,
    status instance_status,
    tier subscription_tier,
    config JSONB,
    features JSONB
) AS $$
BEGIN
    RETURN QUERY
    SELECT
        i.id,
        i.subdomain,
        i.frontend_url,
        i.backend_url,
        i.matrix_server_url,
        i.status,
        s.tier,
        i.config,
        s.features
    FROM instances i
    JOIN subscriptions s ON i.subscription_id = s.id
    WHERE s.account_id = user_id
    AND s.status = 'active'
    AND i.status = 'running'
    ORDER BY i.created_at DESC
    LIMIT 1;
END;
$$ LANGUAGE plpgsql SECURITY DEFINER;

-- Function to get all user instances (including inactive)
CREATE OR REPLACE FUNCTION get_all_user_instances(user_id UUID)
RETURNS TABLE (
    instance_id UUID,
    subdomain TEXT,
    status instance_status,
    subscription_status subscription_status,
    tier subscription_tier,
    created_at TIMESTAMPTZ
) AS $$
BEGIN
    RETURN QUERY
    SELECT
        i.id,
        i.subdomain,
        i.status,
        s.status,
        s.tier,
        i.created_at
    FROM instances i
    JOIN subscriptions s ON i.subscription_id = s.id
    WHERE s.account_id = user_id
    ORDER BY i.created_at DESC;
END;
$$ LANGUAGE plpgsql SECURITY DEFINER;

-- Function to track daily usage
CREATE OR REPLACE FUNCTION track_usage(
    p_instance_id UUID,
    p_agent_name TEXT DEFAULT NULL,
    p_tool_name TEXT DEFAULT NULL,
    p_platform TEXT DEFAULT NULL,
    p_message_type TEXT DEFAULT 'sent' -- 'sent' or 'received'
) RETURNS void AS $$
DECLARE
    v_subscription_id UUID;
    v_max_messages INTEGER;
    v_current_messages INTEGER;
BEGIN
    -- Get subscription info
    SELECT i.subscription_id INTO v_subscription_id
    FROM instances i
    WHERE i.id = p_instance_id;

    IF v_subscription_id IS NULL THEN
        RAISE EXCEPTION 'Instance not found: %', p_instance_id;
    END IF;

    -- Insert or update today's metrics
    INSERT INTO usage_metrics (
        instance_id,
        date,
        messages_sent,
        messages_received,
        agents_used,
        tools_used,
        platforms_active
    )
    VALUES (
        p_instance_id,
        CURRENT_DATE,
        CASE WHEN p_message_type = 'sent' THEN 1 ELSE 0 END,
        CASE WHEN p_message_type = 'received' THEN 1 ELSE 0 END,
        CASE WHEN p_agent_name IS NOT NULL
             THEN jsonb_build_object(p_agent_name, 1)
             ELSE '{}'::jsonb
        END,
        CASE WHEN p_tool_name IS NOT NULL
             THEN jsonb_build_object(p_tool_name, 1)
             ELSE '{}'::jsonb
        END,
        CASE WHEN p_platform IS NOT NULL
             THEN jsonb_build_object(p_platform, true)
             ELSE '{}'::jsonb
        END
    )
    ON CONFLICT (instance_id, date) DO UPDATE
    SET
        messages_sent = usage_metrics.messages_sent +
            CASE WHEN p_message_type = 'sent' THEN 1 ELSE 0 END,
        messages_received = usage_metrics.messages_received +
            CASE WHEN p_message_type = 'received' THEN 1 ELSE 0 END,
        agents_used = CASE
            WHEN p_agent_name IS NOT NULL THEN
                usage_metrics.agents_used ||
                jsonb_build_object(p_agent_name,
                    COALESCE((usage_metrics.agents_used->>p_agent_name)::int, 0) + 1)
            ELSE usage_metrics.agents_used
        END,
        tools_used = CASE
            WHEN p_tool_name IS NOT NULL THEN
                usage_metrics.tools_used ||
                jsonb_build_object(p_tool_name,
                    COALESCE((usage_metrics.tools_used->>p_tool_name)::int, 0) + 1)
            ELSE usage_metrics.tools_used
        END,
        platforms_active = CASE
            WHEN p_platform IS NOT NULL THEN
                usage_metrics.platforms_active ||
                jsonb_build_object(p_platform, true)
            ELSE usage_metrics.platforms_active
        END;

    -- Reset daily counter if needed
    UPDATE subscriptions
    SET
        last_reset_at = CASE
            WHEN last_reset_at < CURRENT_DATE THEN CURRENT_DATE
            ELSE last_reset_at
        END,
        current_messages_today = CASE
            WHEN last_reset_at < CURRENT_DATE THEN 1
            ELSE current_messages_today + 1
        END
    WHERE id = v_subscription_id;

    -- Check if user has exceeded daily limit
    SELECT max_messages_per_day, current_messages_today
    INTO v_max_messages, v_current_messages
    FROM subscriptions
    WHERE id = v_subscription_id;

    IF v_current_messages > v_max_messages THEN
        -- Log the over-limit event
        INSERT INTO audit_logs (
            account_id,
            instance_id,
            action,
            action_category,
            details
        )
        SELECT
            s.account_id,
            p_instance_id,
            'message_limit_exceeded',
            'usage',
            jsonb_build_object(
                'limit', v_max_messages,
                'current', v_current_messages,
                'date', CURRENT_DATE
            )
        FROM subscriptions s
        WHERE s.id = v_subscription_id;
    END IF;
END;
$$ LANGUAGE plpgsql SECURITY DEFINER;

-- Function to check usage limits
CREATE OR REPLACE FUNCTION check_usage_limits(p_instance_id UUID)
RETURNS TABLE (
    is_within_limits BOOLEAN,
    messages_remaining INTEGER,
    daily_limit INTEGER,
    storage_used_gb DECIMAL(10,3),
    storage_limit_gb INTEGER
) AS $$
BEGIN
    RETURN QUERY
    SELECT
        s.current_messages_today <= s.max_messages_per_day,
        GREATEST(0, s.max_messages_per_day - s.current_messages_today),
        s.max_messages_per_day,
        s.current_storage_gb,
        s.max_storage_gb
    FROM instances i
    JOIN subscriptions s ON i.subscription_id = s.id
    WHERE i.id = p_instance_id;
END;
$$ LANGUAGE plpgsql SECURITY DEFINER;

-- Function to provision a new instance
CREATE OR REPLACE FUNCTION provision_instance(
    p_subscription_id UUID,
    p_config JSONB DEFAULT NULL
) RETURNS UUID AS $$
DECLARE
    v_instance_id UUID;
    v_app_name TEXT;
    v_subdomain TEXT;
BEGIN
    -- Generate unique identifiers
    v_instance_id := gen_random_uuid();
    v_app_name := 'mindroom-' || substring(v_instance_id::text, 1, 8);
    v_subdomain := 'mr-' || substring(v_instance_id::text, 1, 8);

    -- Create instance record
    INSERT INTO instances (
        id,
        subscription_id,
        dokku_app_name,
        subdomain,
        status,
        config
    ) VALUES (
        v_instance_id,
        p_subscription_id,
        v_app_name,
        v_subdomain,
        'provisioning',
        COALESCE(p_config, '{
            "agents": {},
            "teams": {},
            "tools": {},
            "models": {},
            "rooms": []
        }'::jsonb)
    );

    -- Log the provisioning
    INSERT INTO audit_logs (
        account_id,
        instance_id,
        action,
        action_category,
        details
    )
    SELECT
        s.account_id,
        v_instance_id,
        'instance_provisioning_started',
        'instance',
        jsonb_build_object(
            'subscription_id', p_subscription_id,
            'app_name', v_app_name,
            'subdomain', v_subdomain
        )
    FROM subscriptions s
    WHERE s.id = p_subscription_id;

    RETURN v_instance_id;
END;
$$ LANGUAGE plpgsql SECURITY DEFINER;

-- Function to deprovision an instance
CREATE OR REPLACE FUNCTION deprovision_instance(
    p_instance_id UUID,
    p_reason TEXT DEFAULT NULL
) RETURNS void AS $$
BEGIN
    -- Update instance status
    UPDATE instances
    SET
        status = 'deprovisioning',
        updated_at = NOW()
    WHERE id = p_instance_id;

    -- Log the deprovisioning
    INSERT INTO audit_logs (
        account_id,
        instance_id,
        action,
        action_category,
        details
    )
    SELECT
        s.account_id,
        p_instance_id,
        'instance_deprovisioning_started',
        'instance',
        jsonb_build_object(
            'reason', COALESCE(p_reason, 'manual'),
            'timestamp', NOW()
        )
    FROM instances i
    JOIN subscriptions s ON i.subscription_id = s.id
    WHERE i.id = p_instance_id;
END;
$$ LANGUAGE plpgsql SECURITY DEFINER;

-- Function to update instance health
CREATE OR REPLACE FUNCTION update_instance_health(
    p_instance_id UUID,
    p_health_status TEXT,
    p_health_details JSONB DEFAULT NULL,
    p_error_message TEXT DEFAULT NULL
) RETURNS void AS $$
BEGIN
    UPDATE instances
    SET
        last_health_check = NOW(),
        health_status = p_health_status,
        health_details = COALESCE(p_health_details, '{}'::jsonb),
        error_message = p_error_message,
        updated_at = NOW()
    WHERE id = p_instance_id;

    -- If health is critical, log it
    IF p_health_status IN ('critical', 'failed') THEN
        INSERT INTO audit_logs (
            account_id,
            instance_id,
            action,
            action_category,
            details,
            success
        )
        SELECT
            s.account_id,
            p_instance_id,
            'instance_health_critical',
            'instance',
            jsonb_build_object(
                'status', p_health_status,
                'details', p_health_details,
                'error', p_error_message
            ),
            false
        FROM instances i
        JOIN subscriptions s ON i.subscription_id = s.id
        WHERE i.id = p_instance_id;
    END IF;
END;
$$ LANGUAGE plpgsql SECURITY DEFINER;

-- Function to get usage statistics for billing
CREATE OR REPLACE FUNCTION get_billing_metrics(
    p_account_id UUID,
    p_start_date DATE,
    p_end_date DATE
) RETURNS TABLE (
    total_messages INTEGER,
    total_storage_gb DECIMAL(10,3),
    unique_agents INTEGER,
    unique_tools INTEGER,
    active_days INTEGER,
    average_daily_messages DECIMAL(10,2)
) AS $$
BEGIN
    RETURN QUERY
    WITH metrics AS (
        SELECT
            SUM(um.messages_sent + um.messages_received) as total_messages,
            COUNT(DISTINCT um.date) as active_days,
            COUNT(DISTINCT jsonb_object_keys(um.agents_used)) as unique_agents,
            COUNT(DISTINCT jsonb_object_keys(um.tools_used)) as unique_tools
        FROM usage_metrics um
        JOIN instances i ON um.instance_id = i.id
        JOIN subscriptions s ON i.subscription_id = s.id
        WHERE s.account_id = p_account_id
        AND um.date BETWEEN p_start_date AND p_end_date
    ),
    storage AS (
        SELECT MAX(current_storage_gb) as max_storage
        FROM subscriptions
        WHERE account_id = p_account_id
    )
    SELECT
        COALESCE(m.total_messages, 0)::INTEGER,
        COALESCE(st.max_storage, 0.0),
        COALESCE(m.unique_agents, 0)::INTEGER,
        COALESCE(m.unique_tools, 0)::INTEGER,
        COALESCE(m.active_days, 0)::INTEGER,
        CASE
            WHEN m.active_days > 0 THEN
                ROUND(m.total_messages::DECIMAL / m.active_days, 2)
            ELSE 0.0
        END
    FROM metrics m, storage st;
END;
$$ LANGUAGE plpgsql SECURITY DEFINER;

-- Function to handle Stripe webhook events
CREATE OR REPLACE FUNCTION handle_stripe_event(
    p_event_type TEXT,
    p_event_data JSONB
) RETURNS void AS $$
DECLARE
    v_account_id UUID;
    v_subscription_id UUID;
BEGIN
    CASE p_event_type
        WHEN 'customer.created' THEN
            -- Create or update account
            INSERT INTO accounts (email, stripe_customer_id)
            VALUES (
                p_event_data->>'email',
                p_event_data->>'id'
            )
            ON CONFLICT (stripe_customer_id) DO UPDATE
            SET email = EXCLUDED.email,
                updated_at = NOW();

        WHEN 'customer.subscription.created' THEN
            -- Get account ID
            SELECT id INTO v_account_id
            FROM accounts
            WHERE stripe_customer_id = p_event_data->'customer'->>'id';

            -- Create subscription
            INSERT INTO subscriptions (
                account_id,
                stripe_subscription_id,
                stripe_price_id,
                status,
                current_period_start,
                current_period_end
            )
            VALUES (
                v_account_id,
                p_event_data->>'id',
                p_event_data->'items'->'data'->0->'price'->>'id',
                (p_event_data->>'status')::subscription_status,
                to_timestamp((p_event_data->>'current_period_start')::bigint),
                to_timestamp((p_event_data->>'current_period_end')::bigint)
            );

        WHEN 'customer.subscription.updated' THEN
            -- Update subscription
            UPDATE subscriptions
            SET
                status = (p_event_data->>'status')::subscription_status,
                current_period_start = to_timestamp((p_event_data->>'current_period_start')::bigint),
                current_period_end = to_timestamp((p_event_data->>'current_period_end')::bigint),
                updated_at = NOW()
            WHERE stripe_subscription_id = p_event_data->>'id';

        WHEN 'customer.subscription.deleted' THEN
            -- Cancel subscription
            UPDATE subscriptions
            SET
                status = 'cancelled',
                cancelled_at = NOW(),
                updated_at = NOW()
            WHERE stripe_subscription_id = p_event_data->>'id';

            -- Get instances to deprovision
            FOR v_subscription_id IN
                SELECT id FROM subscriptions
                WHERE stripe_subscription_id = p_event_data->>'id'
            LOOP
                PERFORM deprovision_instance(
                    i.id,
                    'subscription_cancelled'
                )
                FROM instances i
                WHERE i.subscription_id = v_subscription_id;
            END LOOP;
    END CASE;

    -- Log the event
    INSERT INTO audit_logs (
        account_id,
        action,
        action_category,
        details
    )
    VALUES (
        v_account_id,
        p_event_type,
        'billing',
        p_event_data
    );
END;
$$ LANGUAGE plpgsql SECURITY DEFINER;

-- Comments for documentation
COMMENT ON FUNCTION get_user_instance IS 'Get the active instance for a user';
COMMENT ON FUNCTION track_usage IS 'Track usage metrics for billing and monitoring';
COMMENT ON FUNCTION check_usage_limits IS 'Check if instance is within usage limits';
COMMENT ON FUNCTION provision_instance IS 'Provision a new MindRoom instance';
COMMENT ON FUNCTION deprovision_instance IS 'Deprovision an existing instance';
COMMENT ON FUNCTION update_instance_health IS 'Update instance health status';
COMMENT ON FUNCTION get_billing_metrics IS 'Get billing metrics for a date range';
COMMENT ON FUNCTION handle_stripe_event IS 'Handle incoming Stripe webhook events';
-- Triggers and automation for MindRoom SaaS platform
-- Handles automatic processes and data integrity

-- Function to automatically reset daily usage counters
CREATE OR REPLACE FUNCTION reset_daily_usage()
RETURNS void AS $$
BEGIN
    UPDATE subscriptions
    SET
        current_messages_today = 0,
        last_reset_at = CURRENT_DATE
    WHERE last_reset_at < CURRENT_DATE;
END;
$$ LANGUAGE plpgsql;

-- Function to calculate and update storage usage
CREATE OR REPLACE FUNCTION calculate_storage_usage(p_instance_id UUID)
RETURNS DECIMAL(10,3) AS $$
DECLARE
    v_total_storage DECIMAL(10,3);
BEGIN
    -- Calculate storage from latest metrics
    SELECT COALESCE(SUM(storage_used_mb) / 1024.0, 0.0)
    INTO v_total_storage
    FROM usage_metrics
    WHERE instance_id = p_instance_id
    AND date = CURRENT_DATE;

    -- Update subscription storage
    UPDATE subscriptions s
    SET current_storage_gb = v_total_storage
    FROM instances i
    WHERE i.subscription_id = s.id
    AND i.id = p_instance_id;

    RETURN v_total_storage;
END;
$$ LANGUAGE plpgsql;

-- Function to handle subscription tier changes
CREATE OR REPLACE FUNCTION handle_tier_change()
RETURNS TRIGGER AS $$
BEGIN
    IF OLD.tier != NEW.tier THEN
        -- Update limits based on new tier
        CASE NEW.tier
            WHEN 'free' THEN
                NEW.max_agents := 1;
                NEW.max_messages_per_day := 100;
                NEW.max_storage_gb := 1;
                NEW.max_platforms := 1;
                NEW.max_team_members := 1;
                NEW.features := '{
                    "custom_agents": false,
                    "api_access": false,
                    "priority_support": false,
                    "advanced_memory": false,
                    "voice_messages": false,
                    "file_uploads": false,
                    "team_collaboration": false
                }'::jsonb;

            WHEN 'starter' THEN
                NEW.max_agents := 3;
                NEW.max_messages_per_day := 1000;
                NEW.max_storage_gb := 5;
                NEW.max_platforms := 3;
                NEW.max_team_members := 1;
                NEW.features := '{
                    "custom_agents": false,
                    "api_access": true,
                    "priority_support": false,
                    "advanced_memory": true,
                    "voice_messages": true,
                    "file_uploads": true,
                    "team_collaboration": false
                }'::jsonb;

            WHEN 'professional' THEN
                NEW.max_agents := 10;
                NEW.max_messages_per_day := 10000;
                NEW.max_storage_gb := 50;
                NEW.max_platforms := 10;
                NEW.max_team_members := 5;
                NEW.features := '{
                    "custom_agents": true,
                    "api_access": true,
                    "priority_support": true,
                    "advanced_memory": true,
                    "voice_messages": true,
                    "file_uploads": true,
                    "team_collaboration": true
                }'::jsonb;

            WHEN 'enterprise' THEN
                NEW.max_agents := 999;
                NEW.max_messages_per_day := 999999;
                NEW.max_storage_gb := 999;
                NEW.max_platforms := 999;
                NEW.max_team_members := 999;
                NEW.features := '{
                    "custom_agents": true,
                    "api_access": true,
                    "priority_support": true,
                    "advanced_memory": true,
                    "voice_messages": true,
                    "file_uploads": true,
                    "team_collaboration": true
                }'::jsonb;
        END CASE;

        -- Log tier change
        INSERT INTO audit_logs (
            account_id,
            action,
            action_category,
            details
        )
        VALUES (
            NEW.account_id,
            'subscription_tier_changed',
            'billing',
            jsonb_build_object(
                'old_tier', OLD.tier,
                'new_tier', NEW.tier,
                'new_limits', jsonb_build_object(
                    'max_agents', NEW.max_agents,
                    'max_messages_per_day', NEW.max_messages_per_day,
                    'max_storage_gb', NEW.max_storage_gb
                )
            )
        );

        -- Update instance resource limits based on new tier
        UPDATE instances i
        SET
            memory_limit_mb = CASE NEW.tier
                WHEN 'free' THEN 512
                WHEN 'starter' THEN 1024
                WHEN 'professional' THEN 2048
                WHEN 'enterprise' THEN 4096
            END,
            cpu_limit = CASE NEW.tier
                WHEN 'free' THEN 0.5
                WHEN 'starter' THEN 1.0
                WHEN 'professional' THEN 2.0
                WHEN 'enterprise' THEN 4.0
            END,
            disk_limit_gb = NEW.max_storage_gb
        WHERE i.subscription_id = NEW.id;
    END IF;

    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

-- Trigger for tier changes
CREATE TRIGGER subscription_tier_change
    BEFORE UPDATE OF tier ON subscriptions
    FOR EACH ROW
    EXECUTE FUNCTION handle_tier_change();

-- Function to check and enforce usage limits
CREATE OR REPLACE FUNCTION enforce_usage_limits()
RETURNS TRIGGER AS $$
DECLARE
    v_max_messages INTEGER;
    v_current_messages INTEGER;
BEGIN
    -- Check if we need to reset the counter
    IF OLD.last_reset_at < CURRENT_DATE THEN
        NEW.current_messages_today := 1;
        NEW.last_reset_at := CURRENT_DATE;
    END IF;

    -- Check message limits
    IF NEW.current_messages_today > NEW.max_messages_per_day THEN
        -- Mark instances as rate-limited
        UPDATE instances
        SET
            health_status = 'rate_limited',
            health_details = jsonb_build_object(
                'reason', 'daily_message_limit_exceeded',
                'limit', NEW.max_messages_per_day,
                'current', NEW.current_messages_today
            )
        WHERE subscription_id = NEW.id
        AND status = 'running';

        -- Log rate limit event
        INSERT INTO audit_logs (
            account_id,
            action,
            action_category,
            details,
            success
        )
        VALUES (
            NEW.account_id,
            'rate_limit_exceeded',
            'usage',
            jsonb_build_object(
                'type', 'messages',
                'limit', NEW.max_messages_per_day,
                'current', NEW.current_messages_today
            ),
            false
        );
    END IF;

    -- Check storage limits
    IF NEW.current_storage_gb > NEW.max_storage_gb THEN
        UPDATE instances
        SET
            health_status = 'storage_exceeded',
            health_details = jsonb_build_object(
                'reason', 'storage_limit_exceeded',
                'limit_gb', NEW.max_storage_gb,
                'current_gb', NEW.current_storage_gb
            )
        WHERE subscription_id = NEW.id;

        INSERT INTO audit_logs (
            account_id,
            action,
            action_category,
            details,
            success
        )
        VALUES (
            NEW.account_id,
            'storage_limit_exceeded',
            'usage',
            jsonb_build_object(
                'limit_gb', NEW.max_storage_gb,
                'current_gb', NEW.current_storage_gb
            ),
            false
        );
    END IF;

    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

-- Trigger for usage limit enforcement
CREATE TRIGGER enforce_subscription_limits
    BEFORE UPDATE ON subscriptions
    FOR EACH ROW
    WHEN (OLD.current_messages_today IS DISTINCT FROM NEW.current_messages_today
          OR OLD.current_storage_gb IS DISTINCT FROM NEW.current_storage_gb)
    EXECUTE FUNCTION enforce_usage_limits();

-- Function to handle instance status changes
CREATE OR REPLACE FUNCTION handle_instance_status_change()
RETURNS TRIGGER AS $$
BEGIN
    -- Log status changes
    IF OLD.status != NEW.status THEN
        INSERT INTO audit_logs (
            account_id,
            instance_id,
            action,
            action_category,
            details
        )
        SELECT
            s.account_id,
            NEW.id,
            'instance_status_changed',
            'instance',
            jsonb_build_object(
                'old_status', OLD.status,
                'new_status', NEW.status,
                'subdomain', NEW.subdomain
            )
        FROM subscriptions s
        WHERE s.id = NEW.subscription_id;

        -- Update lifecycle timestamps
        CASE NEW.status
            WHEN 'running' THEN
                NEW.last_started_at := NOW();
                NEW.provisioned_at := COALESCE(NEW.provisioned_at, NOW());
            WHEN 'stopped' THEN
                NEW.last_stopped_at := NOW();
            WHEN 'deprovisioning' THEN
                NEW.deprovisioned_at := NOW();
        END CASE;
    END IF;

    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

-- Trigger for instance status changes
CREATE TRIGGER instance_status_change
    BEFORE UPDATE OF status ON instances
    FOR EACH ROW
    EXECUTE FUNCTION handle_instance_status_change();

-- Function to clean up expired data
CREATE OR REPLACE FUNCTION cleanup_expired_data()
RETURNS void AS $$
BEGIN
    -- Delete old audit logs (keep 90 days)
    DELETE FROM audit_logs
    WHERE created_at < NOW() - INTERVAL '90 days';

    -- Delete old usage metrics (keep 365 days)
    DELETE FROM usage_metrics
    WHERE date < CURRENT_DATE - INTERVAL '365 days';

    -- Delete expired backups
    DELETE FROM instance_backups
    WHERE expires_at < NOW()
    AND status = 'completed';

    -- Mark expired API keys as inactive
    UPDATE api_keys
    SET is_active = false
    WHERE expires_at < NOW()
    AND is_active = true;
END;
$$ LANGUAGE plpgsql;

-- Function to calculate uptime percentage
CREATE OR REPLACE FUNCTION calculate_uptime()
RETURNS void AS $$
BEGIN
    UPDATE instances
    SET uptime_percentage =
        CASE
            WHEN last_started_at IS NULL THEN 0
            WHEN status = 'running' THEN
                LEAST(100,
                    EXTRACT(EPOCH FROM (NOW() - COALESCE(last_stopped_at, last_started_at))) /
                    EXTRACT(EPOCH FROM (NOW() - last_started_at)) * 100
                )
            ELSE uptime_percentage
        END
    WHERE status IN ('running', 'stopped');
END;
$$ LANGUAGE plpgsql;

-- Function to auto-pause inactive instances
CREATE OR REPLACE FUNCTION auto_pause_inactive_instances()
RETURNS void AS $$
BEGIN
    UPDATE instances
    SET
        status = 'stopped',
        health_details = jsonb_build_object(
            'reason', 'auto_paused_inactive',
            'last_activity', last_health_check
        )
    WHERE status = 'running'
    AND last_health_check < NOW() - INTERVAL '7 days'
    AND subscription_id IN (
        SELECT id FROM subscriptions
        WHERE tier = 'free'
    );

    -- Log auto-pause events
    INSERT INTO audit_logs (
        account_id,
        instance_id,
        action,
        action_category,
        details
    )
    SELECT
        s.account_id,
        i.id,
        'instance_auto_paused',
        'instance',
        jsonb_build_object(
            'reason', 'inactivity',
            'last_activity', i.last_health_check
        )
    FROM instances i
    JOIN subscriptions s ON i.subscription_id = s.id
    WHERE i.status = 'stopped'
    AND i.health_details->>'reason' = 'auto_paused_inactive';
END;
$$ LANGUAGE plpgsql;

-- Create scheduled jobs (using pg_cron extension if available)
-- Note: This requires pg_cron extension to be enabled
-- If pg_cron is not available, these should be run from an external scheduler

-- Schedule daily reset of usage counters (runs at midnight UTC)
-- SELECT cron.schedule('reset-daily-usage', '0 0 * * *', 'SELECT reset_daily_usage();');

-- Schedule hourly uptime calculation
-- SELECT cron.schedule('calculate-uptime', '0 * * * *', 'SELECT calculate_uptime();');

-- Schedule daily cleanup of expired data (runs at 2 AM UTC)
-- SELECT cron.schedule('cleanup-expired', '0 2 * * *', 'SELECT cleanup_expired_data();');

-- Schedule auto-pause check every 6 hours
-- SELECT cron.schedule('auto-pause-instances', '0 */6 * * *', 'SELECT auto_pause_inactive_instances();');

-- Comments for documentation
COMMENT ON FUNCTION reset_daily_usage IS 'Reset daily usage counters at midnight';
COMMENT ON FUNCTION calculate_storage_usage IS 'Calculate and update storage usage for an instance';
COMMENT ON FUNCTION handle_tier_change IS 'Handle subscription tier changes and update limits';
COMMENT ON FUNCTION enforce_usage_limits IS 'Enforce usage limits and rate limiting';
COMMENT ON FUNCTION handle_instance_status_change IS 'Track instance status changes and lifecycle';
COMMENT ON FUNCTION cleanup_expired_data IS 'Clean up old data to save storage';
COMMENT ON FUNCTION calculate_uptime IS 'Calculate instance uptime percentage';
COMMENT ON FUNCTION auto_pause_inactive_instances IS 'Automatically pause inactive free tier instances';
-- Add auth_token field to instances table for simple authentication
ALTER TABLE instances
ADD COLUMN auth_token TEXT;

-- Add index for quick lookups
CREATE INDEX idx_instances_auth_token ON instances(auth_token);

-- Comment on the new column
COMMENT ON COLUMN instances.auth_token IS 'Simple authentication token for accessing the instance (temporary until proper auth is implemented)';-- Remove all Dokku references and use K8s-appropriate naming

-- Rename dokku_app_name to instance_id (unique identifier for the instance)
ALTER TABLE instances
RENAME COLUMN dokku_app_name TO instance_id;

-- Update the comment
COMMENT ON COLUMN instances.instance_id IS 'Unique identifier for the Kubernetes instance (e.g., sub1757)';

-- The subdomain column stays the same as it's still used for the URL
