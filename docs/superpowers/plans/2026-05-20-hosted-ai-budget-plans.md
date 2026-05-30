# Hosted AI Budget Plans Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task.
> Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add BYOK, Hobby, and Pro SaaS plans with per-customer OpenRouter budget keys and reproducible Kubernetes resource profiles.

**Architecture:** Pricing metadata drives frontend copy, Stripe checkout, webhook tier mapping, OpenRouter key provisioning, and Helm instance resources.
The platform backend owns OpenRouter API calls and stores generated keys only in tenant Kubernetes Secrets.
Supabase stores non-secret key metadata for reuse and audit.

**Tech Stack:** FastAPI, Supabase, Stripe, OpenRouter HTTP API, Helm, Kubernetes Secrets, Next.js, React, Jest, pytest.

---

## Files

- Modify `saas-platform/pricing-config.yaml` to rename Starter to BYOK and add Hobby and Pro.
- Modify `saas-platform/platform-backend/src/backend/pricing.py` to validate new plan metadata.
- Create `saas-platform/platform-backend/src/backend/openrouter.py` for OpenRouter key provisioning.
- Modify `saas-platform/platform-backend/src/backend/config.py` to load `OPENROUTER_PROVISIONING_API_KEY`.
- Modify `saas-platform/platform-backend/src/backend/routes/provisioner.py` to generate per-plan OpenRouter secrets and apply resource profiles.
- Modify `saas-platform/supabase/migrations/000_consolidated_complete_schema.sql` to add non-secret OpenRouter metadata columns on `instances`.
- Modify `cluster/k8s/platform/templates/all.yaml` and values files to mount the provisioning key.
- Modify `saas-platform/platform-frontend/src/components/landing/Pricing.tsx` and `saas-platform/platform-frontend/src/app/dashboard/billing/upgrade/page.tsx` to show BYOK, Hobby, and Pro.
- Add or update backend and frontend tests around the new behavior.

## Task 1: Pricing Metadata

- [ ] Write a backend pricing test that asserts BYOK is $10/month, Hobby is $20/month with $15 AI budget, and Pro is $200/month with $150 AI budget.
- [ ] Run the pricing test and verify it fails because the metadata fields do not exist yet.
- [ ] Extend the Pydantic pricing models with `included_ai_budget_usd`, `requires_customer_provider_keys`, and `resource_profile`.
- [ ] Update `pricing-config.yaml` with `byok`, `hobby`, and `pro`.
- [ ] Run the pricing test and verify it passes.
- [ ] Commit pricing metadata changes.

## Task 2: OpenRouter Service

- [ ] Write tests for request construction against `POST https://openrouter.ai/api/v1/keys`.
- [ ] Verify the test fails because `backend.openrouter` does not exist.
- [ ] Implement `OpenRouterKeyPlan`, `CreatedOpenRouterKey`, and `create_openrouter_key`.
- [ ] Ensure the service accepts an injected HTTP function so tests do not need network access.
- [ ] Run OpenRouter service tests and verify they pass.
- [ ] Commit OpenRouter service changes.

## Task 3: Provisioner Integration

- [ ] Write provisioner tests that assert BYOK applies an empty `openrouter_key`.
- [ ] Write provisioner tests that assert Hobby applies a generated `openrouter_key` with limit 15 and monthly reset.
- [ ] Write provisioner tests that assert Pro applies a generated `openrouter_key` with limit 150 and Pro resource overrides.
- [ ] Verify the tests fail against current shared-key behavior.
- [ ] Wire pricing metadata into `provision_instance`.
- [ ] Persist non-secret OpenRouter metadata on `instances`.
- [ ] Keep the actual key only in the externally applied Kubernetes Secret.
- [ ] Run provisioner tests and verify they pass.
- [ ] Commit provisioner integration changes.

## Task 4: Frontend Plans

- [ ] Write or update tests that expect BYOK, Hobby, Pro, `$10`, `$20`, `$200`, `$15 included`, and `$150 included`.
- [ ] Verify the tests fail against the current Starter and Professional copy.
- [ ] Update landing and upgrade pages to sort plans as BYOK, Hobby, Pro, Enterprise.
- [ ] Remove per-user controls from the primary hosted checkout flow because these plans are not per-seat.
- [ ] Run frontend tests and verify they pass.
- [ ] Commit frontend plan changes.

## Task 5: Deployment Wiring

- [ ] Add `OPENROUTER_PROVISIONING_API_KEY_FILE` support to the platform backend deployment.
- [ ] Add `openrouter_provisioning_api_key` to the platform Secret template and example values.
- [ ] Run Helm template tests or `helm template` and verify the secret mount renders without exposing values in release arguments.
- [ ] Commit deployment wiring changes.

## Task 6: Validation, PR, and Deployment

- [ ] Run targeted backend tests for pricing, OpenRouter, provisioner, webhooks, and instances.
- [ ] Run targeted frontend tests for pricing, upgrade, API, and build.
- [ ] Run `git diff --check`.
- [ ] Push `codex/hosted-ai-budget-plans`.
- [ ] Open a PR and wait for CI.
- [ ] Merge only if CI passes.
- [ ] Deploy backend/frontend with the current production secret strategy.
- [ ] Validate production health and that plan config is visible.
- [ ] Do not switch live Stripe prices or OpenRouter provisioning live until required credentials and Stripe live products exist.
