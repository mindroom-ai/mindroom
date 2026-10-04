---
icon: lucide/life-buoy
---


# Support

Use this page to get help with MindRoom and MindRoom clients, including the iOS app, and to look up how hosted subscriptions and account deletion work.

## Contact

- General support and bug reports: [MindRoom GitHub issues](https://github.com/mindroom-ai/mindroom/issues)

Do not post access tokens, private room IDs, personal data, or safety-report evidence in a public issue.

## What to Include in a Support Request

- What you were trying to do
- What happened instead
- Any error message shown
- Screenshots or screen recordings (if available)
- Device model and OS version (for mobile and desktop app issues)
- App version / build number
- Homeserver URL (if relevant)

## Common Issues

### Login / Registration Problems

- Confirm the homeserver URL is correct
- Confirm the homeserver is reachable over HTTPS (for public servers)
- If using SSO, confirm the homeserver advertises the expected identity provider

### Media (Images / Audio) Not Loading

- Check connectivity to the homeserver
- Confirm your account still has access to the room/content

### Account Deactivation

- Use the in-app path: `Settings` -> `Account` -> `Delete / Deactivate Account`
- To delete a hosted MindRoom platform account, see [Account Deletion](#account-deletion).

## Hosted Subscriptions

A hosted instance runs only while its subscription entitles it, and its data is deleted when the subscription stays inactive past the grace period.

| Subscription state | Instance | Platform OpenRouter key | Data |
|--------------------|----------|-------------------------|------|
| `active`, unexpired `trialing`, or `past_due` while Stripe retries a payment | Keeps running | Enabled | Kept |
| `cancelled`, `unpaid`, `incomplete`, `incomplete_expired`, `paused`, expired trial, free tier, or account pending deletion | Stopped | Disabled | Kept until the teardown date |
| Still inactive after the grace period | Uninstalled and marked `deprovisioned` | Deleted | Volumes and instance Secrets deleted |
| Entitled again while stopped | Started, and re-provisioned when the tier changed | Re-enabled with the tier's limit | Kept |
| Entitled again after teardown | Re-provisioned as a fresh instance | New key | Starts empty |
| Entitled on a different tier while running | Re-provisioned with the tier's resources | Same key with the tier's limit | Kept |
| Entitled on a cheaper tier while stopped by the customer | Stays stopped, and is redeployed for its tier once the customer starts it | Limit lowered to the tier's budget | Kept |

A `past_due` subscription keeps running only if it has had a successful payment; one that never paid is treated as `unpaid` and stops.
Customers whose instance is stopped for an inactive subscription see a dashboard banner with the teardown date and a link to billing.
While the subscription stays active, an instance a customer or admin stopped manually is never restarted automatically.
If the subscription becomes inactive, MindRoom holds that instance like any other, and restoring billing starts it again.

To cancel, select `Manage Subscription` on the dashboard's `Billing` page to open the Stripe customer portal, or call `POST /my/subscription/cancel`.
The API cancels at the end of the current billing period by default, so the instance keeps running until then; send `{"cancel_at_period_end": false}` to cancel immediately.
Until the period ends, `Reactivate subscription` on the `Billing` page opens the same portal to keep the subscription.

The grace period defaults to 30 days, is at least 1 day, and is set with `cleanupScheduler.teardownGraceDays` (`INSTANCE_TEARDOWN_GRACE_DAYS`).
The platform operator enables the nightly job that applies this lifecycle, as described in [Subscription Lifecycle](deployment/saas-platform.md#subscription-lifecycle).

Each tier's AI budget is its `included_ai_budget_usd` in `pricing-config.yaml`, and a tier without one gets a zero-dollar limit.
A plan change or resubscription on another tier always gets that tier's budget and resources, never those of a pricier earlier tier.
Plan changes keep the instance's existing OpenRouter key and its accumulated usage.
Downgrading never shrinks an instance's storage volumes, so a downgrade from `pro` keeps its larger volumes.
Each Stripe customer gets a plan's trial only once, so cancelling and checking out again starts a paid subscription.

## Account Deletion

Request deletion of a hosted account from the platform dashboard under `Settings` -> `Delete Account`, or with `POST /my/gdpr/request-deletion` and the JSON body `{"confirmation": true}`.
Without that confirmation, the API only returns a warning and schedules nothing.
The request does the following:

- Every renewing subscription is set to end at the end of its current billing period, and a subscription already set to end within its current period keeps that end.
- Subscriptions with no paid period to finish (`incomplete`, `paused`) are cancelled.
- The account's instances stop at once with their platform OpenRouter keys disabled, and the response says when stopping failed and will be retried automatically.

If Stripe cannot schedule the end of billing, the request fails with `502` and the account is not deleted, so try again.

While the account is pending deletion, its instances stay stopped even when a subscription is still paid.
Provisioning or starting an instance fails with `409` `This account is pending deletion and cannot run instances`.
Opening a checkout or the billing portal, or cancelling or reactivating a subscription, fails with `409` `Cancel the account deletion before changing billing`.

To cancel the deletion within 7 days, sign in and select `Cancel Deletion Request` in `Settings`, or call `POST /my/gdpr/cancel-deletion`.
Signing in alone does not cancel the deletion.
Cancelling restores the account and lets the subscriptions the deletion set to end renew again with the end date the customer had chosen before.
Instances restart once a subscription is entitled, and a subscription whose period ended meanwhile needs a new checkout.
Each instance stopped by the deletion or an inactive subscription gets its full teardown grace period again.
After 7 days, cancelling fails with `409` `This account deletion can no longer be cancelled`.
Cancelling a deletion never lifts a suspension.

An account's instances are not torn down while its deletion can still be cancelled, even when `cleanupScheduler.teardownGraceDays` is shorter than 7 days.
After the 7 days, the nightly cleanup, when the operator enables it, deletes the instances and account data; see [Account Deletion](deployment/saas-platform.md#account-deletion) for what it deletes and retains.

## Abuse / Safety Reports

Use in-app report/block tools first when available.

Do not put moderation evidence or private identifiers in a public GitHub issue.
If the in-app controls fail, open a general issue without sensitive evidence so maintainers can publish an appropriate private contact path.

Useful public context includes:

- the affected feature and client version
- a short general description with identifiers removed
- sanitized error text with tokens, Matrix IDs, room IDs, and message links removed

## Response Times

Support is provided on a best-effort basis.
