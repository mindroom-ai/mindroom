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

Modular FastAPI application with a thin entrypoint (`main.py`) that includes routers defined under `backend/`:

- `backend/config.py` – env, clients, and settings
- `backend/deps.py` – shared auth dependencies
- `backend/k8s.py` – Kubernetes helpers
- `backend/routes/*` – route modules (accounts, admin, instances, etc.)

### API Structure

- `/admin/*` - Admin CRUD operations (React Admin compatible)
- `/admin/metrics/*` - Dashboard and monitoring endpoints
- `/admin/instances/*` - Instance control (start/stop/restart)
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

Configure the environment variables below, then run from the repository root:

```bash
cd saas-platform/platform-backend
uv sync --all-extras
uv run uvicorn main:app --reload --host 127.0.0.1 --port 8000
```

The installed project exposes `src/main.py` as `main`; this command serves port 8000 with development reload enabled.

## Environment Variables

Requires:
- `SUPABASE_URL` - Supabase project URL
- `SUPABASE_SERVICE_KEY` - Service role key for admin operations
- `STRIPE_SECRET_KEY` - Stripe API key
- `STRIPE_WEBHOOK_SECRET` - Webhook endpoint secret
- Optional: `ENABLE_CLEANUP_SCHEDULER=true` to enable the daily GDPR cleanup job (runs at 03:00 UTC)
