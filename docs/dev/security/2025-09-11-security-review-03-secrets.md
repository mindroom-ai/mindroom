# Security Review 03: Secrets Management

September 2025 review of how the SaaS platform and its Kubernetes deployment store, scan for, and rotate secrets, updated by a March 2026 audit note.
Findings that the current charts and scripts fix are omitted.

## Decisions

- Platform backend secrets are mounted as read-only files from a Kubernetes Secret instead of passed as environment variables, so they do not appear in the process environment (`/proc/<pid>/environ`).

## Open

- Three provider API keys (DeepSeek, Google, and OpenRouter) were found committed in docs during the review.
  No revocation at the providers is on record, and whether every public mirror was purged of them (for example with `scripts/clean-git-history.sh`) is unconfirmed.
- No full-history secret scan has been recorded; the CI gitleaks step runs on a shallow checkout and does not fail the build, so it never covers the full history.
- No key rotation has been confirmed executed.
- Kubernetes Secret encryption at rest (etcd) has not been verified on the deployed cluster.
- Synapse's `macaroon_secret_key` and `form_secret` are rendered into the homeserver ConfigMap rather than a Secret, and there is no rotation plan for Matrix secrets or the homeserver signing key.
