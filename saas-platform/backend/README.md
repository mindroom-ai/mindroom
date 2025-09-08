# MindRoom Backend (Simplified)

## Overview
Single-file FastAPI backend that handles:
- Admin API endpoints (for customer portal admin interface)
- Dashboard metrics
- Instance management (start/stop/restart)
- Stripe webhooks
- Admin authentication via Supabase with is_admin flag

## Setup

### Environment Variables
```env
# Supabase
SUPABASE_URL=https://xxx.supabase.co
SUPABASE_SERVICE_KEY=xxx

# Stripe
STRIPE_SECRET_KEY=sk_test_xxx
STRIPE_WEBHOOK_SECRET=whsec_xxx

# Admin
ADMIN_EMAIL=admin@mindroom.chat
ADMIN_PASSWORD=your-password
```

### Local Development
```bash
# Using Docker Compose
docker-compose up

# Or directly with Python
pip install -r requirements.txt
uvicorn backend:app --reload
```

### Production Deployment
```bash
# Build Docker image
docker build -t platform-backend .

# Deploy to Kubernetes
kubectl apply -f k8s/backend.yaml
```

## API Endpoints

### Admin API
- `POST /api/admin/auth/logout` - Admin logout
- `GET /api/admin/{resource}` - List records (for admin interface)
- `GET /api/admin/{resource}/{id}` - Get single record
- `POST /api/admin/{resource}` - Create record
- `PUT /api/admin/{resource}/{id}` - Update record
- `DELETE /api/admin/{resource}/{id}` - Delete record

### Metrics
- `GET /api/admin/metrics/dashboard` - Dashboard metrics

### Instance Management
- `POST /api/admin/instances/{id}/start` - Start instance
- `POST /api/admin/instances/{id}/stop` - Stop instance
- `POST /api/admin/instances/{id}/restart` - Restart instance

### Webhooks
- `POST /webhooks/stripe` - Stripe webhook handler

### Health
- `GET /health` - Health check

## Architecture

```
backend.py (400 lines)
├── Supabase client (database)
├── Stripe client (payments)
├── FastAPI routes
│   ├── Admin auth (simple)
│   ├── React Admin CRUD
│   ├── Dashboard metrics
│   ├── Instance control (kubectl)
│   └── Stripe webhooks
└── Static file serving (production)
```

## Simplifications Made

1. **No JWT** - Simple admin auth with static credentials
2. **No complex routers** - Everything in one file
3. **No provisioning complexity** - Just kubectl commands
4. **No webhook storage** - Process and forget
5. **No user auth** - Customer portal uses Supabase directly
6. **Single Dockerfile** - One simple container

This is perfect for a solo developer who wants to focus on the product, not infrastructure complexity.
