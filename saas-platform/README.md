# MindRoom SaaS Platform

## Architecture Overview

MindRoom is a multi-tenant SaaS platform for deploying AI chatbot instances. Each customer gets their own isolated deployment with a unique subdomain.

### Core Components

- **Platform Services**: Customer portal, admin interface, billing integration
- **Customer Instances**: Isolated MindRoom deployments per customer
- **Infrastructure**: Kubernetes cluster managed by Terraform
- **Database**: Supabase for authentication and data storage
- **Payments**: Stripe integration for subscriptions

### Technology Stack

- **Frontend**: Next.js with TypeScript
- **Backend**: FastAPI (Python)
- **Infrastructure**: Kubernetes (K3s), Terraform, Helm
- **Hosting**: Hetzner Cloud
- **Authentication**: Supabase Auth with JWT
- **DNS/SSL**: Automated cert-manager with Let's Encrypt

### Domain Architecture

```
app.mindroom.chat         → Platform customer portal
api.mindroom.chat         → Platform API
*.mindroom.chat          → Customer instances (e.g., acme.mindroom.chat)
```

### Deployment Environments

- **Local**: Docker Compose for development
- **Staging**: Testing environment with `.staging.mindroom.chat` domains
- **Production**: Live customer deployments

## Repository Structure

```
saas-platform/
├── platform-backend/     # FastAPI backend service
├── platform-frontend/    # Next.js customer portal
├── k8s/                 # Kubernetes Helm charts
│   ├── platform/        # Platform services chart
│   └── instance/        # Customer instance template
├── terraform-k8s/       # Infrastructure as code
└── docker-compose.yml   # Local development
```

## Key Concepts

### Multi-tenancy
Each customer instance runs in isolation with:
- Dedicated subdomain and SSL certificate
- Separate database namespace
- Independent resource allocation
- Isolated Matrix rooms

### Service Communication
- Frontend communicates with backend via REST API
- Backend manages Kubernetes deployments
- Customer instances are fully autonomous
- Platform monitors instance health

### Security Model
- JWT-based authentication via Supabase
- Admin access controlled by `is_admin` flag in database
- API keys for service-to-service communication
- Network isolation between customer instances

For a detailed explanation of the end-to-end authentication flow across the platform (customer portal) and per-instance deployments (nginx sidecar + backend JWT verification), see docs/authentication.md.
