# Agent 1: Supabase Setup & Database Schema

## Project Context

You are working on MindRoom, an AI agent platform that allows users to deploy their own AI assistants across multiple chat platforms (Slack, Discord, Telegram, etc.) via Matrix protocol bridges.

### Current Architecture Understanding

First, read these files to understand the existing system:
1. `README.md` - Understand what MindRoom does
2. `deploy/deploy.py` - See how instances are currently deployed
3. `config.yaml` - Understand agent configuration structure
4. `src/mindroom/bot.py` - Understand the core bot structure

### The Goal

We are transforming MindRoom from a single-user self-hosted solution into a multi-tenant SaaS platform where:
- Customers can sign up and pay for different subscription tiers
- Each customer gets their own isolated MindRoom instance
- Instances are automatically provisioned when customers subscribe via Stripe
- Each instance runs in its own Docker containers via Dokku

### Technology Stack
- **Dokku**: For container orchestration and deployment (like a personal Heroku)
- **Stripe**: For billing and subscription management
- **Supabase**: For authentication, database, and real-time updates
- **Docker**: Each customer gets isolated containers

## Your Specific Task

You will work ONLY in the `supabase/` directory to set up the authentication and database layer.

### Step 1: Initialize Supabase Project

```bash
cd /home/basnijholt/Work/mindroom-2
npx supabase init
```

### Step 2: Create Database Schema

Create migrations in `supabase/migrations/` for these tables:

```sql
-- 001_initial_schema.sql
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

-- Subscription tiers enum
CREATE TYPE subscription_tier AS ENUM ('free', 'starter', 'professional', 'enterprise');
CREATE TYPE subscription_status AS ENUM ('trialing', 'active', 'cancelled', 'past_due', 'incomplete');
CREATE TYPE instance_status AS ENUM ('provisioning', 'running', 'stopped', 'failed', 'deprovisioning');

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

    -- Usage tracking (reset daily)
    current_messages_today INTEGER DEFAULT 0,
    last_reset_at DATE DEFAULT CURRENT_DATE,

    trial_ends_at TIMESTAMPTZ,
    current_period_end TIMESTAMPTZ,
    cancelled_at TIMESTAMPTZ,
    created_at TIMESTAMPTZ DEFAULT NOW(),
    updated_at TIMESTAMPTZ DEFAULT NOW()
);

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

    -- Configuration from MindRoom
    config JSONB DEFAULT '{}'::jsonb, -- Store agent configs, tools, etc.
    environment_vars JSONB DEFAULT '{}'::jsonb, -- API keys, etc.

    -- Resource limits (enforced by Dokku)
    memory_limit_mb INTEGER DEFAULT 512,
    cpu_limit DECIMAL(3,2) DEFAULT 0.5, -- 0.5 = half a CPU core

    -- Health tracking
    last_health_check TIMESTAMPTZ,
    health_status TEXT,
    error_message TEXT,

    provisioned_at TIMESTAMPTZ,
    deprovisioned_at TIMESTAMPTZ,
    created_at TIMESTAMPTZ DEFAULT NOW(),
    updated_at TIMESTAMPTZ DEFAULT NOW()
);

-- Usage metrics table
CREATE TABLE usage_metrics (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    instance_id UUID NOT NULL REFERENCES instances(id) ON DELETE CASCADE,
    date DATE NOT NULL,

    messages_sent INTEGER DEFAULT 0,
    agents_used JSONB DEFAULT '{}'::jsonb, -- {"agent_name": count}
    tools_used JSONB DEFAULT '{}'::jsonb, -- {"tool_name": count}

    created_at TIMESTAMPTZ DEFAULT NOW(),
    UNIQUE(instance_id, date)
);

-- Audit log for important events
CREATE TABLE audit_logs (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    account_id UUID REFERENCES accounts(id) ON DELETE SET NULL,
    action TEXT NOT NULL,
    details JSONB DEFAULT '{}'::jsonb,
    ip_address INET,
    user_agent TEXT,
    created_at TIMESTAMPTZ DEFAULT NOW()
);
```

### Step 3: Create Row Level Security (RLS) Policies

