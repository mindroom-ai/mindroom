---
icon: lucide/wallet
---

# Budgets

Budgets cap how much each Matrix user can spend on AI replies per calendar month.
A user at their cap keeps chatting, but replies that would use a priced model use a cheaper fallback model until the next month starts.

## Setup

Budgets need prices for the models you pay for and a `budgets` section:

```yaml
models:
  astra:
    provider: openai
    id: gpt-6-astra
    pricing:
      input: 5.0
      output: 30.0
      cache_read: 0.5
  luna:
    provider: openai
    id: gpt-6-luna
    pricing:
      input: 0.2
      output: 1.25

budgets:
  monthly_limit_usd: 20
  fallback_model: luna
```

Prices are USD per million tokens, set with the `pricing` field described in [Model Config Fields](configuration/models.md#model-config-fields).
Give every model entry that points at a paid model its prices, including entries that differ only in API transport or ID prefix.
The dashboard **Budgets** page edits the same settings and prices.
To give a user a cap other than the default, see [Per-User Caps](#per-user-caps).

## Fields

| Field | Type | Default | Description |
| --- | --- | --- | --- |
| `monthly_limit_usd` | number ≥ 0 or `null` | `null` | Cap in USD for each user without a [cap of their own](#per-user-caps); `null` leaves those users uncapped |
| `fallback_model` | model name | required | Model that replies use instead of a priced model once the user reaches their cap |

A cap of `0` sends every priced reply for that user to the fallback model.
Bridge aliases share their canonical user's cap and spend.
Removing the `budgets` section turns budgets off.
Changes to caps, prices, and the fallback model apply without a restart.

## Per-User Caps

A user's own cap replaces `monthly_limit_usd` for that user.
Set it on the [Budgets page](#dashboard-budgets-page) or through the [Budget API](#budget-api), including for users who have not used MindRoom yet.
Per-user caps are not part of `config.yaml`: they survive restarts, and config edits or redeploys leave them unchanged.
A cap set for a bridge alias belongs to its canonical user.
Only people have caps; agents, bot accounts, and MindRoom's internal user cannot get one.
Caps can be set only while budgets are on, and they stay stored if budgets are turned off.

## How Spend Is Counted

Spend is the cost of the user's [recorded token usage](usage.md#token-usage) since the first day of the current month in UTC, at the configured prices.
It includes the user's agent and team replies, delegated agents, and helper work they triggered, such as compaction summaries.
Usage of models without prices, such as local models or subscription logins, costs nothing.
`claude_agent` coding sessions are charged at the prices of the Anthropic model entry with the same model ID, however Claude Code is signed in.
Reasoning and thinking tokens are charged at the output price.
Audio tokens count at the model's text prices.
Internal work with no requester (`system:internal`) and usage without a date or requester are not charged.
Spend updates within about a minute of a chat reply finishing and within five minutes of other usage, such as voice calls, so a user can go slightly over their cap before the fallback applies.
A reply that already started finishes on its model.

## Over-Budget Replies

Once a user reaches their cap, replies that would use a priced model use `fallback_model` instead.
This covers agent and team replies including every team member, delegated agents, Dynamic Workflow participants, scheduled tasks, the agent's replies in voice calls, and [OpenAI-compatible API](openai-api.md) keys mapped to a requester.
The speech model of a realtime or live call profile is not priced or swapped.
Models without prices keep running, because they add no spend.
A `claude_agent` tool without its own `model` starts Claude Code on the reply's model, so set its `model` when `fallback_model` is not a Claude model.
The fallback model's usage still counts toward spend, but the fallback is never blocked.
Requests without a human requester, such as unauthenticated OpenAI-compatible calls, are never budgeted.
Users get no chat message when the fallback applies; the Budgets page shows who is over budget.

## Dashboard Budgets Page

The **Budgets** page turns budgets on or off, sets the default cap and the fallback model, edits per-user caps, and edits model prices.
It lists each user with spend or a cap of their own, their month-to-date spend, whether they are over budget and using the fallback model, and who last set their cap.
It also lists models used this month that have no prices.
A per-user cap is saved as soon as you leave its field or press Enter, and clearing the field returns the user to the default cap.
Click **Save** to write the other changes to `config.yaml`.

## Budget API

The dashboard routes use standard dashboard authentication, and the same routes under `/api/budgets/service` use the [budget service](#budget-service) credential.

| Route | Effect |
| --- | --- |
| `GET /api/budgets` | Budget settings and spend, or `{"enabled": false}` with budgets off |
| `PUT /api/budgets/users/{user_id}` | Create or replace one user's cap with a body like `{"limit_usd": 50}` |
| `DELETE /api/budgets/users/{user_id}` | Remove one user's cap so the default applies; returns `204`, also when the user had no cap |
| `GET /api/budgets/service` | Same as `GET /api/budgets` |
| `PUT /api/budgets/service/users/{user_id}` | Same as `PUT /api/budgets/users/{user_id}` |
| `DELETE /api/budgets/service/users/{user_id}` | Same as `DELETE /api/budgets/users/{user_id}` |

URL-encode `user_id` in the path, for example `/api/budgets/users/%40alice%3Aexample.com`.
`limit_usd` is a JSON number of at least `0`; no other body fields are accepted.
`PUT` returns `user_id`, `limit_usd`, `updated_at`, and `updated_by`, where `user_id` is the canonical user the cap belongs to.
`updated_by` is `dashboard` for a cap set on the Budgets page or its routes, and `budget-service` for the budget service.

`GET` returns these fields:

| Field | Contents |
| --- | --- |
| `period_start`, `period_end` | First day of the current UTC month and of the next month |
| `generated_at` | When spend was last computed, or `null` before the first computation |
| `default_limit_usd`, `fallback_model` | The configured default cap and fallback model |
| `users` | One row per user with spend this month or a cap of their own, highest spend first, with `user_id`, `spend_usd`, the applied `limit_usd`, `over_budget`, and the user's own cap as `override_usd`, `updated_at`, and `updated_by`, which are `null` when the default applies |
| `unpriced_models` | Rows with `provider`, `model`, and this month's `total_tokens` for models without prices |
| `coverage` | `scanned_sources` and `unavailable_sources`, as in [usage coverage](usage.md#token-usage) |

| Status | Meaning |
| --- | --- |
| `503` `Budget monitor unavailable` | The agents are not running in the same process as the API, so no spend or caps are available |
| `409` `Budgets are off` | Turn on budgets before setting or removing caps |
| `422` `User ID must be a concrete Matrix user ID` | Use a full Matrix user ID like `@alice:example.com` |
| `422` `Caps apply only to people, not agents or bots` | The ID belongs to an agent, a bot account, or MindRoom's internal user |
| `422` with a list of errors | `limit_usd` is missing, negative, not a finite number, or the body has other fields |

## Budget Service

Strict JWT deployments can let a service, such as a company spend dashboard, read budgets and set caps without a browser or administrator identity.
Configure [trusted-upstream strict JWT mode](deployment/trusted-upstream-auth.md#strict-jwt-mode), then add a dedicated audience and client ID:

```bash
MINDROOM_BUDGET_SERVICE_JWT_AUDIENCE=mindroom-budget-service
MINDROOM_BUDGET_SERVICE_CLIENT_ID=budget-service-client.example.org
```

The service sends an assertion that follows the rules of the [usage export service](usage.md#usage-export-service), with this audience and client ID instead.
Give the `/api/budgets/service` path its own access application, so the service's audience never applies to dashboard routes.
A budget service assertion authorizes only the `/api/budgets/service` routes, and a usage export assertion does not authorize them.

| Status | Meaning |
| --- | --- |
| `503` `Budget service authentication is not configured` | Trusted-upstream auth, a strict-JWT setting, or a budget service setting is missing |
| `401` `Invalid budget service JWT` | Missing or invalid assertion |
