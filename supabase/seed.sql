-- Seed data for development and testing
-- This file contains test data for the MindRoom SaaS platform

-- Test accounts
INSERT INTO accounts (id, email, full_name, company_name, stripe_customer_id) VALUES
('00000000-0000-0000-0000-000000000001', 'test@example.com', 'Test User', 'Test Company', 'cus_test_free'),
('00000000-0000-0000-0000-000000000002', 'starter@example.com', 'Starter User', 'Startup Inc', 'cus_test_starter'),
('00000000-0000-0000-0000-000000000003', 'pro@example.com', 'Pro User', 'Professional Corp', 'cus_test_pro'),
('00000000-0000-0000-0000-000000000004', 'enterprise@example.com', 'Enterprise User', 'Enterprise LLC', 'cus_test_enterprise')
ON CONFLICT (id) DO NOTHING;

-- Test subscriptions (different tiers)
INSERT INTO subscriptions (
  id,
  account_id,
  stripe_subscription_id,
  stripe_price_id,
  tier,
  status,
  max_agents,
  max_messages_per_day,
  max_storage_gb,
  max_platforms,
  features,
  current_period_start,
  current_period_end
) VALUES
(
  '10000000-0000-0000-0000-000000000001',
  '00000000-0000-0000-0000-000000000001',
  'sub_test_free',
  'price_free',
  'free',
  'active',
  1,
  100,
  1,
  1,
  '{
    "custom_agents": false,
    "api_access": false,
    "priority_support": false,
    "advanced_memory": false,
    "voice_messages": false,
    "file_uploads": false,
    "team_collaboration": false
  }'::jsonb,
  NOW(),
  NOW() + INTERVAL '30 days'
),
(
  '10000000-0000-0000-0000-000000000002',
  '00000000-0000-0000-0000-000000000002',
  'sub_test_starter',
  'price_starter_monthly',
  'starter',
  'active',
  3,
  1000,
  5,
  3,
  '{
    "custom_agents": false,
    "api_access": true,
    "priority_support": false,
    "advanced_memory": true,
    "voice_messages": true,
    "file_uploads": true,
    "team_collaboration": false
  }'::jsonb,
  NOW(),
  NOW() + INTERVAL '30 days'
),
(
  '10000000-0000-0000-0000-000000000003',
  '00000000-0000-0000-0000-000000000003',
  'sub_test_pro',
  'price_professional_monthly',
  'professional',
  'active',
  10,
  10000,
  50,
  10,
  '{
    "custom_agents": true,
    "api_access": true,
    "priority_support": true,
    "advanced_memory": true,
    "voice_messages": true,
    "file_uploads": true,
    "team_collaboration": true
  }'::jsonb,
  NOW(),
  NOW() + INTERVAL '30 days'
),
(
  '10000000-0000-0000-0000-000000000004',
  '00000000-0000-0000-0000-000000000004',
  'sub_test_enterprise',
  'price_enterprise_monthly',
  'enterprise',
  'trialing',
  999,
  999999,
  999,
  999,
  '{
    "custom_agents": true,
    "api_access": true,
    "priority_support": true,
    "advanced_memory": true,
    "voice_messages": true,
    "file_uploads": true,
    "team_collaboration": true
  }'::jsonb,
  NOW(),
  NOW() + INTERVAL '30 days'
)
ON CONFLICT (id) DO NOTHING;

-- Test instances
INSERT INTO instances (
  id,
  subscription_id,
  dokku_app_name,
  subdomain,
  status,
  backend_url,
  frontend_url,
  matrix_server_url,
  config,
  memory_limit_mb,
  cpu_limit,
  disk_limit_gb,
  health_status,
  provisioned_at
) VALUES
(
  '20000000-0000-0000-0000-000000000001',
  '10000000-0000-0000-0000-000000000001',
  'mindroom-test-free',
  'test-free',
  'running',
  'https://test-free-api.mindroom.app',
  'https://test-free.mindroom.app',
  'https://test-free-matrix.mindroom.app',
  '{
    "agents": {
      "assistant": {
        "display_name": "Assistant",
        "model": "gpt-4",
        "instructions": ["Help users with their questions"]
      }
    },
    "rooms": ["lobby"],
    "tools": [],
    "models": {}
  }'::jsonb,
  512,
  0.5,
  1,
  'healthy',
  NOW() - INTERVAL '7 days'
),
(
  '20000000-0000-0000-0000-000000000002',
  '10000000-0000-0000-0000-000000000002',
  'mindroom-test-starter',
  'test-starter',
  'running',
  'https://test-starter-api.mindroom.app',
  'https://test-starter.mindroom.app',
  'https://test-starter-matrix.mindroom.app',
  '{
    "agents": {
      "assistant": {
        "display_name": "Assistant",
        "model": "gpt-4",
        "instructions": ["Help users with their questions"]
      },
      "researcher": {
        "display_name": "Researcher",
        "model": "claude-3",
        "instructions": ["Research topics and provide insights"]
      }
    },
    "rooms": ["lobby", "research"],
    "tools": ["web_search", "calculator"],
    "models": {
      "default": "gpt-4",
      "research": "claude-3"
    }
  }'::jsonb,
  1024,
  1.0,
  5,
  'healthy',
  NOW() - INTERVAL '30 days'
),
(
  '20000000-0000-0000-0000-000000000003',
  '10000000-0000-0000-0000-000000000003',
  'mindroom-test-pro',
  'test-pro',
  'stopped',
  'https://test-pro-api.mindroom.app',
  'https://test-pro.mindroom.app',
  'https://test-pro-matrix.mindroom.app',
  '{
    "agents": {},
    "rooms": [],
    "tools": [],
    "models": {}
  }'::jsonb,
  2048,
  2.0,
  50,
  'stopped',
  NOW() - INTERVAL '60 days'
)
ON CONFLICT (id) DO NOTHING;

