# Security Review: Dependency & Supply Chain Security

September 2025 review of dependency and supply-chain security for the MindRoom repository: vulnerability audits, base images, dependency sources, version pinning, and automated scanning.
It keeps the audit results, the base-image registry decision, and the gaps still open when re-checked against CI and the Dockerfiles on 2026-10-12.

## Evidence

A manual audit in September 2025 found no known vulnerabilities in the Python dependencies (`pip-audit` 2.9.0, 400+ packages, shared by the core project and the platform backend) or in the platform portal's JavaScript dependencies (326 packages).
The core dashboard's JavaScript dependencies (`pnpm audit`, 1,148 packages) had five low or moderate advisories: the esbuild and vite development servers allowed cross-origin or file-system reads (GHSA-67mh-4wv8-2f99, GHSA-jqfw-vq24-v9c3), and mermaid, pulled in through `@lobehub/icons`, allowed XSS and prototype pollution through diagram definitions (GHSA-7fh5-64p2-3v2j, GHSA-wh77-vqf5-xf8w).

## Decisions

- Base images are official images pulled through a public registry mirror instead of Docker Hub, to avoid Docker Hub pull rate limits.

## Open findings

- Scanning does not gate changes: the CI Python dependency audit and secret scan report findings without failing the build, so a vulnerable lock file or a committed secret still merges.
- JavaScript dependencies, where the 2025 audit found every advisory, and built container images are not scanned in CI, so new advisories there surface only through manual audits.
- The `uv` build stage uses the `ghcr.io/astral-sh/uv:latest` image, so a rebuild can pick up an unreviewed `uv` release.
