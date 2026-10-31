# SaaS instance status sync and Stripe catalog: one owner each

Status: implemented (2026-10-30).
Scope: how the platform backend derives `instances.status` from Kubernetes, and which path owns the Stripe catalog.
This record keeps what the code cannot show.

## Problem

Two copies derived and wrote `instances.status` from Kubernetes: the dashboard's background refresh in `routes/instances.py` and the admin and system bulk sync in `provisioner_service.sync_instances`.
Every fix after the copy (status preservation, readiness, the status guard) landed in the dashboard copy only, so the two disagreed on readiness, missing deployments, kubectl failures, and write guarding.
An admin "Refresh All" during a first boot, a restart, or a crash loop showed the instance Running with Launch enabled, and the next dashboard poll flipped it back.

Two scripts owned the Stripe catalog.
`setup-stripe-products.js` kept a hardcoded catalog after `pricing-config.yaml` and `sync-stripe-prices.py` replaced it, and its lookup keys collided with the synchronizer's.
The synchronizer itself reused a price whose amount no longer matched the YAML, so the pricing page and checkout disagreed after an amount change.

## Decisions

### Readiness decides `running`

An instance is `running` only once every desired pod is ready, available, and updated; desired zero is `stopped`; anything else is `provisioning`.
Provisioning already writes `running` only once pods are ready, and the dashboard showed this rule to owners before the bulk sync adopted it.
The rule binds the syncs only: starting an instance still records `running` as the desired state before its pods are ready, and the next sync corrects it.
A crash-looping instance therefore shows `provisioning`, also after an admin "Refresh All".
The admin actions offer Stop and Restart for `provisioning` too, so a crash loop never leaves an admin only the destructive Uninstall; the customer page keeps them for `running`, because during a first boot they would interrupt the provision.
An admin can still interrupt a provision in flight that way.

### A missing deployment keeps `provisioning` and `deprovisioned`

A provision records its `provisioning` claim before Helm creates the deployment, and the sync cannot tell that window from a provision that died part-way.
Marking such a row `error` showed a healthy new instance as failed until the provision overwrote it after its readiness wait.
An orphaned `provisioning` row comes from a provision that stopped before Helm ran: a backend restart, or a failed step before the deployment that does not mark the row `error`.
Such a row can still own volumes, because an admin provision from `error` and a lifecycle resume both redeploy instances whose deployment is gone, so Uninstall is not a safe recovery.
The operator procedure in `docs/deployment/saas-platform.md` sets the row to `error` by hand and provisions it again, which keeps the volumes.
Any other status without a deployment becomes `error`.

### A failed observation changes nothing

A kubectl failure says nothing about the instance, so it must never read as a missing deployment.
The earlier existence check returned false on any failure, so an API-server outage marked running instances `error` and made the subscription lifecycle redeploy a resuming instance instead of starting it.
The deployment lookup raises on failure for every caller: a sync leaves the row as it was and unstamped, so the next dashboard poll checks again, the bulk sync counts the failure, and the lifecycle records it and retries.

### Every write is guarded by the status it read

A provision that claims the instance while a sync waits on Kubernetes changes its status, and the sync must not undo that claim.
The guard compares status only, so a sync that read `provisioning` while a provision held the instance can still record what Kubernetes shows (see the known limitation).

### Stripe prices are matched by amount

A Stripe price's amount cannot change, so the synchronizer reuses only an active price with the configured tier, billing cycle, and amount.
Otherwise it creates a new price that takes over the plan's lookup key, because Stripe allows one price per lookup key and the replaced price still holds it.
Existing subscribers stay on their old price and still map to their plan through its metadata.
The synchronizer writes only the test-mode price fields; live-mode price IDs are still set by hand.

## Rejected alternatives

- Mark `provisioning` without a deployment `error` as the bulk sync did: the dashboard polls during every signup, so new customers would often see a failed instance for minutes.
- Flag `provisioning` without a deployment as `error` once it is older than a threshold: every sync stamps `updated_at`, so the age needs a separate claim time, one more timed rule for a crash-only case.
- Let the admin Provision action accept `provisioning`: one click during a live provision would start a second Helm run.
- Move the dashboard's in-process deduplication into the service: it is a scheduling concern of the dashboard route, and an overlap with the bulk sync is harmless under the status guard.

## Known limitation

A sync during a redeploy of an existing deployment that is still ready can record `running` over the provision's `provisioning` claim before Helm replaces the pods.
The status alone cannot show that a provision is in flight; both copies behaved this way before.
