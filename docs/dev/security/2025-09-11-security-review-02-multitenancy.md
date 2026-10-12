# MindRoom Security Review: Multi-Tenancy & Data Isolation

September 2025 review of tenant isolation in the SaaS platform's Supabase database and backend API.
Findings that the current schema and code fix are omitted.

## Invariants

- Tenant isolation has two layers that guard different paths.
  RLS policies guard direct database API access made with the public anon key and a user's JWT.
  The backend's service-role client bypasses RLS, so every backend query made for a tenant's request must be scoped to that tenant's account in code (admin routes, gated by `verify_admin`, deliberately span tenants), and the service-role key must never reach client-side code.

## Open

- No test applies the RLS policies against PostgreSQL as one tenant and checks that another tenant's rows stay hidden; the multitenancy tests mock the Supabase client, so a schema change could regress isolation unnoticed.
- Tenants cannot see their own audit logs; a per-tenant view that keeps admin oversight was suggested and is optional.
