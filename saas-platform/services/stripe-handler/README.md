# MindRoom Stripe Handler Service

This service handles all Stripe webhook events for the MindRoom SaaS platform, managing subscriptions, provisioning instances, and coordinating with other services.

## 🏗️ Architecture

The Stripe Handler is a critical service that:
- Processes Stripe webhook events for subscription lifecycle management
- Coordinates with Supabase for data persistence
- Calls the Dokku Provisioner service to manage customer instances
- Sends transactional emails for important events

## 📊 Subscription Tiers

| Tier | Price | Agents | Messages/Day | Memory | CPU | Features |
|------|-------|--------|--------------|--------|-----|----------|
| **Free** | $0 | 1 | 100 | 256 MB | 0.25 | Basic tools only |
| **Starter** | $49/mo | 3 | 1,000 | 512 MB | 0.5 | All tools, email support |
| **Professional** | $199/mo | 10 | 10,000 | 2 GB | 2.0 | Priority support, API access |
| **Enterprise** | Custom | Unlimited | Unlimited | 8 GB | 4.0 | SLA, dedicated support |

## 🚀 Quick Start

### Prerequisites
- Node.js 18+
- npm or yarn
- Stripe account with webhook endpoint configured
- Supabase project with required tables
- Dokku provisioner service running

### Installation

1. **Clone and install dependencies:**
```bash
cd services/stripe-handler
npm install
```

2. **Configure environment:**
```bash
cp .env.example .env
# Edit .env with your credentials
```

3. **Set up Stripe webhook:**
   - Go to Stripe Dashboard → Developers → Webhooks
   - Add endpoint: `https://your-domain.com/webhooks/stripe`
   - Select events to listen for (see Event Handling section)
   - Copy the signing secret to `STRIPE_WEBHOOK_SECRET`

4. **Run in development:**
```bash
npm run dev
```

5. **Build for production:**
```bash
npm run build
npm start
```

## 🐳 Docker Deployment

### Build and run with Docker:
```bash
docker build -t mindroom-stripe-handler .
docker run -p 3005:3005 --env-file .env mindroom-stripe-handler
```

### Use Docker Compose:
```bash
# Production mode
docker-compose up

# Development with Stripe CLI forwarding
docker-compose --profile development up

# Testing with mocks
docker-compose --profile testing up
```

## 📨 Event Handling

### Subscription Events
- `customer.subscription.created` - Provisions new instance
- `customer.subscription.updated` - Updates resource limits
- `customer.subscription.deleted` - Deprovisions instance
- `customer.subscription.trial_will_end` - Sends reminder email

### Invoice Events
- `invoice.payment_succeeded` - Activates/reactivates service
- `invoice.payment_failed` - Sends warning, starts grace period
- `invoice.upcoming` - Pre-renewal processing

### Customer Events
- `customer.created` - Creates account record
- `customer.updated` - Updates account information
- `customer.deleted` - Archives account (retention)

## 🔄 Workflow

### New Subscription Flow
1. Customer subscribes via Stripe Checkout/Portal
2. `subscription.created` webhook received
3. Account created/verified in Supabase
4. Instance provisioned via Dokku Provisioner
5. Welcome email sent with instance URL
6. Instance status tracked in database

### Upgrade/Downgrade Flow
1. Customer changes plan in Stripe
2. `subscription.updated` webhook received
3. Resource limits updated in database
4. Dokku instance resources adjusted
5. Confirmation email sent

### Cancellation Flow
1. Customer cancels subscription
2. `subscription.deleted` webhook received
3. Grace period starts (7 days default)
4. Instance marked for deprovisioning
5. Cancellation email sent
6. After grace period: instance deprovisioned

## 🛠️ Development

### Project Structure
```
services/stripe-handler/
├── src/
│   ├── index.ts           # Express server
│   ├── config.ts          # Configuration
│   ├── routes/
│   │   └── webhooks.ts    # Webhook endpoints
│   ├── handlers/          # Event handlers
│   │   ├── subscription.ts
│   │   ├── invoice.ts
│   │   └── customer.ts
│   ├── services/          # External services
│   │   ├── supabase.ts    # Database operations
│   │   ├── provisioner.ts # Instance management
│   │   └── email.ts       # Email notifications
│   └── types/
│       └── index.ts       # TypeScript types
├── tests/                 # Test files
├── Dockerfile
├── docker-compose.yml
└── package.json
```

### Testing Webhooks Locally

Use Stripe CLI to forward webhooks to your local server:

```bash
# Install Stripe CLI
brew install stripe/stripe-cli/stripe

# Login to Stripe
stripe login

# Forward webhooks to local server
stripe listen --forward-to localhost:3005/webhooks/stripe

# Trigger test events
stripe trigger payment_intent.succeeded
stripe trigger customer.subscription.created
```

### Database Schema

Required Supabase tables:

