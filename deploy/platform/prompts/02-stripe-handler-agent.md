# Agent 2: Stripe Integration Service

## Project Context

You are working on MindRoom, an AI agent platform that allows users to deploy their own AI assistants across multiple chat platforms. Read `README.md` to understand the full vision.

### Current System Understanding

First, read these files:
1. `README.md` - Core product understanding
2. `config.yaml` - See the different agent types and configurations
3. `deploy/deploy.py` - Understand current deployment mechanism

### The Goal

We are building a multi-tenant SaaS where customers can subscribe to different tiers and get their own isolated MindRoom instance. Your service will handle all Stripe payment events and coordinate with other services to provision/manage customer instances.

### Architecture Overview
- When a customer subscribes via Stripe → Your service gets webhook
- Your service updates Supabase database → Triggers instance provisioning
- When subscription changes → Your service updates resource limits
- When customer cancels → Your service triggers deprovisioning

## Your Specific Task

You will work ONLY in the `services/stripe-handler/` directory to build a Node.js service that processes Stripe webhooks.

### Step 1: Initialize Project

```bash
cd services/stripe-handler
npm init -y
npm install express stripe @supabase/supabase-js dotenv cors helmet morgan
npm install -D @types/node @types/express typescript nodemon ts-node
```

### Step 2: Create Project Structure

```
services/stripe-handler/
├── src/
│   ├── index.ts                 # Main server
│   ├── config.ts                # Configuration
│   ├── routes/
│   │   └── webhooks.ts         # Stripe webhook handler
│   ├── handlers/
│   │   ├── subscription.ts     # Subscription event handlers
│   │   ├── invoice.ts          # Invoice event handlers
│   │   └── customer.ts         # Customer event handlers
│   ├── services/
│   │   ├── supabase.ts        # Supabase client and operations
│   │   ├── provisioner.ts     # Call Dokku provisioner
│   │   └── email.ts           # Email notifications
│   └── types/
│       └── index.ts            # TypeScript types
├── package.json
├── tsconfig.json
├── .env.example
├── Dockerfile
└── README.md
```

### Step 3: Core Implementation

#### A. `src/index.ts` - Main Server
```typescript
import express from 'express';
import cors from 'cors';
import helmet from 'helmet';
import morgan from 'morgan';
import { webhookRouter } from './routes/webhooks';
import { config } from './config';

const app = express();

// Middleware
app.use(helmet());
app.use(cors());
app.use(morgan('combined'));

// IMPORTANT: Raw body for Stripe webhooks
app.use('/webhooks/stripe', express.raw({ type: 'application/json' }));
app.use(express.json());

// Routes
app.use('/webhooks', webhookRouter);

// Health check
app.get('/health', (req, res) => {
  res.json({ status: 'healthy', service: 'stripe-handler' });
});

app.listen(config.port, () => {
  console.log(`Stripe handler running on port ${config.port}`);
});
```

#### B. `src/config.ts` - Configuration
```typescript
import dotenv from 'dotenv';
dotenv.config();

export const config = {
  port: process.env.PORT || 3005,
  stripe: {
    secretKey: process.env.STRIPE_SECRET_KEY!,
    webhookSecret: process.env.STRIPE_WEBHOOK_SECRET!,

    // Price IDs for different tiers
    prices: {
      starter: process.env.STRIPE_PRICE_STARTER || 'price_starter',
      professional: process.env.STRIPE_PRICE_PRO || 'price_pro',
      enterprise: process.env.STRIPE_PRICE_ENTERPRISE || 'price_enterprise',
    }
  },
  supabase: {
    url: process.env.SUPABASE_URL!,
    serviceKey: process.env.SUPABASE_SERVICE_KEY!,
  },
  provisioner: {
    url: process.env.DOKKU_PROVISIONER_URL || 'http://localhost:8002',
    apiKey: process.env.PROVISIONER_API_KEY,
  }
};
```

