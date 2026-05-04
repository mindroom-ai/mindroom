# MindRoom Review Rules

## Review Focus

- Prioritize bugs, security risks, privacy leaks, Matrix protocol regressions, broken authorization, deployment mistakes, and missing tests.
- Keep suggestions scoped to the changed code and the requested behavior.
- Prefer direct fixes that match existing local patterns over speculative abstractions.
- Treat `CLAUDE.md` as the source of truth for repository-specific architecture and workflow guidance.

## Python

- Prefer functional Python and typed dataclasses or Pydantic models over loose dictionaries for structured data.
- Keep imports at module top unless a function-local import avoids a real circular dependency or optional dependency boundary.
- Avoid broad try/except blocks unless the code is handling an expected external failure boundary.
- Do not weaken typed production code with `getattr` or `hasattr` fallbacks.
- If a test or mock breaks, prefer stricter fixtures or typed objects over production code branches that only satisfy the old test.

## Frontend

- Preserve the existing Vite React dashboard and Next.js SaaS portal conventions.
- Operational UI should stay dense, scannable, and responsive.
- Flag layout changes that are likely to overlap, truncate critical text, or fail on mobile.

## Docs And Generated Files

- Markdown docs should use one sentence per line.
- Do not recommend hand-editing generated CLI documentation sections.
- For generated documentation references, prefer updating the source documentation or generator.

## Deployment And Secrets

- Flag committed secrets, plaintext credentials, unsafe Helm values, and accidental use of manual Helm flows where the provisioner API is expected.
- Treat SSO cookies, Supabase RLS, Kubernetes namespace boundaries, and Matrix credentials as high-risk review areas.
