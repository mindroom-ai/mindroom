-- Test data for MindRoom SaaS platform
-- This creates sample customer accounts and subscriptions for testing

-- Insert test customer accounts
INSERT INTO accounts (email, full_name, company_name)
VALUES
    ('test@example.com', 'Test User', 'Test Company'),
    ('demo@example.com', 'Demo User', 'Demo Inc')
ON CONFLICT (email) DO NOTHING;

-- Insert test subscription for the test user
INSERT INTO subscriptions (account_id, tier, status, max_agents, max_messages_per_day, max_storage_gb)
SELECT id, 'starter', 'active', 5, 5000, 10
FROM accounts
WHERE email = 'test@example.com'
ON CONFLICT DO NOTHING;

-- Insert instance for test user
INSERT INTO instances (
    account_id,
    name,
    status,
    dokku_app_name,
    url
)
SELECT
    id,
    'test-instance',
    'running',
    'mindroom-test-' || SUBSTRING(id::text, 1, 8),
    'https://test-' || SUBSTRING(id::text, 1, 8) || '.mindroom.chat'
FROM accounts
WHERE email = 'test@example.com'
ON CONFLICT DO NOTHING;
