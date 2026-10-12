# Frontend Security Review - MindRoom SaaS Platform

September 2025 review of the SaaS platform portal frontend: CSP and security headers, re-authentication, route authorization, cookies, client-side storage, and Subresource Integrity.
It keeps the reasoning that refutes one reported finding and the findings still open when re-checked against the code on 2026-10-12.

## Refuted finding: development mock user

The review reported the `NEXT_PUBLIC_DEV_AUTH` mock user as a critical authentication bypass and recommended removing it, but it only fakes client-side state.
It applies only when `NODE_ENV` is `development`, and Next.js inlines `NEXT_PUBLIC_` values when the bundle is built, so setting the variable on a running deployment cannot enable it.
The mock user has no Supabase session, so backend API calls made under it carry no valid token and are rejected.

## Open findings

- No step-up authentication: admin pages, instance start, stop, and restart, billing, and account settings run on the same session as the rest of the dashboard, with no password, MFA, or recent-sign-in check, so a stolen session carries full power.
- No session timeout: the platform enforces no idle or absolute session lifetime of its own.
- The platform leaves Supabase session cookie attributes such as `HttpOnly`, `Secure`, and `SameSite` to the SDK, and nobody has audited whether those defaults are adequate.
- The analytics script loads from an external host without Subresource Integrity, so a compromise of that host runs arbitrary script in the portal's origin.
