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
  users:
    "@alice:example.com": 100
    "@intern:example.com": 5
```

Prices are USD per million tokens, set with the `pricing` field described in [Model Config Fields](https://docs.mindroom.chat/configuration/models/#model-config-fields).
The dashboard **Budgets** page edits the same settings and prices.

## Fields

| Field | Type | Default | Description |
| --- | --- | --- | --- |
| `monthly_limit_usd` | number ≥ 0 or `null` | `null` | Cap in USD for each user without an entry in `users`; `null` leaves those users uncapped |
| `fallback_model` | model name | required | Model that replies use instead of a priced model once the user reaches their cap |
| `users` | map of Matrix user ID to number ≥ 0 | `{}` | Caps in USD for specific users, overriding `monthly_limit_usd` |

A cap of `0` sends every priced reply for that user to the fallback model.
Bridge aliases share their canonical user's cap and spend.
Removing the `budgets` section turns budgets off.
Changes to caps, prices, and the fallback model apply without a restart.

## How Spend Is Counted

Spend is the cost of the user's [recorded token usage](https://docs.mindroom.chat/usage/#token-usage) since the first day of the current month in UTC, at the configured prices.
It includes the user's agent and team replies, delegated agents, and helper work they triggered, such as compaction summaries.
Usage of models without prices, such as local models or subscription logins, costs nothing.
Gemini thinking tokens are charged at the output price.
Audio tokens, internal work with no requester (`system:internal`), and usage without a date or requester are not charged.
Spend updates within about a minute of a reply finishing, so a user can go slightly over their cap before the fallback applies.
A reply that already started finishes on its model.

## Over-Budget Replies

Once a user reaches their cap, replies that would use a priced model use `fallback_model` instead.
This covers agent and team replies including every team member, delegated agents, Dynamic Workflow participants, scheduled tasks, and [OpenAI-compatible API](https://docs.mindroom.chat/openai-api/) keys mapped to a requester.
Models without prices keep running, because they add no spend.
The fallback model's usage still counts toward spend, but the fallback is never blocked.
Requests without a human requester, such as unauthenticated OpenAI-compatible calls, are never budgeted.

## Dashboard Budgets Page

The **Budgets** page turns budgets on or off, sets the default cap and the fallback model, edits per-user caps, and edits model prices.
It lists each user with spend or a cap of their own, their month-to-date spend, and whether they are over budget and using the fallback model.
It also lists models used this month that have no prices.
Click **Save** to write changes to `config.yaml`.

## Budget Status API

`GET /api/budgets` returns budget settings and spend under standard dashboard authentication.
With budgets off it returns `{"enabled": false}`.

| Field | Contents |
| --- | --- |
| `period_start`, `period_end` | First day of the current UTC month and of the next month |
| `generated_at` | When spend was last computed, or `null` before the first computation |
| `default_limit_usd`, `fallback_model` | The configured default cap and fallback model |
| `users` | Rows with `user_id`, `spend_usd`, `limit_usd`, and `over_budget`, highest spend first |
| `unpriced_models` | Rows with `provider`, `model`, and this month's `total_tokens` for models without prices |
| `coverage` | `scanned_sources` and `unavailable_sources`, as in [usage coverage](https://docs.mindroom.chat/usage/#token-usage) |

A `503` with `Budget monitor unavailable` means the agents are not running in the same process as the API, so no spend is available.
