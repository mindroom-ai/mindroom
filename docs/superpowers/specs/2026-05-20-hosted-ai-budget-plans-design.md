# Hosted AI Budget Plans Design

## Summary

MindRoom SaaS should keep the existing low-cost hosted plan, rename it to BYOK, and add two hosted plans with included monthly OpenRouter usage.
The first customer flow should show three clear plans: BYOK at $10/month, Hobby at $20/month, and Pro at $200/month.
BYOK runs a hosted MindRoom instance but expects the customer to configure their own model provider keys.
Hobby includes up to $15/month of OpenRouter usage through a per-customer OpenRouter key.
Pro includes up to $150/month of OpenRouter usage and gets a larger Kubernetes resource profile.

## Product Semantics

The UI must say "included monthly AI usage" or "included AI budget" instead of "tokens topped up".
OpenRouter exposes API key spending limits and monthly resets, while account-level balance funding remains owned by MindRoom.
Unused included usage is therefore margin, and the product should not imply that unused credit belongs to the customer.

## Architecture

Pricing metadata remains the source of truth in `saas-platform/pricing-config.yaml`.
The backend pricing model gains plan fields for `included_ai_budget_usd`, `requires_customer_provider_keys`, and `resource_profile`.
The webhook path continues to map Stripe prices to subscription tiers through Stripe price metadata.
Instance provisioning reads the subscription tier and derives the correct OpenRouter key behavior and resource profile.

## OpenRouter Provisioning

The platform backend gets a focused OpenRouter service.
The service uses a platform management key stored as `OPENROUTER_PROVISIONING_API_KEY`.
For a plan with included AI budget, it creates an OpenRouter API key named with the account and instance identifiers, with `limit` set to the included budget and `limit_reset` set to `monthly`.
The returned OpenRouter key is stored only in the tenant Kubernetes Secret as `openrouter_key`.
Database rows store non-secret key metadata only, such as hash, label, monthly limit, and reset cadence.

## BYOK Behavior

BYOK does not receive a platform OpenRouter key.
BYOK instances get an empty `openrouter_key` value from platform provisioning.
The customer can later configure provider keys through the MindRoom workspace credential UI.

## Resource Profiles

The instance chart receives resource values from the platform provisioner.
The initial profiles are intentionally small and reproducible.
BYOK and Hobby use the current default profile.
Pro increases MindRoom, Synapse, sandbox, and storage values through Helm overrides.
Dedicated nodes or separate node pools remain a later scaling step after demand exists.

## Failure Handling

If an OpenRouter key cannot be created for a paid included-budget plan, provisioning fails before Helm runs.
The instance row is left in `error` when deployment cannot complete.
If a re-provisioned instance already has a matching OpenRouter key recorded, the platform reuses the existing secret value from Kubernetes when possible instead of minting a duplicate key.
No secret values should be logged, committed, or stored in Helm release values.

## Testing

Backend unit tests cover pricing metadata, OpenRouter request construction, subscription tier handling, and tenant secret wiring.
Provisioner tests verify BYOK has no generated OpenRouter key, Hobby receives a $15 monthly key, and Pro receives a $150 monthly key plus larger resource overrides.
Frontend tests verify the new plan names, prices, and included budget copy.
Deployment validation checks rendered Helm arguments and live backend health without printing secrets.
