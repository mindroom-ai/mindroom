# MindRoom API Security Review - Category 10

September 2025 review of the SaaS platform backend's API protections: rate limiting, request size, file uploads, CAPTCHA, versioning, internal API separation, and API key management.
It keeps the findings still open when re-checked against the code on 2026-10-12.

## Open findings

### No CAPTCHA or bot check on high-risk flows

Account setup and Stripe checkout have per-IP rate limits but no CAPTCHA or other bot check, so automated account creation and checkout attempts spread across many addresses are not stopped.

### No API versioning or deprecation path

Routes are unversioned and change with each deployment, so an endpoint whose contract proves insecure cannot be deprecated while older clients still use it; it can only be changed for every client at once.

### Provisioner API key has no rotation path

The internal `/system/*` endpoints accept a single shared `PROVISIONER_API_KEY`, so rotating it is a hard cutover with no overlap window.
There is no rotation schedule and no monitoring of the key's usage.
