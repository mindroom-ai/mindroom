# Platform Components

## Platform Backend

FastAPI service that provides:
- Admin API for customer management
- Instance lifecycle management (start/stop/restart)
- Stripe webhook processing
- Kubernetes integration for deployments
- Dashboard metrics and monitoring

### Key Design Decisions
- Single-file architecture for simplicity
- Direct kubectl commands for instance management
- Stateless design (all state in database)
- Admin authentication via Supabase JWT

## Platform Frontend

Next.js application serving:
- Customer self-service portal
- Admin dashboard (React Admin)
- Account management
- Billing and subscription UI
- Instance configuration

### Architecture Patterns
- Server-side rendering for performance
- API routes for backend communication
- Supabase client for authentication
- Responsive design with Tailwind CSS

## Customer Instances

Each MindRoom instance consists of:
- **Backend Container**: Runs bot and API server (port 8765)
- **Frontend Container**: Serves web UI (port 3003)
- **Persistent Storage**: Config files and conversation data
- **Environment Isolation**: Separate namespace and secrets

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
- **webhooks**: Payment event tracking

### Key Relationships
- One account can have multiple instances
- Each instance has one active subscription
- Webhook events linked to subscriptions

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
- Usage-based billing support
- Webhook event processing

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