-- Test usage metrics (for the past week)
INSERT INTO usage_metrics (
  instance_id,
  date,
  messages_sent,
  messages_received,
  agents_used,
  tools_used,
  platforms_active,
  average_response_time_ms,
  error_count,
  storage_used_mb
)
SELECT
  '20000000-0000-0000-0000-000000000002',
  CURRENT_DATE - i,
  50 + (random() * 50)::int,
  100 + (random() * 100)::int,
  jsonb_build_object('assistant', (10 + (random() * 20)::int), 'researcher', (5 + (random() * 10)::int)),
  jsonb_build_object('web_search', (5 + (random() * 10)::int), 'calculator', (2 + (random() * 5)::int)),
  '{"slack": true, "discord": false, "telegram": true}'::jsonb,
  200 + (random() * 300)::int,
  (random() * 5)::int,
  100 + (random() * 50)::int
FROM generate_series(0, 6) AS i;

-- Test API keys
INSERT INTO api_keys (
  id,
  account_id,
  name,
  key_hash,
  key_prefix,
  permissions,
  is_active
) VALUES
(
  '30000000-0000-0000-0000-000000000001',
  '00000000-0000-0000-0000-000000000002',
  'Development API Key',
  '$2b$10$EixZaYVK1fsbw1ZfbX3OXePaWxn96p36WQoeG6Lruj3vjPGga31lW', -- hashed version
  'mr_test_',
  '["read", "write"]'::jsonb,
  true
),
(
  '30000000-0000-0000-0000-000000000002',
  '00000000-0000-0000-0000-000000000003',
  'Production API Key',
  '$2b$10$K1enCiQdLpKy3h6wVnZSQuPuLJqwGMYqfP0dkwHrqzoBe7JBhMBHa', -- hashed version
  'mr_prod_',
  '["read", "write", "admin"]'::jsonb,
  true
)
ON CONFLICT (id) DO NOTHING;

-- Test audit logs
INSERT INTO audit_logs (
  account_id,
  instance_id,
  action,
  action_category,
  details,
  success
)
SELECT
  '00000000-0000-0000-0000-000000000002',
  '20000000-0000-0000-0000-000000000002',
  CASE (random() * 4)::int
    WHEN 0 THEN 'instance_started'
    WHEN 1 THEN 'config_updated'
    WHEN 2 THEN 'api_call'
    WHEN 3 THEN 'agent_added'
    ELSE 'health_check'
  END,
  CASE (random() * 3)::int
    WHEN 0 THEN 'instance'
    WHEN 1 THEN 'config'
    ELSE 'api'
  END,
  jsonb_build_object(
    'timestamp', NOW() - (i || ' hours')::interval,
    'user_agent', 'Mozilla/5.0'
  ),
  random() > 0.1
FROM generate_series(0, 50) AS i;

-- Test support ticket
INSERT INTO support_tickets (
  id,
  account_id,
  instance_id,
  subject,
  description,
  priority,
  status
) VALUES
(
  '40000000-0000-0000-0000-000000000001',
  '00000000-0000-0000-0000-000000000002',
  '20000000-0000-0000-0000-000000000002',
  'Agent not responding in Slack',
  'My assistant agent stopped responding in our Slack workspace. It was working fine yesterday but now doesn''t acknowledge any messages.',
  'high',
  'open'
)
ON CONFLICT (id) DO NOTHING;

-- Comments for clarity
COMMENT ON TABLE accounts IS 'Test accounts with different subscription tiers for development';
COMMENT ON TABLE subscriptions IS 'Test subscriptions demonstrating different tier features and limits';
COMMENT ON TABLE instances IS 'Test instances showing various states and configurations';
COMMENT ON TABLE usage_metrics IS 'Simulated usage data for testing billing and analytics';
COMMENT ON TABLE audit_logs IS 'Sample audit trail for testing logging and monitoring';
