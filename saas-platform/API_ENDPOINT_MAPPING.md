# API Endpoint Mapping: Backend to Frontend

This document maps the platform application's OpenAPI method/path operations to their current callers.
The committed [OpenAPI schema](platform-backend/openapi.json) defines the inventory; the source routers in `platform-backend/src/main.py` provide the implementations.
The inventory excludes `/metrics`, which is registered outside the OpenAPI schema.
Framework documentation and schema routes, enabled outside production, are also excluded.
Backend filenames below are relative to `platform-backend/src/backend/routes/`; frontend paths are relative to `platform-frontend/`.

## Summary

- **OpenAPI operations**: 53, counting each HTTP method and path template once.
- **OpenAPI operations called by platform frontend code**: 32, including browser requests and server authentication checks.
- **OpenAPI operations without a direct platform frontend caller**: 21, comprising six system operations, six Matrix OIDC operations, one Stripe webhook, and eight other routes.

Ordinary browser requests go directly to the configured platform API through `src/lib/api.ts`.
The frontend also makes server-side authentication checks in `proxy.ts`, `src/lib/auth/admin.ts`, and `src/app/auth/callback/route.ts`.
An operation without a platform frontend caller can still serve an external integration or an API client.

## Health, Accounts, Subscriptions, and Usage

| Method | Path | Backend module | Frontend caller or purpose |
| --- | --- | --- | --- |
| GET | `/health` | `health.py` | `src/app/admin/page.tsx`: system health indicator |
| GET | `/my/account` | `accounts.py` | `src/lib/api.ts` → settings; `src/lib/auth/admin.ts` → admin account details |
| GET | `/my/account/admin-status` | `accounts.py` | `proxy.ts`, `src/lib/auth/admin.ts`, and `src/app/auth/callback/route.ts`: server admin checks |
| POST | `/my/account/setup` | `accounts.py` | `src/lib/api.ts` → `src/app/dashboard/page.tsx`: account setup |
| GET | `/my/subscription` | `subscriptions.py` | `src/hooks/useSubscription.ts`: subscription details |
| POST | `/my/subscription/cancel` | `subscriptions.py` | No current frontend caller; cancel a subscription |
| POST | `/my/subscription/reactivate` | `subscriptions.py` | No current frontend caller; reactivate a subscription |
| GET | `/my/usage` | `usage.py` | `src/hooks/useUsage.ts`: usage metrics with a days parameter |

## Customer Instances

| Method | Path | Backend module | Frontend caller |
| --- | --- | --- | --- |
| GET | `/my/instances` | `instances.py` | `src/lib/api.ts` → `src/lib/instance-resource.ts`: shared instance loading |
| POST | `/my/instances/provision` | `instances.py` | `src/lib/api.ts` → `src/components/dashboard/InstanceCard.tsx` and `src/app/dashboard/instance/page.tsx` |
| POST | `/my/instances/{instance_id}/start` | `instances.py` | `src/lib/api.ts` → `src/app/dashboard/instance/page.tsx` |
| POST | `/my/instances/{instance_id}/stop` | `instances.py` | `src/lib/api.ts` → `src/app/dashboard/instance/page.tsx` |
| POST | `/my/instances/{instance_id}/restart` | `instances.py` | `src/lib/api.ts` → `src/app/dashboard/instance/page.tsx` and `src/hooks/useInstance.ts` |

## Admin Operations

| Method | Path | Backend module | Frontend caller or purpose |
| --- | --- | --- | --- |
| GET | `/admin/stats` | `admin.py` | `src/app/admin/page.tsx`: platform statistics |
| GET | `/admin/metrics/dashboard` | `admin.py` | `src/app/admin/page.tsx`: dashboard metrics |
| POST | `/admin/instances/{instance_id}/start` | `admin.py` | `src/components/admin/InstanceActions.tsx`: start |
| POST | `/admin/instances/{instance_id}/stop` | `admin.py` | `src/components/admin/InstanceActions.tsx`: stop |
| POST | `/admin/instances/{instance_id}/restart` | `admin.py` | `src/components/admin/InstanceActions.tsx`: restart |
| DELETE | `/admin/instances/{instance_id}/uninstall` | `admin.py` | `src/components/admin/InstanceActions.tsx`: uninstall |
| POST | `/admin/instances/{instance_id}/provision` | `admin.py` | `src/components/admin/InstanceActions.tsx`: reprovision |
| POST | `/admin/sync-instances` | `admin.py` | `src/app/admin/instances/page.tsx`: synchronize Kubernetes and database state |
| GET | `/admin/accounts/{account_id}` | `admin.py` | `src/app/admin/accounts/[id]/page.tsx`: account details |
| PUT | `/admin/accounts/{account_id}/status` | `admin.py` | `src/app/admin/accounts/page.tsx`: status control |
| DELETE | `/admin/accounts/{account_id}/complete` | `admin.py` | `src/app/admin/accounts/page.tsx`: complete account deletion |
| GET | `/admin/{resource}` | `admin.py` | `src/app/admin/{accounts,subscriptions,instances,audit-logs,usage}/page.tsx`: list resources |
| GET | `/admin/{resource}/{resource_id}` | `admin.py` | No current frontend caller; generic record lookup |
| POST | `/admin/{resource}` | `admin.py` | No current frontend caller; generic record creation |
| PUT | `/admin/{resource}/{resource_id}` | `admin.py` | No current frontend caller; generic record update |
| DELETE | `/admin/{resource}/{resource_id}` | `admin.py` | No current frontend caller; generic record deletion |
| POST | `/admin/auth/logout` | `admin.py` | No current frontend caller; logout placeholder |