#### C. `src/routes/webhooks.ts` - Webhook Router
```typescript
import { Router } from 'express';
import Stripe from 'stripe';
import { config } from '../config';
import { handleSubscriptionCreated, handleSubscriptionUpdated, handleSubscriptionDeleted } from '../handlers/subscription';
import { handleInvoicePaymentSucceeded, handleInvoicePaymentFailed } from '../handlers/invoice';

const stripe = new Stripe(config.stripe.secretKey, {
  apiVersion: '2023-10-16',
});

export const webhookRouter = Router();

webhookRouter.post('/stripe', async (req, res) => {
  const sig = req.headers['stripe-signature'] as string;

  let event: Stripe.Event;

  try {
    event = stripe.webhooks.constructEvent(
      req.body,
      sig,
      config.stripe.webhookSecret
    );
  } catch (err: any) {
    console.error('Webhook signature verification failed:', err.message);
    return res.status(400).send(`Webhook Error: ${err.message}`);
  }

  // Handle the event
  try {
    switch (event.type) {
      case 'customer.subscription.created':
        await handleSubscriptionCreated(event.data.object as Stripe.Subscription);
        break;

      case 'customer.subscription.updated':
        await handleSubscriptionUpdated(event.data.object as Stripe.Subscription);
        break;

      case 'customer.subscription.deleted':
        await handleSubscriptionDeleted(event.data.object as Stripe.Subscription);
        break;

      case 'invoice.payment_succeeded':
        await handleInvoicePaymentSucceeded(event.data.object as Stripe.Invoice);
        break;

      case 'invoice.payment_failed':
        await handleInvoicePaymentFailed(event.data.object as Stripe.Invoice);
        break;

      default:
        console.log(`Unhandled event type ${event.type}`);
    }

    res.json({ received: true });
  } catch (error) {
    console.error('Error processing webhook:', error);
    res.status(500).send('Error processing webhook');
  }
});
```

