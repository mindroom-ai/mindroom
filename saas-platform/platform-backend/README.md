# Platform Backend

FastAPI backend service for the MindRoom SaaS platform.

## Purpose

Provides APIs for:
- Customer portal operations
- Admin dashboard functionality
- Instance management (Kubernetes)
- Stripe webhook processing
- Health monitoring

## Architecture

Single-file FastAPI application (`main.py`) designed for simplicity and maintainability.

### API Structure

- `/api/admin/*` - Admin CRUD operations (React Admin compatible)
- `/api/admin/metrics/*` - Dashboard and monitoring endpoints
- `/api/admin/instances/*` - Instance control (start/stop/restart)
- `/webhooks/stripe` - Payment event processing
- `/health` - Service health check

### Authentication

- Uses Supabase JWT tokens for authentication
- Admin access controlled by `is_admin` flag in accounts table
- Service-to-service auth via API keys

### External Integrations

- **Supabase**: Database and authentication
- **Stripe**: Payment processing
- **Kubernetes**: Instance management via kubectl

## Development

Runs on port 8000 by default. Supports hot-reload in development mode.

## Environment Variables

Requires:
- `SUPABASE_URL` - Supabase project URL
- `SUPABASE_SERVICE_KEY` - Service role key for admin operations
- `STRIPE_SECRET_KEY` - Stripe API key
- `STRIPE_WEBHOOK_SECRET` - Webhook endpoint secret