```sql
-- 002_rls_policies.sql
-- Enable RLS
ALTER TABLE accounts ENABLE ROW LEVEL SECURITY;
ALTER TABLE subscriptions ENABLE ROW LEVEL SECURITY;
ALTER TABLE instances ENABLE ROW LEVEL SECURITY;
ALTER TABLE usage_metrics ENABLE ROW LEVEL SECURITY;

-- Accounts: Users can only see their own account
CREATE POLICY "Users can view own account" ON accounts
    FOR SELECT USING (auth.uid() = id);

CREATE POLICY "Users can update own account" ON accounts
    FOR UPDATE USING (auth.uid() = id);

-- Subscriptions: Users can only see their own subscriptions
CREATE POLICY "Users can view own subscriptions" ON subscriptions
    FOR SELECT USING (account_id = auth.uid());

-- Instances: Users can only see instances from their subscriptions
CREATE POLICY "Users can view own instances" ON instances
    FOR SELECT USING (
        subscription_id IN (
            SELECT id FROM subscriptions WHERE account_id = auth.uid()
        )
    );

-- Usage metrics: Users can view metrics for their instances
CREATE POLICY "Users can view own usage" ON usage_metrics
    FOR SELECT USING (
        instance_id IN (
            SELECT i.id FROM instances i
            JOIN subscriptions s ON i.subscription_id = s.id
            WHERE s.account_id = auth.uid()
        )
    );
```

### Step 4: Create Edge Functions

Create these Edge Functions in `supabase/functions/`:

#### A. `handle-stripe-webhook/index.ts`
```typescript
// This function receives webhooks from Stripe and updates our database
import { serve } from "https://deno.land/std@0.168.0/http/server.ts"
import { createClient } from "https://esm.sh/@supabase/supabase-js@2"
import Stripe from "https://esm.sh/stripe@13"

const stripe = new Stripe(Deno.env.get('STRIPE_SECRET_KEY')!, {
  apiVersion: '2023-10-16',
})

serve(async (req) => {
  const signature = req.headers.get('stripe-signature')!
  const body = await req.text()

  // Verify webhook signature
  const event = stripe.webhooks.constructEvent(
    body,
    signature,
    Deno.env.get('STRIPE_WEBHOOK_SECRET')!
  )

  const supabase = createClient(
    Deno.env.get('SUPABASE_URL')!,
    Deno.env.get('SUPABASE_SERVICE_KEY')!
  )

  switch (event.type) {
    case 'customer.subscription.created':
      // New subscription - provision instance
      // 1. Create subscription record
      // 2. Call provision-instance function
      break

    case 'customer.subscription.updated':
      // Subscription changed - update limits
      break

    case 'customer.subscription.deleted':
      // Subscription cancelled - deprovision instance
      break
  }

  return new Response(JSON.stringify({ received: true }), {
    headers: { "Content-Type": "application/json" },
    status: 200,
  })
})
```

#### B. `provision-instance/index.ts`
```typescript
// This function calls our Dokku provisioner service to create a new instance
import { serve } from "https://deno.land/std@0.168.0/http/server.ts"
import { createClient } from "https://esm.sh/@supabase/supabase-js@2"

serve(async (req) => {
  const { subscription_id } = await req.json()

  const supabase = createClient(
    Deno.env.get('SUPABASE_URL')!,
    Deno.env.get('SUPABASE_SERVICE_KEY')!
  )

  // 1. Get subscription details
  // 2. Generate unique app name
  // 3. Call Dokku provisioner service
  // 4. Update instance record with URLs
  // 5. Send welcome email

  return new Response(JSON.stringify({ success: true }), {
    headers: { "Content-Type": "application/json" },
    status: 200,
  })
})
```

#### C. `deprovision-instance/index.ts`
```typescript
// This function removes a customer's instance when they cancel
// Similar structure to provision-instance
```

### Step 5: Create Database Functions

Create `supabase/migrations/003_functions.sql`:

