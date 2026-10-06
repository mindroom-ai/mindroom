---
icon: lucide/building-2
---

# SaaS Platform

## Hetzner Scaling Baseline

Start with one K3s server node and keep tenant storage on Hetzner Cloud Volumes.
This keeps the current bill low while making later worker nodes possible.
Use `hcloud-volumes` for provisioned instance PVCs and avoid `local-path` for tenant data that must survive node replacement.
The platform frontend and backend are stateless and can use HPA once metrics-server is installed.
Tenant MindRoom and Synapse releases stay single-replica stateful workloads; scale capacity by placing more tenants across more nodes.

Production platform values should make the storage class explicit:

```yaml
provisioner:
  instanceStorageClassName: hcloud-volumes

autoscaling:
  enabled: false
```

Enable `autoscaling.enabled` only after the cluster has metrics-server and enough nodes or headroom to schedule the extra replicas.

## Instance Deployment

### Via Provisioner API (Recommended)

Create customer instances through the portal or `POST /my/instances/provision` using the customer's Supabase access token.
The route checks subscription entitlement and derives the account, subscription, and tier from the authenticated customer.
Set `PLATFORM_DOMAIN` to the deployment domain and `SUPABASE_ACCESS_TOKEN` to that customer's access token:

```bash
curl --fail-with-body --request POST \
  "https://api.${PLATFORM_DOMAIN}/my/instances/provision" \
  --header @- <<EOF
Authorization: Bearer ${SUPABASE_ACCESS_TOKEN}
EOF
```

The authorization header is read from standard input so the token does not appear in curl's process arguments.

Fresh creation omits an instance ID and receives a generated ID; an existing instance for the subscription is returned, restarted when an inactive subscription stopped it, or re-provisioned when deprovisioned.
Use the returned `customer_id` with the CLI's `logs <id>` command.
The CLI's `provision <id>` command uses fixed test metadata and is not the new-customer creation path.

### Direct Helm Installation

For debugging only, first create a private values file with a nonempty sandbox token from a trusted working directory:

```bash
(
  set -eu
  umask 077
  test ! -d ./instance-secrets.yaml
  instance_secrets_tmp="$(mktemp ./instance-secrets.yaml.XXXXXX)"
  trap 'rm -f -- "$instance_secrets_tmp"' EXIT
  instance_sandbox_token="$(openssl rand -hex 32)"
  printf 'sandbox_proxy_token: "%s"\n' "$instance_sandbox_token" > "$instance_secrets_tmp"
  mv -f -- "$instance_secrets_tmp" ./instance-secrets.yaml
)
```

The private temporary file replaces `instance-secrets.yaml` atomically, without reusing an existing file's permissions or following a file symlink.
Keep this file out of version control.
With Helm's default Secret storage backend, the command below also retains supplied values and rendered Secrets in release history in `mindroom-instances`.
Readers of those release Secrets can recover the sandbox token and other supplied credentials.
Restrict access to both the instance Secret and every retained Helm release Secret, as well as this local values file.
Deleting the local file or changing future values does not remove credentials from older Helm release revisions.
The chart passes the same token to the runtime and sandbox runner; the default file and shell tools need it to acquire the static runner.
The provisioner supplies this token automatically, while a direct install must provide it.
Browser dashboard login through the platform also needs `platformSsoSecret` set to the key the platform derives for that instance; without it the instance accepts only Supabase bearer tokens.
Tenant pods run tenant code, so the namespace must enforce the Pod Security `baseline` profile.
Terraform creates it with that label and the provisioner reapplies the label before every deployment; a direct install labels it first:

```bash
kubectl create namespace mindroom-instances
kubectl label namespace mindroom-instances pod-security.kubernetes.io/enforce=baseline --overwrite
helm upgrade --install instance-1 ./cluster/k8s/instance \
  --namespace mindroom-instances \
  -f instance-secrets.yaml \
  --set customer=1 \
  --set accountId="your-account-uuid" \
  --set baseDomain=mindroom.chat \
  --set anthropic_key="your-key" \
  --set openrouter_key="your-key" \
  --set supabaseUrl="https://your-project.supabase.co" \
  --set supabaseAnonKey="your-anon-key"
```

Never give an instance the Supabase service-role key; the runtime authenticates with the anon key only.

