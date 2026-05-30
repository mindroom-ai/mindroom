-- Hosted AI budget plans.
-- Converts the pre-customer Starter/Professional plan names to BYOK/Hobby/Pro
-- and stores only non-secret OpenRouter key metadata on instances.

ALTER TABLE accounts DROP CONSTRAINT IF EXISTS accounts_tier_check;
ALTER TABLE subscriptions DROP CONSTRAINT IF EXISTS subscriptions_tier_check;

UPDATE accounts
SET tier = CASE tier
    WHEN 'starter' THEN 'byok'
    WHEN 'professional' THEN 'pro'
    ELSE tier
END
WHERE tier IN ('starter', 'professional');

UPDATE subscriptions
SET tier = CASE tier
    WHEN 'starter' THEN 'byok'
    WHEN 'professional' THEN 'pro'
    ELSE tier
END
WHERE tier IN ('starter', 'professional');

UPDATE instances
SET tier = CASE tier
    WHEN 'starter' THEN 'byok'
    WHEN 'professional' THEN 'pro'
    ELSE tier
END
WHERE tier IN ('starter', 'professional');

ALTER TABLE accounts
    ADD CONSTRAINT accounts_tier_check CHECK (tier IN ('free', 'byok', 'hobby', 'pro', 'enterprise'));

ALTER TABLE subscriptions
    ADD CONSTRAINT subscriptions_tier_check CHECK (tier IN ('free', 'byok', 'hobby', 'pro', 'enterprise'));

ALTER TABLE instances
    ADD COLUMN IF NOT EXISTS openrouter_key_hash TEXT,
    ADD COLUMN IF NOT EXISTS openrouter_key_label TEXT,
    ADD COLUMN IF NOT EXISTS openrouter_key_limit_usd INTEGER,
    ADD COLUMN IF NOT EXISTS openrouter_key_limit_reset TEXT,
    ADD COLUMN IF NOT EXISTS openrouter_key_created_at TIMESTAMPTZ;
