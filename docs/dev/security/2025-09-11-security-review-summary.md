# MindRoom SaaS Security Review (September 2025)

This is the index of the September 2025 security review of the hosted SaaS platform (`saas-platform/` and `cluster/`), done ahead of the beta and open-source release across twelve categories.
It was a repository review, not a deployment attestation, and its status claims need fresh validation before any production decision.
Its numeric risk scores are not usable evidence, because the review defined no reproducible scoring methodology.
The core MindRoom runtime trust model lives in [Security Posture](../../architecture/security-posture.md), and hosted tenant isolation in [Multi-Tenant Architecture](../../deployment/saas-platform.md#multi-tenant-architecture).

## Category reports

Each report holds its category's assumptions, decisions, and open findings; this page keeps only what spans categories.

[01 auth and tokens](2025-09-11-security-review-01-auth.md), [02 multitenancy](2025-09-11-security-review-02-multitenancy.md), [03 secrets](2025-09-11-security-review-03-secrets.md), [04 injection](2025-09-11-security-review-04-injection.md), [06 infrastructure](2025-09-11-security-review-06-infrastructure.md), [07 data protection](2025-09-11-security-review-07-data-protection.md), [08 dependencies](2025-09-11-security-review-08-dependencies.md), [09 error handling](2025-09-11-security-review-09-error-handling.md), [10 API security](2025-09-11-security-review-10-api-security.md), [11 monitoring](2025-09-11-security-review-11-monitoring.md), and [12 frontend](2025-09-11-security-review-12-frontend.md).

## Decisions

- The review recommended restricted staging with trusted testers and no public exposure until the review set is revalidated and the open findings close.
- The production blockers were, in priority order, a verified rotation of platform secrets with confirmed revocation of the provider keys leaked into the Git history ([03](2025-09-11-security-review-03-secrets.md)), alert routing and incident response ([11](2025-09-11-security-review-11-monitoring.md)), platform pod hardening together with a decision on internal TLS ([06](2025-09-11-security-review-06-infrastructure.md)), and then the remaining open findings in the category reports.
- Internal TLS or mTLS was treated as optional for the first release and a post-launch improvement; the open choice is mTLS or a service mesh versus documented compensating controls.
- The backend caches verified tokens to avoid a Supabase round-trip per request; the resulting revocation lag is an open finding in [01](2025-09-11-security-review-01-auth.md).
- A bug bounty waits until monitoring and incident response are mature, and an external audit or penetration test follows remediation, before public release.

## Assumptions and limitations

- Stripe's hosted checkout processes payments, which keeps the platform's PCI DSS obligations at the reduced hosted-checkout level only as long as card data never reaches platform servers ([07](2025-09-11-security-review-07-data-protection.md)).
- The platform holds no GDPR, SOC 2, or ISO 27001 attestation: the GDPR self-service flows exist, but processor-wide deletion and retention were not verified, and alerting, incident response, and formal security policies are the SOC 2 and ISO 27001 gaps.

## Secret-rotation cautions

- `scripts/rotate-api-keys.sh` and `scripts/apply-rotated-keys.sh` write Secret names and keys that the current charts do not read, so they must not be used; rotate through the current Helm, provisioner, or external-secret workflow, and verify the rotation in a disposable namespace before granting production access.
- Rewriting the shared Git history to purge the leaked keys is destructive and needs repository-owner coordination, a tested backup, and a repository-specific plan, so no generic force-push procedure applies.
