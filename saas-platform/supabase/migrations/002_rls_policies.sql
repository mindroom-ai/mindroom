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