#### D. `src/handlers/subscription.ts` - Subscription Handlers
```typescript
import Stripe from 'stripe';
import { supabase } from '../services/supabase';
import { provisionInstance, deprovisionInstance, updateInstanceLimits } from '../services/provisioner';
import { sendEmail } from '../services/email';

// Map Stripe price IDs to our tiers
function getTierFromPriceId(priceId: string): string {
  const priceMap: { [key: string]: string } = {
    'price_starter': 'starter',
    'price_pro': 'professional',
    'price_enterprise': 'enterprise',
  };
  return priceMap[priceId] || 'free';
}

// Map tier to resource limits
function getResourceLimits(tier: string) {
  const limits: { [key: string]: any } = {
    'free': { agents: 1, messagesPerDay: 100, memoryMb: 256, cpuLimit: 0.25 },
    'starter': { agents: 3, messagesPerDay: 1000, memoryMb: 512, cpuLimit: 0.5 },
    'professional': { agents: 10, messagesPerDay: 10000, memoryMb: 2048, cpuLimit: 2 },
    'enterprise': { agents: -1, messagesPerDay: -1, memoryMb: 8192, cpuLimit: 4 },
  };
  return limits[tier] || limits['free'];
}

export async function handleSubscriptionCreated(subscription: Stripe.Subscription) {
  console.log('Handling subscription created:', subscription.id);

  // Get customer email from Stripe
  const customerId = subscription.customer as string;

  // Get or create account in Supabase
  const { data: account, error: accountError } = await supabase
    .from('accounts')
    .upsert({
      stripe_customer_id: customerId,
      email: subscription.metadata?.email || '',
    })
    .select()
    .single();

  if (accountError) {
    console.error('Error creating account:', accountError);
    throw accountError;
  }

  // Get tier from price ID
  const tier = getTierFromPriceId(subscription.items.data[0].price.id);
  const limits = getResourceLimits(tier);

  // Create subscription record
  const { data: sub, error: subError } = await supabase
    .from('subscriptions')
    .insert({
      account_id: account.id,
      stripe_subscription_id: subscription.id,
      stripe_price_id: subscription.items.data[0].price.id,
      tier: tier,
      status: subscription.status,
      max_agents: limits.agents,
      max_messages_per_day: limits.messagesPerDay,
      current_period_end: new Date(subscription.current_period_end * 1000),
      trial_ends_at: subscription.trial_end ? new Date(subscription.trial_end * 1000) : null,
    })
    .select()
    .single();

  if (subError) {
    console.error('Error creating subscription:', subError);
    throw subError;
  }

  // Provision instance
  const instanceData = await provisionInstance({
    subscriptionId: sub.id,
    accountId: account.id,
    tier: tier,
    limits: limits,
  });

  // Create instance record
  await supabase
    .from('instances')
    .insert({
      subscription_id: sub.id,
      dokku_app_name: instanceData.appName,
      subdomain: instanceData.subdomain,
      status: 'provisioning',
      frontend_url: instanceData.frontendUrl,
      backend_url: instanceData.backendUrl,
      memory_limit_mb: limits.memoryMb,
      cpu_limit: limits.cpuLimit,
    });

  // Send welcome email
  await sendEmail({
    to: account.email,
    subject: 'Welcome to MindRoom!',
    template: 'welcome',
    data: {
      instanceUrl: instanceData.frontendUrl,
      tier: tier,
    },
  });
}

export async function handleSubscriptionUpdated(subscription: Stripe.Subscription) {
  console.log('Handling subscription updated:', subscription.id);

  // Update subscription in database
  const newTier = getTierFromPriceId(subscription.items.data[0].price.id);
  const limits = getResourceLimits(newTier);

  const { data: sub } = await supabase
    .from('subscriptions')
    .update({
      tier: newTier,
      status: subscription.status,
      max_agents: limits.agents,
      max_messages_per_day: limits.messagesPerDay,
      current_period_end: new Date(subscription.current_period_end * 1000),
    })
    .eq('stripe_subscription_id', subscription.id)
    .select()
    .single();

  if (sub) {
    // Update instance resource limits
    const { data: instance } = await supabase
      .from('instances')
      .select('dokku_app_name')
      .eq('subscription_id', sub.id)
      .single();

    if (instance) {
      await updateInstanceLimits({
        appName: instance.dokku_app_name,
        limits: limits,
      });

      // Update instance record
      await supabase
        .from('instances')
        .update({
          memory_limit_mb: limits.memoryMb,
          cpu_limit: limits.cpuLimit,
        })
        .eq('subscription_id', sub.id);
    }
  }
}

export async function handleSubscriptionDeleted(subscription: Stripe.Subscription) {
  console.log('Handling subscription deleted:', subscription.id);

  // Get subscription and instance
  const { data: sub } = await supabase
    .from('subscriptions')
    .select('*, instances(*)')
    .eq('stripe_subscription_id', subscription.id)
    .single();

  if (sub && sub.instances?.[0]) {
    // Deprovision instance
    await deprovisionInstance({
      appName: sub.instances[0].dokku_app_name,
    });

    // Update instance status
    await supabase
      .from('instances')
      .update({
        status: 'deprovisioning',
        deprovisioned_at: new Date(),
      })
      .eq('id', sub.instances[0].id);

    // Update subscription status
    await supabase
      .from('subscriptions')
      .update({
        status: 'cancelled',
        cancelled_at: new Date(),
      })
      .eq('id', sub.id);
  }
}
```

