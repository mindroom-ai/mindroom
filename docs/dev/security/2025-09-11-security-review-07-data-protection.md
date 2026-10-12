# Security Review: Data Protection & Privacy

September 2025 review of how the SaaS platform protects customer personal data: encryption, logging, payment data, deletion, GDPR flows, and retention.
It keeps the payment-data invariant and the findings still open when re-checked against the code on 2026-10-12.

## Invariants

- Card data must never reach platform servers: checkout is hosted by Stripe, so card handling stays isolated with Stripe.

## Open findings

### PII has no encryption beyond the database's own

Without column-level encryption, personal data, including Stripe webhook payloads that can carry invoice contact details, depends entirely on the hosted Supabase database for encryption at rest, and no evidence that it is enabled has been recorded.

### Compliance work outside the code

The repository has no breach-notification procedure, data protection impact assessment, or documented lawful basis for processing.
The GDPR export, deletion, and consent endpoints do not replace them.

Log redaction gaps are recorded in [09 error handling](2025-09-11-security-review-09-error-handling.md#log-redaction-is-opt-in).