The generic list callers use `accounts`, `subscriptions`, `instances`, `audit_logs`, and `usage_metrics` as resource values.
Account detail requests use the specific `/admin/accounts/{account_id}` route, registered before generic record lookup.
The generic CRUD API retains its React Admin-compatible response shapes; the current UI uses custom React components.

## System Provisioner

These operations are for provisioner clients and have no direct platform frontend caller.

| Method | Path | Backend module | Purpose |
| --- | --- | --- | --- |
| POST | `/system/provision` | `provisioner.py` | Provision an instance |
| POST | `/system/instances/{instance_id}/start` | `provisioner.py` | Start an instance |
| POST | `/system/instances/{instance_id}/stop` | `provisioner.py` | Stop an instance |
| POST | `/system/instances/{instance_id}/restart` | `provisioner.py` | Restart an instance |
| DELETE | `/system/instances/{instance_id}/uninstall` | `provisioner.py` | Uninstall an instance |
| POST | `/system/sync-instances` | `provisioner.py` | Synchronize Kubernetes and database state |

Admin lifecycle routes verify the Supabase user and `accounts.is_admin` through `verify_admin`, call `backend/services/provisioner_service.py` directly, and record the action in the audit log.
System routes separately validate the provisioner bearer key before calling that same service.
There is no admin-to-system HTTP proxy hop.
The shared service owns the Kubernetes and Helm lifecycle work.

## Pricing, Stripe, and Privacy

| Method | Path | Backend module | Frontend caller or purpose |
| --- | --- | --- | --- |
| GET | `/pricing/config` | `pricing.py` | `src/lib/api.ts` → billing and upgrade pages |
| GET | `/pricing/stripe-price/{plan}/{billing_cycle}` | `pricing.py` | No current frontend caller; look up one Stripe price ID |
| POST | `/stripe/checkout` | `stripe_routes.py` | `src/lib/api.ts` → `src/app/dashboard/billing/upgrade/page.tsx` |
| POST | `/stripe/portal` | `stripe_routes.py` | `src/lib/api.ts` → `src/app/dashboard/billing/page.tsx` |
| POST | `/webhooks/stripe` | `webhooks.py` | External Stripe webhook; no platform frontend caller |
| GET | `/my/gdpr/export-data` | `gdpr.py` | `src/lib/api.ts` → `src/app/dashboard/settings/page.tsx`: data export |
| POST | `/my/gdpr/request-deletion` | `gdpr.py` | `src/lib/api.ts` → `src/app/dashboard/settings/page.tsx`: request account deletion |
| POST | `/my/gdpr/cancel-deletion` | `gdpr.py` | `src/lib/api.ts` → `src/app/dashboard/settings/page.tsx`: cancel deletion |
| POST | `/my/gdpr/consent` | `gdpr.py` | `src/lib/api.ts` → `src/app/dashboard/settings/page.tsx`: consent preferences |

## SSO and Matrix OIDC

| Method | Path | Backend module | Frontend caller or purpose |
| --- | --- | --- | --- |
| POST | `/my/sso-cookie` | `sso.py` | `src/lib/api.ts` → auth completion and dashboard: set the SSO cookie |
| DELETE | `/my/sso-cookie` | `sso.py` | `src/lib/api.ts` → `src/hooks/useAuth.tsx`: clear the SSO cookie |
| GET | `/matrix-oidc/.well-known/openid-configuration` | `matrix_oidc.py` | OIDC discovery for Synapse |
| GET | `/.well-known/openid-configuration/matrix-oidc` | `matrix_oidc.py` | Alternate OIDC discovery path |
| GET | `/matrix-oidc/jwks.json` | `matrix_oidc.py` | Public signing keys for the OIDC client |
| GET | `/matrix-oidc/authorize` | `matrix_oidc.py` | Synapse browser redirect flow using platform-cookie authentication |
| POST | `/matrix-oidc/token` | `matrix_oidc.py` | Synapse authorization-code exchange with client authentication |
| GET | `/matrix-oidc/userinfo` | `matrix_oidc.py` | Synapse user-info request with an OIDC access token |

The six Matrix OIDC operations participate in the Matrix login flow rather than direct platform frontend API calls.
They are registered even when OIDC is disabled; their handlers check whether the integration is enabled.

## Maintaining the Map

Compare method/path pairs against the registered routers and committed OpenAPI schema after route changes.
Trace both frontend request helpers and their consuming pages, hooks, or components before counting an operation as used.
Count generic route templates once, even when several resource values use them.
Health indicators, admin instance controls, and admin metrics already have frontend callers listed above.