#### E. `src/services/provisioner.ts` - Dokku Provisioner Client
```typescript
import fetch from 'node-fetch';
import { config } from '../config';

export async function provisionInstance(data: {
  subscriptionId: string;
  accountId: string;
  tier: string;
  limits: any;
}) {
  const response = await fetch(`${config.provisioner.url}/provision`, {
    method: 'POST',
    headers: {
      'Content-Type': 'application/json',
      'X-API-Key': config.provisioner.apiKey || '',
    },
    body: JSON.stringify(data),
  });

  if (!response.ok) {
    throw new Error(`Provisioner error: ${response.statusText}`);
  }

  return response.json();
}

export async function deprovisionInstance(data: {
  appName: string;
}) {
  const response = await fetch(`${config.provisioner.url}/deprovision`, {
    method: 'DELETE',
    headers: {
      'Content-Type': 'application/json',
      'X-API-Key': config.provisioner.apiKey || '',
    },
    body: JSON.stringify(data),
  });

  if (!response.ok) {
    throw new Error(`Deprovisioner error: ${response.statusText}`);
  }

  return response.json();
}

export async function updateInstanceLimits(data: {
  appName: string;
  limits: any;
}) {
  const response = await fetch(`${config.provisioner.url}/update`, {
    method: 'PUT',
    headers: {
      'Content-Type': 'application/json',
      'X-API-Key': config.provisioner.apiKey || '',
    },
    body: JSON.stringify(data),
  });

  if (!response.ok) {
    throw new Error(`Update limits error: ${response.statusText}`);
  }

  return response.json();
}
```

### Step 4: Dockerfile

```dockerfile
FROM node:18-alpine

WORKDIR /app

# Copy package files
COPY package*.json ./
RUN npm ci --production

# Copy source
COPY . .

# Build TypeScript
RUN npm run build

# Run as non-root
USER node

EXPOSE 3005

CMD ["node", "dist/index.js"]
```

### Step 5: Environment Variables

Create `.env.example`:

```bash
# Server
PORT=3005

# Stripe
STRIPE_SECRET_KEY=sk_test_...
STRIPE_WEBHOOK_SECRET=whsec_...
STRIPE_PRICE_STARTER=price_...
STRIPE_PRICE_PRO=price_...
STRIPE_PRICE_ENTERPRISE=price_...

# Supabase
SUPABASE_URL=https://xxx.supabase.co
SUPABASE_SERVICE_KEY=eyJ...

# Dokku Provisioner
DOKKU_PROVISIONER_URL=http://localhost:8002
PROVISIONER_API_KEY=secret_key

# Email (optional)
RESEND_API_KEY=re_...
```

### Step 6: Testing

Create `tests/webhook.test.js` to test webhook handling:

```javascript
// Test webhook signature verification
// Test each event type
// Test error handling
// Test idempotency
```

## Important Subscription Tiers

Based on the MindRoom value proposition, implement these tiers:

| Tier | Price | Agents | Messages/Day | Features |
|------|-------|--------|--------------|----------|
| Free | $0 | 1 | 100 | Basic tools only |
| Starter | $49/mo | 3 | 1,000 | All tools, email support |
| Professional | $199/mo | 10 | 10,000 | Priority support, API access |
| Enterprise | Custom | Unlimited | Unlimited | SLA, dedicated support |

## Key Implementation Details

1. **Idempotency**: Stripe may send webhooks multiple times. Use event IDs to prevent duplicate processing.

2. **Error Handling**: If provisioning fails, update the instance status to 'failed' and notify admin.

3. **Grace Period**: When payment fails, give 7 days before deprovisioning.

4. **Usage Tracking**: Track API calls and messages to enforce limits.

5. **Security**: Verify webhook signatures, use service keys for Supabase.

## Output Files Required

```
services/stripe-handler/
├── src/
│   ├── index.ts
│   ├── config.ts
│   ├── routes/
│   │   └── webhooks.ts
│   ├── handlers/
│   │   ├── subscription.ts
│   │   ├── invoice.ts
│   │   └── customer.ts
│   ├── services/
│   │   ├── supabase.ts
│   │   ├── provisioner.ts
│   │   └── email.ts
│   └── types/
│       └── index.ts
├── package.json
├── tsconfig.json
├── .env.example
├── Dockerfile
├── docker-compose.yml (for local testing)
└── README.md
```

## Important Notes

1. DO NOT modify any files outside `services/stripe-handler/`
2. DO NOT touch the existing MindRoom bot code
3. Use TypeScript for type safety
4. Log all webhook events for debugging
5. Implement retry logic for external service calls
6. Consider webhook replay attacks - use timestamps
7. Store raw webhook payloads for audit trail

Remember: This service is critical for the business. Make it resilient, well-logged, and easy to monitor.
