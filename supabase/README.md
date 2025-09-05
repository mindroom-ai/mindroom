# MindRoom SaaS Platform - Supabase Database Layer

This directory contains the Supabase configuration for the MindRoom SaaS platform, transforming MindRoom from a single-user self-hosted solution into a multi-tenant SaaS platform.

## Architecture Overview

The MindRoom SaaS platform uses:
- **Supabase** for authentication, database, and real-time updates
- **Stripe** for subscription billing and payment processing
- **Dokku** for container orchestration and instance deployment
- **Matrix/Conduit** for the messaging protocol

## Database Schema

### Core Tables

1. **accounts** - Customer account information
   - Links to Stripe customer IDs
   - Stores profile information

2. **subscriptions** - Subscription records
   - Tracks billing tiers (free, starter, professional, enterprise)
   - Enforces usage limits (messages, storage, agents)
   - Links to Stripe subscriptions

3. **instances** - MindRoom deployments
   - Each customer gets isolated instances
   - Stores Dokku app configuration
   - Tracks health and resource usage

4. **usage_metrics** - Daily usage tracking
   - Message counts per agent/tool
   - Storage consumption
   - Platform activity

5. **audit_logs** - Event tracking
   - All significant actions logged
   - Used for debugging and compliance

6. **support_tickets** - Customer support
   - Priority support for paid tiers
   - Issue tracking and resolution

7. **instance_backups** - Data backups
   - Automatic and manual backups
   - Retention policies

8. **api_keys** - API access management
   - Programmatic access to instances
   - Permission scoping

## Subscription Tiers

### Free Tier
- 1 AI agent
- 100 messages/day
- 1 GB storage
- 1 platform bridge
- Basic features

### Starter ($29/month)
- 3 AI agents
- 1,000 messages/day
- 5 GB storage
- 3 platform bridges
- API access
- Voice messages
- File uploads

### Professional ($99/month)
- 10 AI agents
- 10,000 messages/day
- 50 GB storage
- 10 platform bridges
- Custom agents
- Priority support
- Team collaboration

### Enterprise (Custom)
- Unlimited agents
- Unlimited messages
- Unlimited storage
- All platforms
- All features
- SLA guarantee

## Edge Functions

### 1. handle-stripe-webhook
Processes Stripe webhook events:
- Customer creation
- Subscription lifecycle (created, updated, cancelled)
- Payment events
- Automatically provisions/deprovisions instances

### 2. provision-instance
Creates new MindRoom instances:
- Calls Dokku provisioner service
- Sets up Matrix server
- Configures environment variables
- Sends welcome email

### 3. deprovision-instance
Removes instances:
- Creates backup before deletion
- Cleans up Dokku resources
- Updates database status
- Sends cancellation notice

### 4. check-instance-health
Monitors instance health:
- Checks backend, frontend, Matrix endpoints
- Updates health status
- Sends alerts for critical issues
- Calculates uptime metrics

## Setup Instructions

### Prerequisites

1. Node.js 18+ and npm/pnpm
2. Supabase CLI: `npm install -g supabase`
3. Docker (for local development)

### Local Development

1. **Install dependencies**:
   ```bash
   npm install -g supabase
   ```

2. **Copy environment variables**:
   ```bash
   cp supabase/.env.example supabase/.env.local
   ```

3. **Start Supabase locally**:
   ```bash
   npx supabase start
   ```
   This will start:
   - PostgreSQL database (port 54322)
   - Auth service (port 54321)
   - Storage service
   - Edge Functions runtime
   - Studio UI (port 54323)

4. **Run migrations**:
   ```bash
   npx supabase db reset
   ```
   This will:
   - Drop existing schema
   - Run all migrations
   - Seed test data

5. **Access Supabase Studio**:
   ```
   http://localhost:54323
   ```

6. **Test Edge Functions**:
   ```bash
   npx supabase functions serve
   ```

### Production Deployment

1. **Create Supabase project**:
   - Go to https://app.supabase.com
   - Create new project
   - Copy project URL and keys

2. **Link to project**:
   ```bash
   npx supabase link --project-ref your-project-id
   ```

3. **Push database schema**:
   ```bash
   npx supabase db push
   ```

4. **Deploy Edge Functions**:
   ```bash
   npx supabase functions deploy
   ```

5. **Configure Stripe webhooks**:
   - In Stripe dashboard, add webhook endpoint:
   - URL: `https://your-project.supabase.co/functions/v1/handle-stripe-webhook`
   - Events: All subscription and payment events

## Database Functions

