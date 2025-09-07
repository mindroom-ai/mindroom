# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Architecture Overview

MindRoom SaaS Platform is a Kubernetes-based multi-tenant platform for hosting MindRoom instances. The platform uses:
- **Frontend**: Next.js (customer-portal) and React Admin (admin-dashboard)
- **Backend**: Python FastAPI (provisioners) and Node.js (stripe-handler)
- **Infrastructure**: Kubernetes on Hetzner Cloud (managed via Terraform)
- **Database**: Supabase (PostgreSQL with Row-Level Security)
- **Authentication**: Supabase Auth with OAuth providers (GitHub, Google)
- **Payments**: Stripe for subscriptions and billing

### Service Communication Flow
```
User → app.staging.mindroom.chat → K8s Ingress → customer-portal
                                              ↓
                                    Supabase Auth → OAuth Providers
                                              ↓
                                    Stripe Handler → Provisioner → K8s API
```

## Common Development Commands

### Local Development
```bash
# Customer Portal (Next.js)
cd apps/customer-portal
pnpm install
pnpm run dev  # Runs on localhost:3000

# Admin Dashboard (React Admin)
cd apps/admin-dashboard
pnpm install
pnpm run dev  # Runs on localhost:5173

# Stripe Handler
cd services/stripe-handler
pnpm install
pnpm run dev  # Runs on localhost:3005

# Instance Provisioner
cd services/instance-provisioner
python -m venv venv
source venv/bin/activate
pip install -r requirements.txt
python app.py  # Runs on port 8002
```

### Deployment
```bash
# Deploy a specific service to K8s
./deploy.sh customer-portal
./deploy.sh admin-dashboard
./deploy.sh stripe-handler
./deploy.sh instance-provisioner

# Force K8s to pull new images (uses rollout restart)
cd terraform-k8s
kubectl --kubeconfig=./mindroom-k8s_kubeconfig.yaml rollout restart deployment/customer-portal -n mindroom-staging
```

### Database Setup
```bash
# Migrations are in supabase/migrations/
# Apply them manually in Supabase Dashboard SQL Editor

# Optional: Setup Stripe products
node scripts/db/setup-stripe-products.js

# Optional: Create admin user
node scripts/db/create-admin-user.js
```

## Critical Configuration Points

### Authentication Issues
The authentication flow relies on proper Supabase configuration:
1. **Site URL** in Supabase Dashboard must match deployment URL (e.g., `https://app.staging.mindroom.chat`)
2. **Redirect URLs** must include all valid callback URLs
3. OAuth providers need correct callback URLs: `https://[supabase-project].supabase.co/auth/v1/callback`

### Environment Variables
The platform uses build-time and runtime environment variables:
- **Build-time** (Next.js): `NEXT_PUBLIC_*` variables are baked into the Docker image
- **Runtime**: Server-side variables from K8s secrets

Key environment mappings in K8s:
```yaml
NEXT_PUBLIC_APP_URL: https://app.{{ .Values.domain }}
NEXT_PUBLIC_SUPABASE_URL: {{ .Values.supabase.url }}
NEXT_PUBLIC_SUPABASE_ANON_KEY: {{ .Values.supabase.anonKey }}
```

### Service Dependencies
- **customer-portal**: Depends on Supabase Auth, requires correct redirect URLs
- **stripe-handler**: Needs webhook secret and Supabase service key
- **provisioners**: Require K8s RBAC permissions to create instances

## Key Architecture Decisions

### Multi-Environment Strategy
- **staging**: Uses `staging.mindroom.chat` with test Stripe keys
- **production**: Uses `mindroom.chat` with live Stripe keys
- Both environments share K8s cluster but use different namespaces

### Instance Provisioning
The platform uses a Kubernetes-native provisioner (`instance-provisioner`) that:
- Deploys customer instances using Helm charts
- Manages instance lifecycle (provision/deprovision)
- Runs as a service in the Kubernetes cluster

### Authentication Flow
1. User visits `/auth/login`
2. Supabase Auth UI handles OAuth flow
3. OAuth provider redirects to Supabase callback
4. Supabase redirects to app's `/auth/callback`
5. App exchanges code for session
6. User redirected to `/dashboard`

### Database Schema
Core tables:
- `accounts`: Customer accounts with subscription info
- `instances`: MindRoom instances per account
- `usage_metrics`: Daily usage tracking
- `audit_logs`: Platform activity logging
- `subscriptions`: Stripe subscription tracking

All tables use Row-Level Security (RLS) policies for data isolation.

## Common Issues and Solutions

### OAuth Redirect to localhost:3000
**Cause**: Supabase Site URL set to `http://localhost:3000`
**Fix**: Update Site URL in Supabase Dashboard → Authentication → URL Configuration

### Docker Build Failures
**Cause**: Next.js needs environment variables at build time
**Fix**: Pass build args in deploy.sh:
```bash
--build-arg NEXT_PUBLIC_SUPABASE_URL=$SUPABASE_URL
--build-arg NEXT_PUBLIC_SUPABASE_ANON_KEY=$SUPABASE_ANON_KEY
--build-arg NEXT_PUBLIC_APP_URL=https://app.staging.mindroom.chat
```

### K8s Deployment Not Updating
**Cause**: K8s caches images with `:latest` tag
**Fix**: Force rollout restart after pushing new image:
```bash
kubectl rollout restart deployment/[app-name] -n mindroom-staging
```

## Testing

### Local Testing with Stripe
```bash
# Install Stripe CLI
# Forward webhooks to local handler
stripe listen --forward-to localhost:3005/webhooks

# Trigger test events
stripe trigger payment_intent.succeeded
```

### E2E Testing
```bash
cd apps/customer-portal
pnpm test:e2e

cd apps/admin-dashboard
pnpm test:e2e
```

## Security Considerations

- Never commit secrets to git
- Use K8s secrets for sensitive configuration
- Validate Stripe webhook signatures
- Enable RLS policies on all Supabase tables
- Use service role key only in backend services
