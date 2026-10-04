# Support

Use this page for support requests related to MindRoom and MindRoom clients (including the iOS app).

## Contact

- General support and bug reports: [MindRoom GitHub issues](https://github.com/mindroom-ai/mindroom/issues)

Do not post access tokens, private room IDs, personal data, or safety-report evidence in a public issue.

## What to Include in a Support Request

- What you were trying to do
- What happened instead
- Screenshots or screen recordings (if available)
- Device and OS version (for mobile issues)
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
- Include the homeserver URL and a screenshot if possible

### Account Deactivation

- Use the in-app path: `Settings` -> `Account` -> `Delete / Deactivate Account`
- If the flow fails, include the homeserver URL and any error message shown

## Hosted Subscriptions

| Subscription state | Instance | Platform OpenRouter key | Data |
|--------------------|----------|-------------------------|------|
| `active`, unexpired `trialing`, or `past_due` (Stripe is retrying payment and a positive paid payment is recorded) | Keeps running | Enabled | Kept |
| `cancelled`, `unpaid`, `incomplete`, `incomplete_expired`, `paused`, expired trial, free tier, or account pending deletion | Stopped | Disabled | Kept until the teardown date |
| Still inactive after the grace period | Uninstalled and marked `deprovisioned` | Deleted | PVCs and instance Secrets deleted |
| Entitled again while stopped | Started, or re-provisioned when its key or recorded tier does not match the tier | Re-enabled with its limit adjusted to the tier | Kept |
| Entitled again after teardown | Re-provisioned as a fresh instance | New key | Starts empty |
| Entitled on a different tier while running | Re-provisioned with the tier's resources | Same key with its limit adjusted to the tier, including zero for no budget | Kept |
| Entitled on a cheaper tier while stopped by the customer | Stays stopped | Limit lowered when larger than the tier includes | Kept |

The grace period defaults to 30 days, is at least 1 day, and is set with `cleanupScheduler.teardownGraceDays` (`INSTANCE_TEARDOWN_GRACE_DAYS`).
Only the lifecycle sets `instances.lifecycle_stopped_at` and `instances.teardown_after`, so an instance a customer or admin stopped manually is never restarted automatically.
Right before teardown the job re-reads the subscription and skips the teardown when it is entitled again.
An instance's platform OpenRouter key must match its subscription tier's included budget (`included_ai_budget_usd` in `pricing-config.yaml`), and its `instances.tier`, which provisioning records only after a successful deploy, must match the subscription's tier, so a plan change or a resubscription on another tier never hands back a key or resources from a pricier tier.
Plan changes update the existing OpenRouter key's spending limit in place, preserving its identity and accumulated usage; a tier with no included budget sets a zero-dollar limit.
A subscription with no recorded positive succeeded payment is stored as `unpaid` when Stripe reports `past_due`.
A customer-stopped instance is not redeployed, because that would start it; after the customer starts it, the lifecycle redeploys it for its tier in the background.
Re-provisioning never shrinks an instance's volumes, because Kubernetes refuses to shrink a PVC; a downgrade from `pro` keeps its larger volumes.
Checkout grants a plan's trial only to a Stripe customer who never had a trial, so cancelling and checking out again starts a paid subscription.
Customers whose instance is stopped for an inactive subscription see a dashboard banner with the teardown date and a link to billing.

## Account Deletion

A customer's deletion request (`POST /my/gdpr/request-deletion`) first sets every renewing Stripe subscription of its customer to end at the end of its current billing period (`cancel_at_period_end`) and marks it with the `mindroom_ends_for_account_deletion` metadata key.
A subscription the customer had already set to end within its paid period (`cancel_at`) keeps that end; one set to end later is moved to the period end, and the marker remembers the customer's date so that cancelling the deletion restores it.
If Stripe fails, the request returns `502`, the subscriptions it had already set to end are set back, and the account is not deleted.
It then marks the account pending deletion and stops its instances with their platform OpenRouter keys disabled; the response says so when stopping failed and will be retried.
Only once the deletion is recorded does it cancel `incomplete` and `paused` subscriptions, which have no paid period to finish; a failure there is retried by the nightly run.
An account pending deletion never runs instances, whatever Stripe reports, so its instances stay stopped during the grace period even while its subscription is still paid, and the nightly run keeps them stopped even while Stripe is unreachable.
Such an account cannot provision or start instances, open a checkout or the billing portal, or cancel or reactivate its subscription (`409`) until the deletion is cancelled, and an instance being provisioned or resumed for it is kept stopped.
Each nightly run repeats these Stripe steps for accounts still inside their grace period, which retries a step the request could not finish and also covers deletions requested before these steps existed; it skips an account the customer restored meanwhile and undoes its own change when the restore lands while it runs.
Cancelling the deletion (`POST /my/gdpr/cancel-deletion`) restores only the account and lets the marked subscriptions renew again; its instances restart once a subscription is entitled, which for a subscription whose period ended meanwhile means a new checkout.
A customer who cancels or reactivates a subscription through `/my/subscription/cancel` or `/my/subscription/reactivate` also clears the marker, so a later cancelled deletion never renews a subscription the customer chose to end.
After the 7-day grace period, cancelling returns `409`, because `restore_account` refuses by the database clock.
Soft delete keeps a `suspended` status and `restore_account` only restores a `deleted` one, so cancelling a deletion never lifts a suspension.
A held instance of an account pending deletion is never uninstalled by its own teardown date, even when `cleanupScheduler.teardownGraceDays` is shorter than 7 days; the account's cleanup removes it once the customer can no longer cancel, and until then its teardown date keeps moving forward.
Cancelling the deletion restarts the teardown grace period of every held instance, so a restored account's instance gets the full grace period.

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