```sql
-- Accounts table
CREATE TABLE accounts (
  id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  stripe_customer_id TEXT UNIQUE NOT NULL,
  email TEXT NOT NULL,
  created_at TIMESTAMP DEFAULT NOW(),
  updated_at TIMESTAMP DEFAULT NOW()
);

-- Subscriptions table
CREATE TABLE subscriptions (
  id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  account_id UUID REFERENCES accounts(id),
  stripe_subscription_id TEXT UNIQUE NOT NULL,
  stripe_price_id TEXT NOT NULL,
  tier TEXT NOT NULL,
  status TEXT NOT NULL,
  max_agents INTEGER NOT NULL,
  max_messages_per_day INTEGER NOT NULL,
  current_period_start TIMESTAMP NOT NULL,
  current_period_end TIMESTAMP NOT NULL,
  trial_ends_at TIMESTAMP,
  cancelled_at TIMESTAMP,
  created_at TIMESTAMP DEFAULT NOW(),
  updated_at TIMESTAMP DEFAULT NOW()
);

-- Instances table
CREATE TABLE instances (
  id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  subscription_id UUID REFERENCES subscriptions(id),
  dokku_app_name TEXT UNIQUE NOT NULL,
  subdomain TEXT UNIQUE NOT NULL,
  status TEXT NOT NULL,
  frontend_url TEXT NOT NULL,
  backend_url TEXT NOT NULL,
  memory_limit_mb INTEGER NOT NULL,
  cpu_limit DECIMAL NOT NULL,
  deprovisioned_at TIMESTAMP,
  created_at TIMESTAMP DEFAULT NOW(),
  updated_at TIMESTAMP DEFAULT NOW()
);

-- Usage records table
CREATE TABLE usage_records (
  id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  subscription_id UUID REFERENCES subscriptions(id),
  date DATE NOT NULL,
  messages_sent INTEGER DEFAULT 0,
  agents_active INTEGER DEFAULT 0,
  api_calls INTEGER DEFAULT 0,
  created_at TIMESTAMP DEFAULT NOW()
);

-- Webhook events table (for idempotency)
CREATE TABLE webhook_events (
  id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  stripe_event_id TEXT UNIQUE NOT NULL,
  event_type TEXT NOT NULL,
  payload JSONB NOT NULL,
  processed_at TIMESTAMP,
  error TEXT,
  created_at TIMESTAMP DEFAULT NOW()
);
```

## 🔒 Security

### Webhook Verification
All incoming webhooks are verified using Stripe's signature verification to ensure they're authentic.

### Idempotency
Events are tracked in the database to prevent duplicate processing if Stripe retries webhooks.

### API Key Security
- Use environment variables for sensitive data
- Never commit `.env` files
- Rotate keys regularly
- Use Supabase service keys only for admin operations

### Grace Periods
Failed payments trigger a 7-day grace period before deprovisioning, giving customers time to update payment methods.

## 📊 Monitoring

### Health Check
```bash
curl http://localhost:3005/health
```

### Metrics to Monitor
- Webhook processing success rate
- Provisioning success rate
- Average provisioning time
- Payment failure rate
- Email delivery rate

### Logging
The service logs all webhook events with correlation IDs for debugging:
- Successful operations: INFO level
- Failures: ERROR level with stack traces
- Webhook signatures: DEBUG level

## 🚨 Error Handling

### Retry Logic
- Failed provisioning: 3 retries with exponential backoff
- Email failures: Non-blocking, logged but don't fail webhook
- Database errors: Return 500 to trigger Stripe retry

### Failure Scenarios
1. **Provisioner unavailable**: Queue for retry, notify admin
2. **Database down**: Return 500, Stripe will retry
3. **Invalid webhook**: Return 400, log for investigation
4. **Email service down**: Log error, continue processing

## 📝 Environment Variables

See `.env.example` for all configuration options. Key variables:

- `STRIPE_SECRET_KEY` - Your Stripe API key
- `STRIPE_WEBHOOK_SECRET` - Webhook endpoint signing secret
- `SUPABASE_URL` - Your Supabase project URL
- `SUPABASE_SERVICE_KEY` - Service role key for admin access
- `DOKKU_PROVISIONER_URL` - Provisioner service endpoint
- `RESEND_API_KEY` - Email service API key

## 🤝 Integration Points

### Dokku Provisioner API
- `POST /provision` - Create new instance
- `DELETE /deprovision` - Remove instance
- `PUT /update-limits` - Update resource limits

### Supabase Operations
- Account CRUD operations
- Subscription tracking
- Instance management
- Usage recording
- Webhook event logging

### Email Templates
- Welcome email
- Trial ending reminder
- Payment failed warning
- Subscription cancelled confirmation
- Tier upgrade/downgrade notification

## 📚 Additional Resources

- [Stripe Webhook Documentation](https://stripe.com/docs/webhooks)
- [Stripe API Reference](https://stripe.com/docs/api)
- [Supabase JavaScript Client](https://supabase.com/docs/reference/javascript)
- [MindRoom Documentation](../../README.md)

## 📄 License

This service is part of the MindRoom platform. See the main project LICENSE for details.
