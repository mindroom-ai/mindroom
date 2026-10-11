# Platform Components

## Platform Backend

FastAPI service that provides:
- Admin API for customer management
- Instance lifecycle management (start/stop/restart)
- Stripe webhook processing
- Kubernetes integration for deployments
- Dashboard metrics and monitoring

### Key Design Decisions
- `platform-backend/src/main.py` composes modular `backend/routes/` and `backend/services/`
- Shared provisioner service uses kubectl and Helm for instance lifecycle operations
- Stateless design (all state in database)
- Admin authentication via Supabase JWT

## Platform Frontend

Next.js application serving:
- Customer self-service portal
- Admin dashboard built with custom Next.js and React components
- Account management
- Billing and subscription UI
- Instance configuration

### Architecture Patterns
- Server-side rendering for performance
- Ordinary browser requests use `platform-frontend/src/lib/api.ts` to call the platform API directly
- Server authentication callback, session validation, admin page guards, and CSP-report route
- Supabase client for authentication
- Responsive design with Tailwind CSS

## Customer Instances

Each MindRoom instance consists of:
- **Backend Container**: Runs bot, serves the bundled dashboard, and exposes the APIs (port 8765)
- **Persistent Storage**: Config files and conversation data
- **Tenant Resources**: Separate releases, workloads, PVCs, and Secrets in the shared `mindroom-instances` namespace

Customer labels and NetworkPolicy rules control the specified instance traffic; tenants do not receive separate Kubernetes namespaces.

### Instance Lifecycle
1. Customer signs up and subscribes
2. Platform provisions Kubernetes resources
3. Instance deployed with unique subdomain
4. SSL certificate automatically generated
5. Customer configures via web portal

## Database Schema

### Core Tables (Supabase)
- **accounts**: User accounts with subscription status
- **instances**: Customer instance configurations
- **subscriptions**: Stripe subscription records
- **webhook_events**: Stripe event payloads and processing status

### Key Relationships
- One account can have multiple instances
- Each instance has one active subscription
- Webhook events optionally reference an account; subscription details can appear in the Stripe payload, without a subscription foreign key

## Infrastructure Components

### Kubernetes Resources
- **Deployments**: Platform services and customer instances
- **Services**: Internal networking and load balancing
- **Ingress**: HTTP routing and SSL termination
- **Secrets**: Environment variables and credentials
- **ConfigMaps**: Instance configurations

### Terraform Management
- Provisions cloud servers
- Configures Kubernetes cluster
- Sets up DNS records
- Deploys platform services
- Manages SSL certificates

## Integration Points

### Stripe Integration
- Subscription creation and management
- Payment method handling
- Fixed monthly/yearly subscriptions with trials for configured plans
- Webhook event processing

Applicable plans use scoped OpenRouter keys configured with a monthly AI spending limit.
Usage reporting is separate from Stripe checkout, which uses one configured recurring price at quantity one.

### Supabase Integration
- User authentication and sessions
- Database operations
- Row-level security policies
- Real-time subscriptions

### Matrix Integration
Each customer instance connects to Matrix for:
- Multi-agent conversations
- Room management
- Message persistence
- User presence tracking
