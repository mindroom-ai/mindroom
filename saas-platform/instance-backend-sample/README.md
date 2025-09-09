# Instance Backend Auth Samples

This folder contains minimal, copy‑pasteable auth helpers for your instance backend.

- `fastapi_auth.py`: FastAPI dependency `verify_user` that validates Supabase JWT from `Authorization: Bearer <jwt>` and enforces that the authenticated user matches the instance owner via `ACCOUNT_ID`.
- `express_auth.js`: Express middleware `verifyUser` that performs the same check.

Environment variables required (injected via Helm during provisioning):

- `SUPABASE_URL`
- `SUPABASE_ANON_KEY`
- `ACCOUNT_ID` (owner of the instance)

Nginx at the instance forwards the `Authorization` header to the backend (`/api`), so your backend can simply read the header and validate it.
