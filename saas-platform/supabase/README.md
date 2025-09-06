# MindRoom SaaS Platform - Database Migrations

This directory contains the database migrations for the MindRoom SaaS platform.

## Contents

- **`migrations/`** - Individual SQL migration files that define the database schema
- **`all-migrations.sql`** - Combined migration file used for production deployment
- **`.env.example`** - Example environment variables for local development

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

## Deployment

The database migrations are applied to a hosted Supabase instance during deployment:

```bash
# Migrations are run via scripts/database/run-migrations.sh
./scripts/database/run-migrations.sh
```

This script:
1. Copies `all-migrations.sql` to the platform server
2. Runs the migrations via SSH tunnel using PostgreSQL client
3. Verifies the tables were created successfully

## Row Level Security (RLS)

All tables have RLS enabled with policies ensuring:
- Users can only see their own data
- Service role has full access for backend operations
- Subscriptions can only be modified via Stripe webhooks
- Audit logs are read-only for users

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

## Note on Edge Functions

The platform uses dedicated Node.js services for webhook handling and instance management, not Supabase Edge Functions. The services are:
- **stripe-handler** - Processes Stripe webhooks
- **instance-provisioner** - Manages instance provisioning using Kubernetes/Helm
- **customer-portal** - Customer-facing web interface
- **admin-dashboard** - Administrative interface