```sql
-- Function to get user's active instance
CREATE OR REPLACE FUNCTION get_user_instance(user_id UUID)
RETURNS TABLE (
    instance_id UUID,
    subdomain TEXT,
    frontend_url TEXT,
    backend_url TEXT,
    status instance_status,
    tier subscription_tier
) AS $$
BEGIN
    RETURN QUERY
    SELECT
        i.id,
        i.subdomain,
        i.frontend_url,
        i.backend_url,
        i.status,
        s.tier
    FROM instances i
    JOIN subscriptions s ON i.subscription_id = s.id
    WHERE s.account_id = user_id
    AND s.status = 'active'
    AND i.status = 'running'
    ORDER BY i.created_at DESC
    LIMIT 1;
END;
$$ LANGUAGE plpgsql;

-- Function to track daily usage
CREATE OR REPLACE FUNCTION track_usage(
    p_instance_id UUID,
    p_agent_name TEXT DEFAULT NULL,
    p_tool_name TEXT DEFAULT NULL
) RETURNS void AS $$
BEGIN
    -- Insert or update today's metrics
    INSERT INTO usage_metrics (instance_id, date, messages_sent, agents_used, tools_used)
    VALUES (p_instance_id, CURRENT_DATE, 1,
            CASE WHEN p_agent_name IS NOT NULL
                 THEN jsonb_build_object(p_agent_name, 1)
                 ELSE '{}'::jsonb
            END,
            CASE WHEN p_tool_name IS NOT NULL
                 THEN jsonb_build_object(p_tool_name, 1)
                 ELSE '{}'::jsonb
            END
    )
    ON CONFLICT (instance_id, date) DO UPDATE
    SET messages_sent = usage_metrics.messages_sent + 1,
        agents_used = CASE WHEN p_agent_name IS NOT NULL
                          THEN usage_metrics.agents_used ||
                               jsonb_build_object(p_agent_name,
                                   COALESCE((usage_metrics.agents_used->>p_agent_name)::int, 0) + 1)
                          ELSE usage_metrics.agents_used
                      END,
        tools_used = CASE WHEN p_tool_name IS NOT NULL
                         THEN usage_metrics.tools_used ||
                              jsonb_build_object(p_tool_name,
                                  COALESCE((usage_metrics.tools_used->>p_tool_name)::int, 0) + 1)
                         ELSE usage_metrics.tools_used
                     END;

    -- Update subscription daily usage
    UPDATE subscriptions
    SET current_messages_today = current_messages_today + 1
    WHERE id = (SELECT subscription_id FROM instances WHERE id = p_instance_id);
END;
$$ LANGUAGE plpgsql;
```

### Step 6: Create Seed Data

Create `supabase/seed.sql`:

```sql
-- Test data for development
INSERT INTO accounts (email, full_name, stripe_customer_id) VALUES
('test@example.com', 'Test User', 'cus_test123');

INSERT INTO subscriptions (account_id, tier, status, max_agents, max_messages_per_day)
SELECT id, 'starter', 'active', 3, 1000
FROM accounts WHERE email = 'test@example.com';
```

### Step 7: Configuration

Create `supabase/config.toml` and configure:
- API settings
- Auth settings (enable email auth)
- Email templates
- Database settings

### Step 8: Environment Variables

Create `supabase/.env.example`:

```bash
# Supabase
SUPABASE_URL=https://your-project.supabase.co
SUPABASE_ANON_KEY=your-anon-key
SUPABASE_SERVICE_KEY=your-service-key

# Stripe
STRIPE_SECRET_KEY=sk_test_...
STRIPE_WEBHOOK_SECRET=whsec_...

# Dokku Provisioner
DOKKU_PROVISIONER_URL=http://localhost:8002

# Email
RESEND_API_KEY=re_...
```

## Output Files You Must Create

```
supabase/
├── config.toml
├── .env.example
├── migrations/
│   ├── 001_initial_schema.sql
│   ├── 002_rls_policies.sql
│   ├── 003_functions.sql
│   └── 004_triggers.sql
├── functions/
│   ├── handle-stripe-webhook/
│   │   └── index.ts
│   ├── provision-instance/
│   │   └── index.ts
│   ├── deprovision-instance/
│   │   └── index.ts
│   └── check-instance-health/
│       └── index.ts
├── seed.sql
└── README.md (explaining the schema and functions)
```

## Important Notes

1. DO NOT modify any files outside the `supabase/` directory
2. DO NOT touch the existing MindRoom bot code
3. Focus only on the database and authentication layer
4. Make sure all Edge Functions handle errors gracefully
5. Include proper TypeScript types for all functions
6. Add comments explaining complex logic
7. Consider rate limiting and security in all functions

## Testing Your Work

After creating all files, test with:

```bash
# Start local Supabase
npx supabase start

# Run migrations
npx supabase db reset

# Test Edge Functions
npx supabase functions serve

# Deploy to production when ready
npx supabase deploy
```

Remember: You are building the foundation that other services will rely on. Make it robust and well-documented.
