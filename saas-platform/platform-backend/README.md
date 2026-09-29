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
- Optional: `ENABLE_CLEANUP_SCHEDULER=true` to enable the daily cleanup job (runs at 03:00 UTC): GDPR hard deletes after uninstalling the account's instances (see `docs/deployment/kubernetes.md#account-deletion`), log and metric retention, and the hosted instance lifecycle
- Optional: `INSTANCE_TEARDOWN_GRACE_DAYS` (default `30`) days an instance of an inactive subscription stays stopped before teardown; see `docs/deployment/kubernetes.md#subscription-lifecycle`

## Backfilling Stripe payments

`backend.scripts.backfill_payments` records paid Stripe invoices in the `payments` table with the same row-building code as the `invoice.payment_succeeded` webhook.
Use it when webhook deliveries were lost or failed, for example the invoices paid before the webhook handled the Stripe basil invoice shape.
It only considers paid invoices with `amount_paid > 0`, and it skips and reports invoices without a subscription or without a matching account.
The default is a dry run that prints each invoice id, payment date, amount, currency, and account email without writing anything.
`--apply` upserts on `invoice_id`, so re-running it never creates duplicate rows.
Run it inside the platform-backend container, which already has the Stripe and Supabase credentials:

```bash
kubectl -n mindroom-production exec deploy/platform-backend -- python -m backend.scripts.backfill_payments --dry-run
kubectl -n mindroom-production exec deploy/platform-backend -- python -m backend.scripts.backfill_payments --apply
```