### User Management
- `get_user_instance(user_id)` - Get active instance for user
- `get_all_user_instances(user_id)` - List all user instances
- `has_active_subscription(user_id)` - Check subscription status
- `check_user_permission(user_id, permission)` - Verify feature access

### Usage Tracking
- `track_usage(instance_id, agent, tool, platform)` - Record usage
- `check_usage_limits(instance_id)` - Verify within limits
- `get_billing_metrics(account_id, start_date, end_date)` - Billing data

### Instance Management
- `provision_instance(subscription_id, config)` - Create new instance
- `deprovision_instance(instance_id, reason)` - Remove instance
- `update_instance_health(instance_id, status, details)` - Health updates

### Automation
- `reset_daily_usage()` - Reset daily counters
- `calculate_storage_usage(instance_id)` - Update storage metrics
- `cleanup_expired_data()` - Remove old records
- `auto_pause_inactive_instances()` - Pause unused free tier instances

## Row Level Security (RLS)

All tables have RLS enabled with policies ensuring:
- Users can only see their own data
- Service role has full access for backend operations
- Subscriptions can only be modified via Stripe webhooks
- Audit logs are read-only for users

## Testing

### Run tests locally:
```bash
# Start local Supabase
npx supabase start

# Run test suite
npm test

# Test specific Edge Function
npx supabase functions serve handle-stripe-webhook --no-verify-jwt
curl -X POST http://localhost:54321/functions/v1/handle-stripe-webhook \
  -H "Content-Type: application/json" \
  -d '{"type": "customer.created", "data": {"object": {"id": "cus_test", "email": "test@example.com"}}}'
```

### Seed data
The `seed.sql` file contains test data for:
- 4 test accounts (free, starter, pro, enterprise)
- Running instances with different configurations
- Usage metrics for the past week
- Sample audit logs

## Monitoring

### Health Checks
- Automated health checks run every 5 minutes
- Critical issues trigger email alerts
- Uptime percentage tracked per instance

### Usage Monitoring
- Daily usage metrics collected
- Rate limiting enforced automatically
- Storage limits checked in real-time

### Audit Trail
- All significant events logged
- 90-day retention for audit logs
- Searchable by account, instance, or action

## Backup & Recovery

### Automatic Backups
- Daily backups for paid tiers
- 30-day retention
- Stored in S3-compatible storage

### Manual Backups
- Available via API or dashboard
- Created before major operations
- Downloadable by users

## Security

### Authentication
- Supabase Auth with email verification
- Optional OAuth providers (Google, GitHub)
- API key authentication for programmatic access

### Data Protection
- All data encrypted at rest
- TLS for data in transit
- Row Level Security enforces access control
- Sensitive data (API keys, passwords) hashed

### Rate Limiting
- Per-tier message limits
- API rate limiting
- Automatic throttling for excessive usage

## Support

### Documentation
- API documentation: `/docs/api`
- User guides: `/docs/guides`
- Video tutorials: `/docs/videos`

### Contact
- Email: support@mindroom.app
- Slack: mindroom-community.slack.com
- GitHub Issues: github.com/mindroom/mindroom

## Migration from Self-Hosted

For existing self-hosted MindRoom users:
1. Export configuration from existing instance
2. Create account on MindRoom SaaS
3. Import configuration during instance setup
4. Update Matrix bridges to point to new instance
5. Test agents in new environment

## Troubleshooting

### Common Issues

1. **Instance won't provision**
   - Check Dokku provisioner service is running
   - Verify sufficient resources available
   - Check subscription tier limits

2. **Health checks failing**
   - Verify instance URLs are accessible
   - Check Dokku app status
   - Review instance logs

3. **Usage limits exceeded**
   - Upgrade subscription tier
   - Reset happens at midnight UTC
   - Check current usage in dashboard

4. **Webhook failures**
   - Verify Stripe webhook secret
   - Check Edge Function logs
   - Ensure webhook endpoint is accessible

## Development Roadmap

### Phase 1 (Current)
- ✅ Core database schema
- ✅ Stripe integration
- ✅ Instance provisioning
- ✅ Health monitoring

### Phase 2 (Q1 2025)
- [ ] Team workspaces
- [ ] Advanced analytics
- [ ] Custom domain support
- [ ] Backup scheduling UI

### Phase 3 (Q2 2025)
- [ ] Marketplace for agents
- [ ] White-label options
- [ ] Advanced API features
- [ ] Multi-region deployment

## License

Proprietary - MindRoom SaaS Platform
Copyright (c) 2024 MindRoom Inc.