Only enable trusted upstream auth behind a verified access layer; see [Trusted Upstream Browser Auth](trusted-upstream-auth.md#helm-charts) for the instance and provisioner chart values.

## Secrets Management

The instance chart mounts provider keys as files under `/etc/secrets/` and sets the matching [`*_API_KEY_FILE`](../configuration/models.md#file-based-secrets) variables.

Provisioned instances keep their credentials in the Secret `mindroom-api-keys-{instance_id}`, which the platform backend writes outside Helm, so tenant API keys and OIDC client secrets never enter Helm release values or history.
Tenant workloads are untrusted, so the instance Secret holds only instance-scoped credentials and never the platform's Supabase service-role key.
The one exception is the platform-wide Matrix OIDC client secret, which only Synapse mounts; the MindRoom container mounts just the provider key and registration secret files it reads.
The sandbox proxy token is random per instance.
Upgrading from releases that copied platform credentials into instance Secrets requires rotating the Supabase service-role key and any platform provider keys copied by `cluster/scripts/update-api-keys.sh`, then re-provisioning every instance with `POST /system/provision` and its `instance_id`.
Standalone chart installs that set the retired `supabaseServiceKey` value keep that key in the chart-created Secret until it is deleted manually.

## Ingress

Each instance gets three hosts:

- `{customer}.{baseDomain}` - MindRoom dashboard and API
- `{customer}.api.{baseDomain}` - Direct API access
- `{customer}.matrix.{baseDomain}` - Matrix/Synapse server

## Platform Deployment

Run these commands from the repository root against the platform cluster; the repository's `.envrc` exports `KUBECONFIG=./cluster/terraform/terraform-k8s/mindroom-k8s_kubeconfig.yaml`, which the Terraform setup writes.

```bash
# Create values file from example
cp cluster/k8s/platform/values-staging.example.yaml cluster/k8s/platform/values-staging.yaml
# Edit with your configuration

helm upgrade --install platform ./cluster/k8s/platform \
  -f ./cluster/k8s/platform/values-staging.yaml \
  --namespace staging --create-namespace
```

`monitoring.enabled` defaults to `true` and renders a `ServiceMonitor` and `PrometheusRule`, so the install fails on clusters without the Prometheus Operator CRDs; install the operator first or set `monitoring.enabled=false`.

For a fresh install, `staging` stores Helm release records; the chart creates application resources in `mindroom-{environment}`, with `environment` set in values.
For an existing release, retain its original release name and namespace when upgrading.
Ingress hosts use `domain`, independently of the namespace; the base values use `mindroom.chat`, while the staging example uses `staging.mindroom.chat`.
Populate the staging file's credentials before deploying and keep it private.
With this chart-managed Secret workflow, Helm also retains the supplied values and rendered Secrets in release history in `staging`; anyone with permission to read those release Secrets can recover the credentials.
Restrict access to the values file and all retained release Secrets.
For production, set `platformSecrets.create=false` and pre-create the named Secret so API keys, webhook secrets, and Matrix OIDC private keys do not enter Helm release values.
This external-Secret option is also available in staging; omit credential material from Helm values when using it.
Provisioning that external Secret requires the application namespace to exist with ownership compatible with the chart, which still declares `mindroom-{environment}` regardless of `platformSecrets.create`.
Switching to the external-Secret option does not remove credentials from earlier release revisions.
The Secret must contain the same keys rendered by the chart-managed `platform-secrets` Secret, including `supabase_service_key`, `stripe_secret_key`, `stripe_webhook_secret`, `provisioner_api_key`, `instance_credentials_encryption_secret`, provider API keys, and the optional `matrix_oidc_*` keys.
Tiers with an included AI budget need an OpenRouter management key in `apiKeys.openrouterProvisioning` (`openrouter_provisioning_key` in an external Secret), which the platform uses to create each instance's spending-limited OpenRouter key; without it, provisioning those tiers fails with `OPENROUTER_PROVISIONING_API_KEY is required to create included-budget OpenRouter keys`.

Platform ingress hosts:

- `app.{domain}` - Platform frontend
- `api.{domain}` - Platform backend API
- `webhooks.{domain}/webhooks/stripe` - Stripe webhooks

The backend keys its rate limits and its authentication-failure lockout on the client address.
It takes that address from `X-Real-IP` only when the connection comes from a network in `trustedProxyCidrs` (`TRUSTED_PROXY_CIDRS`), and keys every other caller by its own connection address.
The default lists the private IPv4 ranges, so an in-cluster ingress-nginx controller is trusted, while instance pods cannot reach the backend port to use that trust.
Narrow the list to the controller's pod network when you know it, and include only proxies that set `X-Real-IP` from the client connection rather than from client-supplied `X-Forwarded-For` or PROXY protocol headers.
The reference Terraform configures ingress-nginx that way, with `use-forwarded-headers` and `use-proxy-protocol` off and the controller Service's `externalTrafficPolicy` set to `Local` so the controller sees the client's address instead of the node's.
Without `TRUSTED_PROXY_CIDRS`, as in the Docker Compose setup, every caller is keyed by its connection address.

### Matrix SSO

With `matrixOidc.enabled: "true"` (default `"false"`), instance owners sign into their instance's Synapse server with their platform login.
Enabling it requires `matrixOidc.clientSecret`, a random shared secret, and `matrixOidc.privateKey`, a PEM-encoded RSA private key such as one from `openssl genrsa 2048`; an external Secret holds them as `matrix_oidc_client_secret` and `matrix_oidc_private_key`.
`matrixOidc.issuer` defaults to `https://api.{domain}/matrix-oidc`, `matrixOidc.clientId` to `mindroom-synapse`, and `matrixOidc.keyId` to `mindroom-platform`.
Existing instances pick up the setting when they are re-provisioned, for example with `deploy-release.sh`.

## Local Development with Kind

```bash
just cluster-kind-fresh              # Start cluster with everything
just cluster-kind-port-frontend      # http://localhost:3000
just cluster-kind-port-backend       # http://localhost:8000
just cluster-kind-down               # Clean up
```

See `cluster/k8s/kind/README.md` for details.

## CLI Helper

```bash
./cluster/scripts/mindroom-cli.sh list              # List instances
./cluster/scripts/mindroom-cli.sh status            # Overall status
./cluster/scripts/mindroom-cli.sh logs <id>         # View logs
./cluster/scripts/mindroom-cli.sh provision <id>    # Re-provision an existing test fixture
./cluster/scripts/mindroom-cli.sh deprovision <id>  # Remove instance
./cluster/scripts/mindroom-cli.sh upgrade <id>      # Upgrade instance
```

Reads configuration from `saas-platform/.env`.
The `provision` helper always sends an instance ID, a fixed test account UUID, a synthetic subscription ID, and the BYOK tier.
Use it only with a prepared test fixture whose instance and account records already exist, including the account email used to derive the owner identity.
Use the customer route above for customer provisioning; operators should use real account and subscription records with the API below.

## Provisioner API

All endpoints require bearer token (`PROVISIONER_API_KEY`).

| Endpoint | Method | Description |
|----------|--------|-------------|
| `/system/provision` | POST | Create or re-provision an instance |
| `/system/instances/{id}/start` | POST | Start a stopped instance |
| `/system/instances/{id}/stop` | POST | Stop a running instance |
| `/system/instances/{id}/restart` | POST | Restart an instance |
| `/system/instances/{id}/uninstall` | DELETE | Remove an instance: Helm release, PVCs, instance Secrets, and its OpenRouter key |
| `/system/sync-instances` | POST | Sync states between DB and K8s |

For new instances, supply the real account UUID, subscription row UUID, and matching tier, and omit `instance_id`.
Supplying `instance_id` selects an update of an existing instance; a missing row returns `404`.
The account must have an email for owner identity derivation.
Replace the placeholders in this operator request with those database values:

```bash
curl -X POST "https://api.mindroom.chat/system/provision" \
  -H "Content-Type: application/json" \
  -H "Authorization: Bearer $PROVISIONER_API_KEY" \
  -d '{"account_id": "<account-uuid>", "subscription_id": "<subscription-row-uuid>", "tier": "<subscription-tier>"}'
```

The provisioner creates the namespace, generates URLs, deploys via Helm, and updates status in Supabase.
For provisioned instance charts, set `provisioner.instanceCredentialsEncryptionSecret` to a stable high-entropy value so the provisioner can derive stable per-instance `credentials_encryption_key` chart values.
If `provisioner.instanceCredentialsEncryptionSecret` is unset, the provisioner falls back to `PROVISIONER_API_KEY` when generating keys for new instances or explicit existing-instance opt-ins.
Keep this source stable because changing it changes future derived credential encryption keys and each instance's dashboard login signing secret.
Rotating `PROVISIONER_API_KEY` while `provisioner.instanceCredentialsEncryptionSecret` is unset breaks existing dashboard logins with `Invalid platform login` until instances are re-provisioned, so first copy the current key into `provisioner.instanceCredentialsEncryptionSecret`.
New provisioned instances receive a derived credential encryption key by default.
When re-provisioning an existing instance, the provisioner preserves the current encryption state by reusing `credentials_encryption_key` from the existing instance Secret when present.
An existing instance whose MindRoom storage PVC no longer exists, such as one torn down after its grace period, starts on an empty volume and also receives the derived key.
To enable credential encryption for an existing keyless instance, include `"enable_credentials_encryption": true` in the `POST /system/provision` request body.
Treat that opt-in as a one-way switch until a plaintext migration exists.
If an existing instance still has plaintext credential files, enabling credential encryption makes those files unreadable and encrypted-mode saves refuse to overwrite them.
Clear or replace stale plaintext credential files before enabling the flag.
If an already-encrypted instance has lost its instance Secret, pass `"enable_credentials_encryption": true` during reprovisioning so Helm receives the stable derived key again.

## Subscription Lifecycle

Hosted instances follow their subscription.
Stripe subscription and invoice webhooks reconcile the account's instances after the webhook response, so Kubernetes or OpenRouter trouble never fails a webhook.
The nightly cleanup job at 03:00 UTC reconciles every subscription that owns an instance the same way, which also catches missed webhooks and retries failed steps.

See [Hosted Subscriptions](../support.md#hosted-subscriptions).

The cleanup job only runs when `cleanupScheduler.enabled` is true (`ENABLE_CLEANUP_SCHEDULER`); the platform chart enables it by default, and a backend without the variable leaves it off.
The backend runs the scheduler in every replica, so keep the platform backend at one replica while the cleanup scheduler is enabled.
Each nightly task runs independently, so one failure does not skip the others, and every run is recorded in the `cleanup_runs` table.
Admins see the last run, instances pending teardown, and stuck states on the admin portal's Lifecycle page (`GET /admin/instance-lifecycle`).

A failed step is stored in `instances.lifecycle_error` and retried on the next run.
For Stripe-billed subscriptions the lifecycle rechecks the subscription with Stripe, so a lost or out-of-order webhook converges by the next night; if Stripe cannot be reached, nothing is stopped except the instances of an account pending deletion.
When a price cannot be mapped to a tier, the lifecycle logs a warning and still refreshes status and trial end so inactive subscriptions are held.
Changing a plan's `included_ai_budget_usd` redeploys every running instance of that tier, one after another, on its next reconcile.
Operator reprovisioning (`/system/provision`, admin provision) redeploys a held instance but keeps it stopped with its key disabled.
If an instance's OpenRouter key was deleted in OpenRouter or is missing from its instance Secret, re-provisioning replaces it.
Each subscription has at most one instance, so a concurrent second provision request for it gets `409`.

### Account Deletion

See [Account Deletion](../support.md#account-deletion).

After the cancellation window, the nightly job cancels every remaining Stripe subscription of the account and uninstalls every instance (Helm release, PVCs, instance Secrets, and the platform OpenRouter key).
It then deletes the account's instance, subscription, and audit-log rows, and finally the account's Supabase auth user and `accounts` row, so the email and login are gone and signing in cannot recreate the account.
Payment records and Stripe webhook event records are kept after the account is deleted with only their `account_id` cleared; they keep the Stripe customer and subscription identifiers, and webhook payloads can include the account ID (subscription metadata) and invoice contact details.

If a teardown, the row deletion, or the auth user deletion fails, the account keeps its `accounts` row, the run is recorded as failed with the error, and the next run retries from the start.
The admin portal's complete deletion (`DELETE /admin/accounts/{account_id}/complete`) runs the same teardown and deletions at once, without waiting for the cancellation window.
When a step fails it answers `500` and keeps the account row, although Stripe billing may already be cancelled and some instances uninstalled, so retry it.

## Release Deployment

Every release tag publishes versioned images (`ghcr.io/mindroom-ai/{platform-backend,platform-frontend,mindroom}:vYYYY.M.N`), and `cluster/scripts/deploy-release.sh` rolls one of them out to the hosted cluster.
Run it from a repository checkout that has the tag (`git fetch --tags`), with `kubectl` and `helm` pointed at the cluster, for example on the k3s node with `KUBECONFIG=/etc/rancher/k3s/k3s.yaml`.

```bash
cluster/scripts/deploy-release.sh v2026.9.351 --dry-run              # Show the plan and render the Helm upgrade
cluster/scripts/deploy-release.sh v2026.9.351                        # Platform plus running instances
cluster/scripts/deploy-release.sh v2026.9.351 --instances all        # Running plus lifecycle-held instances
cluster/scripts/deploy-release.sh v2026.9.351 --instances 1,7        # Only these instances
cluster/scripts/deploy-release.sh v2026.9.351 --instances none       # Platform only
```

The script performs these steps:

1. Pre-pull the three release images on the node with `sudo k3s crictl pull`, locally or over ssh when `NODE_SSH` is set.
2. Save the current platform Helm values to `BACKUP_DIR` (default `~/saas-deploy`), then `helm upgrade --wait` the `platform` release with the chart from the same tag, setting `imageTag`, `backendImageTag`, `frontendImageTag`, and `provisioner.instanceMindroomImage`.
3. Check `https://api.{domain}/health`, then re-provision each selected instance through `POST /system/provision` with its `subscription_id`, `account_id`, and `instance_id` from the `instances` table and the `tier` of its subscription, and wait for the `synapse-{id}` and `mindroom-{id}` rollouts.
4. Check `https://{id}.{baseDomain}/api/health` for every re-provisioned running instance, where `baseDomain` is `provisioner.instanceBaseDomain` or, when that is empty, `domain`.

It reads the domain, environment, Supabase URL, and platform Secret name from the release's computed Helm values, and reads `provisioner_api_key` and `supabase_service_key` from that Secret in the chart's `mindroom-{environment}` namespace; secrets are never printed.
`--dry-run` still performs these reads and the platform health check, but changes nothing and hides the rendered Helm output because it can contain secrets.
`NAMESPACE` (default `mindroom-production`) and `RELEASE` (default `platform`) select the Helm release to upgrade.
Re-provisioning rewrites the tenant Secret and applies the new MindRoom image, while the live tenant config on the instance PVC is left untouched.
Tenant Synapse runs `matrixdotorg/synapse:latest` with pull policy `Always` unless the platform chart sets `provisioner.instanceSynapseImage` and `provisioner.instanceSynapseImagePullPolicy`.
Set them there to pin or upgrade Synapse; the script keeps them across releases, and each instance switches when it is re-provisioned.
`/system/provision` is rate limited to five requests per minute, so the script waits between instances.
An instance held by the subscription lifecycle (`lifecycle_stopped_at` set) is redeployed and then scaled back to zero with its key disabled, as described in [Subscription Lifecycle](#subscription-lifecycle).
The script never re-provisions an instance that a customer or admin stopped manually (`stopped` without `lifecycle_stopped_at`), because re-provisioning would start it, and it refuses such ids when they are requested explicitly.
Starting a stopped instance through the portal or `/system/instances/{id}/start` only scales its existing deployments back up, so it keeps running the old MindRoom image until it is re-provisioned; run the script with `--instances <id>` after it has been started.
Deprovisioned instances are never re-provisioned either, because that would recreate them empty.
A failed instance does not stop the run; the script reports every failure at the end and exits non-zero.
When the node's memory requests are nearly full, rollout pods can stay `Pending`; stop idle instances or resize the node first.

### Database Migrations

Apply any new files from `saas-platform/supabase/migrations` before deploying a release whose backend depends on them.
Operators have no database password, so `cluster/scripts/db/apply-migration.sh` sends the SQL through the Supabase Management API (`POST https://api.supabase.com/v1/projects/{ref}/database/query`) with a personal access token:

```bash
SUPABASE_ACCESS_TOKEN=sbp_... SUPABASE_PROJECT_REF=<project-ref> \
  cluster/scripts/db/apply-migration.sh saas-platform/supabase/migrations/004_instance_lifecycle.sql
```

The script prints the API response and exits non-zero when the query fails.
The incremental migrations from `002` on are written to be re-runnable on a database that already has the baseline schema, while `000_consolidated_complete_schema.sql` is for fresh installs only.
Apply them in numeric order.
Snapshot the tables a migration touches before applying it, because the Management API cannot roll a committed query back.

The first run of `005_account_deletion.sql` restarts the 7-day grace period of every account whose deletion was requested more than 7 days earlier, because older releases left those instances running and billed and their owners could still cancel.
Without it, the first nightly cleanup after the upgrade would tear them down with no chance to cancel; reruns never restart a grace period again.
List those accounts before applying it, and consider telling their owners that their deletion completes 7 days after the upgrade unless they cancel it:

```sql
SELECT id, email, deleted_at FROM accounts WHERE deleted_at < NOW() - INTERVAL '7 days';
```

Apply migration `006` before deploying a backend that enforces account status, because that backend refuses every account whose status is missing.
Migration `006` sets missing account statuses to `active`, and it fails without changing anything while an account holds a status other than `active`, `suspended`, `deleted`, or `pending_verification`, so correct those rows first.

Migration `007_one_instance_per_subscription.sql` fails without changing anything while a subscription still has more than one instance row, and its error lists them.
Find them before applying it:

```sql
SELECT subscription_id, array_agg(instance_id ORDER BY instance_id), array_agg(status ORDER BY instance_id)
FROM instances GROUP BY subscription_id HAVING count(*) > 1;
```

For each subscription, decide with the customer which instance to keep, usually the running one.
For every other row, check `helm status instance-<instance_id> -n mindroom-instances`, because a `deprovisioned` row from an older release's soft delete can still have a live release.
If the release exists, uninstall it with `DELETE /admin/instances/<instance_id>/uninstall`, which also deletes its PVCs, Secrets, and platform OpenRouter key.
Then delete the row with `DELETE FROM instances WHERE instance_id = <instance_id>;` and run the query again until it returns nothing.

## Multi-Tenant Architecture

Each customer instance gets:

- Separate Kubernetes deployment in `mindroom-instances` namespace
- Isolated PersistentVolumeClaim for data
- Own Matrix/Synapse server (SQLite)
- Independent ConfigMap configuration
- Dedicated ingress routes

Tenants share the namespace, so their isolation comes from these controls:

- Tool code runs in the instance pod's sandbox-runner sidecar, no instance pod holds a Kubernetes API token, and the instance chart refuses dedicated Kubernetes workers.
- The namespace enforces the Pod Security `baseline` profile, which rejects privileged containers, host namespaces, `hostPath` volumes, and capabilities beyond the default set; Terraform creates it with that label, and the provisioner reapplies the label before every deployment.
- Each instance's NetworkPolicy admits service traffic only from the ingress controller and the same instance, and allows HTTP and HTTPS egress only to public addresses and the ingress controller, so metadata services, private networks including private node addresses, and other pods are unreachable on those ports.
  The chart finds the controller by namespace through `ingressControllerNamespace` (default `ingress-nginx`).
  kube-hetzner's nginx addon installs into `nginx` unless its `ingress_target_namespace` is set, so confirm the live namespace with `kubectl get pods -A -l app.kubernetes.io/name=ingress-nginx` and set `provisioner.instanceIngressControllerNamespace` in the platform chart to match; the provisioner passes it to every instance it deploys.
- Every instance container has an ephemeral-storage limit, and the sandbox runner's workspace `emptyDir` has a 1 GiB size limit (`sandboxRunnerWorkspaceSizeLimit`), so tool code that fills the disk gets only its own pod evicted.
  The matching ephemeral-storage requests stay at 64 MiB, because every tenant's requests count against the node's allocatable ephemeral storage and large ones would leave new tenant pods unschedulable.

Platform services run in `mindroom-{environment}` namespace.
The hosted SaaS chart currently runs Synapse per tenant, with server names such as `{customer}.mindroom.chat`.
The public `mindroom.chat` Matrix server is the separate Tuwunel server on `hetzner-matrix`.
Do not reuse `mindroom.chat` as the default SaaS tenant homeserver until the product has an explicit shared-homeserver mode with tenant isolation, provisioning, room ownership, SSO, and deprovisioning semantics.
The runtime-only chart is the better starting point for a future bring-your-own-homeserver or shared-homeserver mode.
