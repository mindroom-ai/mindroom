-- Enable Row Level Security on all tables
-- This prevents unauthorized access to data

-- ============================================================================
-- ENABLE RLS ON ALL TABLES
-- ============================================================================

ALTER TABLE accounts ENABLE ROW LEVEL SECURITY;
ALTER TABLE subscriptions ENABLE ROW LEVEL SECURITY;
ALTER TABLE instances ENABLE ROW LEVEL SECURITY;
ALTER TABLE usage_metrics ENABLE ROW LEVEL SECURITY;
ALTER TABLE webhook_events ENABLE ROW LEVEL SECURITY;
ALTER TABLE audit_logs ENABLE ROW LEVEL SECURITY;

-- ============================================================================
-- SERVICE ROLE POLICIES (Full access for backend services)
-- ============================================================================

-- Service role can do everything (for stripe-handler, admin dashboard, etc.)
CREATE POLICY "Service role full access" ON accounts
    FOR ALL USING (auth.role() = 'service_role');

CREATE POLICY "Service role full access" ON subscriptions
    FOR ALL USING (auth.role() = 'service_role');

CREATE POLICY "Service role full access" ON instances
    FOR ALL USING (auth.role() = 'service_role');

CREATE POLICY "Service role full access" ON usage_metrics
    FOR ALL USING (auth.role() = 'service_role');

CREATE POLICY "Service role full access" ON webhook_events
    FOR ALL USING (auth.role() = 'service_role');

CREATE POLICY "Service role full access" ON audit_logs
    FOR ALL USING (auth.role() = 'service_role');

-- ============================================================================
-- AUTHENTICATED USER POLICIES (For logged-in users)
-- ============================================================================

-- Users can view their own account
CREATE POLICY "Users can view own account" ON accounts
    FOR SELECT USING (
        auth.uid() IS NOT NULL AND
        email = auth.jwt()->>'email'
    );

-- Users can view their own subscriptions
CREATE POLICY "Users can view own subscriptions" ON subscriptions
    FOR SELECT USING (
        auth.uid() IS NOT NULL AND
        account_id IN (
            SELECT id FROM accounts
            WHERE email = auth.jwt()->>'email'
        )
    );

-- Users can view their own instances
CREATE POLICY "Users can view own instances" ON instances
    FOR SELECT USING (
        auth.uid() IS NOT NULL AND
        subscription_id IN (
            SELECT id FROM subscriptions
            WHERE account_id IN (
                SELECT id FROM accounts
                WHERE email = auth.jwt()->>'email'
            )
        )
    );

-- Users can view their own usage metrics
CREATE POLICY "Users can view own usage" ON usage_metrics
    FOR SELECT USING (
        auth.uid() IS NOT NULL AND
        subscription_id IN (
            SELECT id FROM subscriptions
            WHERE account_id IN (
                SELECT id FROM accounts
                WHERE email = auth.jwt()->>'email'
            )
        )
    );

-- ============================================================================
-- PUBLIC POLICIES (Very restrictive)
-- ============================================================================

-- No public access to any tables by default
-- The anon key cannot read or write anything

-- ============================================================================
-- COMMENTS
-- ============================================================================

COMMENT ON POLICY "Service role full access" ON accounts IS
    'Backend services with service_role key have full access';

COMMENT ON POLICY "Users can view own account" ON accounts IS
    'Authenticated users can only see their own account data';

COMMENT ON POLICY "Users can view own subscriptions" ON subscriptions IS
    'Authenticated users can only see their own subscription data';

COMMENT ON POLICY "Users can view own instances" ON instances IS
    'Authenticated users can only see their own instance data';

COMMENT ON POLICY "Users can view own usage" ON usage_metrics IS
    'Authenticated users can only see their own usage metrics';
