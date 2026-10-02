# Kubernetes Deployment

Deploy MindRoom on Kubernetes for production multi-tenant deployments.

## Architecture

MindRoom uses five Helm charts:

- **Instance Chart** (`cluster/k8s/instance/`) - Individual MindRoom runtime with bundled dashboard/API plus Matrix/Synapse
- **Platform Chart** (`cluster/k8s/platform/`) - SaaS control plane (API, frontend, provisioner)
- **Runtime Chart** (`cluster/k8s/runtime/`) - MindRoom runtime only, for clusters that provide Matrix, storage, secrets, ingress, and platform services externally
- **Tuwunel Chart** (`cluster/k8s/tuwunel/`) - Standalone Tuwunel homeserver (MindRoom fork) for clusters that pair the runtime chart with a chart-managed Matrix homeserver
- **Client Chart** (`cluster/k8s/client/`) - Standalone MindRoom web client behind unprivileged nginx, for clusters that already provide a homeserver, ingress, and TLS

## Prerequisites

- Kubernetes cluster (tested with k3s via kube-hetzner)
- kubectl and helm installed
- NGINX Ingress Controller
- cert-manager (for TLS certificates)
- Hetzner Cloud Controller Manager and Hetzner CSI Driver when using `hcloud-volumes`

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

Only enable trusted upstream auth when the instance is behind a verified access layer that strips client-supplied copies of those headers and injects authenticated values itself:

```bash
helm upgrade --install instance-1 ./cluster/k8s/instance \
  --namespace mindroom-instances \
  --reset-then-reuse-values \
  --set-string trustedUpstreamAuth.enabled=true \
  --set trustedUpstreamAuth.userIdHeader=X-MindRoom-User-Id \
  --set trustedUpstreamAuth.emailHeader=X-MindRoom-User-Email \
  --set trustedUpstreamAuth.matrixUserIdHeader=X-MindRoom-Matrix-User-Id \
  --set trustedUpstreamAuth.emailToMatrixUserIdTemplate='@{localpart}:example.org' \
  --set trustedUpstreamAuth.emailDomain=example.com
```

When using the provisioner, configure the platform chart with `provisioner.trustedUpstreamAuth.enabled="true"` and the matching `provisioner.trustedUpstreamAuth.*Header` values.
If your access layer cannot supply a Matrix ID header, configure `provisioner.trustedUpstreamAuth.emailToMatrixUserIdTemplate` with the same template and `provisioner.trustedUpstreamAuth.emailDomain` with the allowed email domain.
The email-to-Matrix template must contain exactly one `{localpart}` placeholder and requires the matching `emailHeader` and `emailDomain` values in both the instance and platform chart configuration.

## Runtime-Only Deployment

Use the runtime chart when you already operate the surrounding platform and only want Kubernetes to run the MindRoom runtime.

The chart intentionally does not create Matrix, ingress, a model gateway, or platform services.
For a chart-managed homeserver, deploy the Tuwunel chart (`cluster/k8s/tuwunel/`) alongside it and point `matrix.homeserverUrl`, `matrix.serverName`, and `matrix.registrationToken` at it as described in `cluster/k8s/tuwunel/README.md`.

```bash
helm upgrade --install mindroom-runtime ./cluster/k8s/runtime \
  --namespace mindroom \
  --create-namespace \
  -f runtime-values.yaml
```

Typical production values point at existing resources:

```yaml
config:
  create: false
  existingConfigMap: mindroom-config
  key: config.yaml

storage:
  create: false
  existingClaim: mindroom-data

matrix:
  homeserverUrl: http://matrix.example.svc.cluster.local:8008
  serverName: example.com
  registrationToken:
    existingSecret: mindroom-secrets
    key: MATRIX_REGISTRATION_TOKEN

env:
  envFrom:
    - secretRef:
        name: mindroom-secrets

workers:
  backend: kubernetes
  sandbox:
    proxyToken:
      existingSecret: mindroom-sandbox-proxy
      key: MINDROOM_SANDBOX_PROXY_TOKEN
```

See `cluster/k8s/runtime/README.md` and `cluster/k8s/runtime/values.yaml` for the full values surface.
For chart-managed worker egress with human-approved temporary hostname grants, see [Approved Egress](https://docs.mindroom.chat/deployment/approved-egress/).

## Worker Backends

The runtime chart supports two worker backend modes for worker-routed tools such as `coding`, `docker`, `file`, `python`, and `shell`.

Both modes store agent data in the same per-agent directory structure.

| Helm value | Behavior | Best for |
|------------|----------|----------|
| `workers.backend: static_runner` | Runs one shared sandbox-runner sidecar inside the main MindRoom pod | Simpler deployments |
| `workers.backend: kubernetes` | Creates dedicated worker Deployments and Services on demand | Stronger runtime isolation per agent (filesystem isolation depends on `worker_scope`) |

The hosted instance chart runs only the shared sidecar (`workerBackend: static_runner`) and fails rendering for any other backend.
Its tenants share the `mindroom-instances` namespace, and Kubernetes RBAC cannot keep one tenant's worker manager away from other tenants' Deployments, Services, PVCs, and Secrets there.
Deploy the runtime chart in a namespace of its own when an instance needs dedicated workers.

### Shared Sidecar Mode

The shared sidecar is the default in both charts.
The primary runtime talks to a shared sidecar over `localhost`.
This keeps the deployment simple, but all proxied tool calls share the same runner process.
The runner reads and writes the same agent storage directories as the main process by mounting only the storage PVC's `agents` and `private_instances` directories over its own `sandbox-runner` directory.
It cannot see the credential store, Matrix state, or other primary runtime state, and it never receives the credential encryption key.
The primary leases each proxied tool's saved settings to the runner per call.
The runner mounts no config, so each call carries the primary's [live config snapshot](https://docs.mindroom.chat/deployment/sandbox-proxy/#live-config-snapshots), limited to the fields runners resolve, and the runner resolves the requesting agent and its settings from it.
Because the runner shares the pod network, the charts give the primary a generated `MINDROOM_API_KEY` unless Supabase authentication or an explicit opt-out is configured.
When encrypted credential storage is enabled in Helm, configure the credential encryption key through a Secret-backed chart value; only the primary runtime receives it.
See [Kubernetes shared sidecar](https://docs.mindroom.chat/deployment/sandbox-proxy/#kubernetes-shared-sidecar-workerbackend-static_runner) for the exact mounts and remaining limits.

### Dedicated Worker Mode

`workers.backend: kubernetes` enables the built-in Kubernetes worker backend in the runtime chart.
The primary runtime creates worker Deployments and Services on demand and routes tool calls to the matching worker.
Each worker pod runs the sandbox-runner app and mounts the same agent workspace as every other runtime for that agent; the agent's sessions, memory, and learning data stay with the primary.
Worker-local files (caches, virtualenvs, metadata) are kept separate per worker.
When a worker is idle, its Deployment scales to zero, but agent data and worker caches are preserved.
Worker pods can reach the primary API over the pod network, so the runtime chart also gives the primary a generated `MINDROOM_API_KEY` in this mode unless the explicit opt-out is configured; worker pods never receive that key.
The runtime chart stores derived worker tokens and optional credential-encryption keys as per-worker entries in one chart-created worker-auth Secret when workers run in the release namespace.
If `workers.kubernetes.namespace` is set to a separate worker namespace, the runtime chart can instead manage per-worker auth Secrets in that namespace.

> [!WARNING]
> **Filesystem isolation depends on `worker_scope`.**
> With `shared`, `user_agent`, or unscoped execution, each worker can only see its own agent's workspace — this is the strongest isolation available.
> With `user`, the worker can see the workspaces of every non-private `worker_scope: user` agent, plus that user's own private workspaces, because it shares one runtime across those agents for a single user; it never mounts agents on other scopes.
> Use `user_agent` for per-agent filesystem isolation.

### Knowledge Source Visibility

Dedicated Kubernetes workers expose assigned knowledge-base source directories only when the resolved source is inside the shared worker-storage root.
The worker key and resolved agent policy determine which agent assignments are visible.
Shared, unscoped, and `user_agent` worker keys select their encoded agent, while a `user` worker key selects every configured agent that resolves to `worker_scope: user` because those agents share that worker.
Knowledge bases assigned to other agents and configured knowledge bases with no matching assignment are not mounted.

For a source at `<shared-storage-root>/<relative-path>`, the worker-visible path is `<worker-storage-mount>/<relative-path>`.
The effective default depends on the deployment:

| Deployment | Worker storage mount | Visible path for `knowledge/reference` |
| --- | --- | --- |
| Runtime chart (`storage.mountPath`) | `/app/agent_data` | `/app/agent_data/knowledge/reference` |
| Direct backend without a mount override | `/app/worker` | `/app/worker/knowledge/reference` |

The runtime chart sets `MINDROOM_KUBERNETES_WORKER_STORAGE_MOUNT_PATH`; the direct-backend fallback applies when that environment override is absent.
Custom chart values or runtime environment settings can select another root.
The worker mounts that directory from the existing worker-storage PVC with `subPath: <relative-path>` and `readOnly: true`.
The mount exposes the complete source directory, including files excluded from semantic indexing by include patterns, exclude patterns, or extension filters.
MindRoom does not copy or clone the source per agent.

If the source already lies inside a workspace the worker mounts, that writable workspace mount provides access and MindRoom does not add a nested duplicate mount.
Sources outside the shared worker-storage root are ignored so existing configurations continue to work without granting access to host-only paths.
MindRoom plans each mount from the configured path, not its link target: a source that is missing or reached through a link is skipped with a warning, and a source inside another agent's workspace, a private instance, or a worker root is refused with an error, because kubelet follows links inside the volume when it mounts and those directories are written by other workers.
Mount plans that would overlap another knowledge source or contain an existing scoped mount fail closed before a Deployment is created.
The final knowledge mount list is part of the worker pod-template hash, so reconciliation recreates workers whose mounted assignments are stale.

Typical Helm values look like:

```yaml
storage:
  accessModes:
    - ReadWriteMany
workers:
  backend: kubernetes
  cleanupIntervalSeconds: 30
  sandbox:
    proxyToken:
      existingSecret: mindroom-sandbox-proxy
      key: MINDROOM_SANDBOX_PROXY_TOKEN
  kubernetes:
    image:
      repository: ""
      pullPolicy: ""
    namePrefix: mindroom-worker
    storageSubpathPrefix: workers
    port: 8766
    readyTimeoutSeconds: 60
    idleTimeoutSeconds: 1800
    runtimeClassName: ""
```

Important behavior and constraints:

- `workers.kubernetes.image` defaults to the main MindRoom image settings when its repository is left empty.
- `workers.cleanupIntervalSeconds` controls how often the primary runtime runs idle-worker cleanup.
- Worker pod-template drift (image, env, resources) is reconciled automatically: each cleanup pass recreates scaled-down worker Deployments whose pod template no longer matches the configured spec, and running workers are recreated on their next provisioning after they scale down.
- Reconciliation is controlled by `workers.kubernetes.reconcilePodTemplates` in the runtime chart (`MINDROOM_KUBERNETES_WORKER_RECONCILE_POD_TEMPLATES`, default on), so worker Deployments do not need manual recycling after image or pod-template changes.
- `workers.kubernetes.idleTimeoutSeconds` controls when a worker is considered idle and eligible to scale down.
- `workers.kubernetes.readyTimeoutSeconds` controls how long the primary runtime waits for a worker Deployment to become ready.
- `workers.kubernetes.port` is the internal Service and container port used by dedicated workers.
- `workers.kubernetes.runtimeClassName` selects one Kubernetes RuntimeClass for the entire dedicated-worker pool, including background-script workers; direct deployments can set `MINDROOM_KUBERNETES_WORKER_RUNTIME_CLASS_NAME`. Leave it empty for the cluster default.
- Before selecting a RuntimeClass, verify that its handler is available on every eligible worker node and supports the configured storage driver, access mode, and mount behavior. Changing the value participates in worker reconciliation and can recreate existing workers when they are next ensured, so finish active work before changing it.
- Dedicated workers need access to the runtime's storage PVC so they can reach agent workspaces.
- Each worker's `/tmp` is a disk-backed `emptyDir` with a 1 GiB size limit; kubelet enforces it by evicting only that worker pod once usage exceeds it, so writes are not refused at the limit as on a tmpfs.
- `MINDROOM_KUBERNETES_WORKER_TMP_SIZE_LIMIT` (for example `4Gi`, set through `env.extra` in the runtime chart) raises that limit and also requests and limits the same amount of container `ephemeral-storage`, because kubelet counts `/tmp` against the pod's container ephemeral-storage limits and some clusters, such as GKE Autopilot, apply a 1 GiB default. Tool subprocesses already get a `TMPDIR` on the worker's persistent storage, so only explicit `/tmp` paths use this space.
- `MINDROOM_KUBERNETES_WORKER_USER_RESOURCES_JSON` (also set through `env.extra`) sizes chosen requesters' workers differently: a JSON object mapping a Matrix user ID to `requests` and/or `limits` with `cpu` and/or `memory` quantities, for example `{"@alice:example.org": {"limits": {"memory": "4Gi"}}}`. Listed quantities replace the global worker values for that requester's `user` and `user_agent` workers, unlisted ones keep them, and shared or unscoped workers, other requesters, and background-script resource profiles are unchanged. Changing an entry rebuilds that requester's workers through pod-template reconciliation.
- For `shared`, `user_agent`, and unscoped execution, mounts are narrowed to just the target agent's workspace plus the worker's scratch space; each workspace is a `subPath` mount at its canonical path.
- Shared credentials are copied into each dedicated worker as needed instead of exposing the whole shared credentials directory inside agent-isolated pods.
- Dedicated workers start with no shared credentials by default.
- Only services listed in `defaults.worker_grantable_credentials` are available inside a dedicated worker.
- `google_vertex_adc` is intentionally unsupported for dedicated workers because workers do not receive ADC files or `GOOGLE_APPLICATION_CREDENTIALS`; keep Vertex ADC usage in the primary runtime.
- `workers.kubernetes.extraEnv` and `MINDROOM_KUBERNETES_WORKER_ENV_JSON` are filtered before reaching worker pods or startup manifests: generated worker env, runtime control env, Kubernetes backend config env, and vendor telemetry env are dropped.
- The one sandbox-control value intentionally allowed through Kubernetes worker extra env is `MINDROOM_SANDBOX_RUNNER_SUBPROCESS_TIMEOUT_SECONDS`, so operators can tune runner subprocess timeouts.
- Dedicated worker runtime env stays deny-by-default for provider and arbitrary `.env` values, while basic runtime plumbing such as `PATH`, `VIRTUAL_ENV`, and linker vars is set separately.
- This matches the broader sandbox-proxy contract for `python` and `shell`: proxied execution is intentionally stricter than direct local execution and does not inherit ordinary runtime `.env` or provider env by default.
- For agent-editable per-workspace env (extra PATH entries, package indexes, npm cache dirs, etc.), use the request-time `.mindroom/worker-env.sh` overlay documented in [Sandbox Proxy Isolation](https://docs.mindroom.chat/deployment/sandbox-proxy/#workspace-env-hook-mindroomworker-envsh). The overlay is sourced inside the running worker per request, so it does not change the worker Deployment, the startup manifest, the pod-template hash, or any Helm value, and does not require a worker restart when edited.
- MindRoom-owned workspace identity, cache, and virtualenv env names remain controlled by the worker runtime and cannot be redirected by `.mindroom/worker-env.sh`: `HOME`, `MINDROOM_AGENT_WORKSPACE`, `XDG_CONFIG_HOME`, `XDG_DATA_HOME`, `XDG_STATE_HOME`, `XDG_CACHE_HOME`, `PIP_CACHE_DIR`, `UV_CACHE_DIR`, `PYTHONPYCACHEPREFIX`, and `VIRTUAL_ENV`.
- Worker-local caches may still live under `workers.kubernetes.storageSubpathPrefix/<worker-dir>/`.

### Storage Requirements

Dedicated workers need access to the same PVC as the primary runtime.
Set `storage.accessModes` to `ReadWriteMany` so multiple workers can access agent storage concurrently.
If your storage class only supports `ReadWriteOnce`, set `workers.kubernetes.colocateWithControlPlaneNode: true` or an explicit `workers.kubernetes.nodeName` so the control plane and dedicated workers stay on the same node.

### RBAC And Network Policy

When `workers.backend: kubernetes` is enabled, the runtime chart creates:

- A worker-manager ServiceAccount for the primary runtime.
- A Role and RoleBinding that allow managing worker Deployments and Services in the worker namespace.
- In the default same-namespace mode, a chart-created worker-auth Secret plus narrow `get` and `patch` access to only that Secret.
- In the explicit separate worker namespace mode, Secret CRUD for per-worker auth Secrets in that worker namespace.
- NetworkPolicy rules that allow the primary runtime to reach the internal worker port while denying worker-to-worker runner ingress.

That Role reaches every Deployment and Service in its namespace, so give each runtime release a namespace of its own.
Label that namespace with `pod-security.kubernetes.io/enforce=baseline`, so admission rejects privileged containers, host namespaces, and `hostPath` volumes in pods created there; the chart's runtime and worker pods satisfy that profile.

### Operations

The authenticated dashboard API exposes `/api/workers` to list active or idle workers and `/api/workers/cleanup` to trigger cleanup manually.
Dedicated workers are internal-only cluster Services and are authenticated with per-worker runner tokens derived from the primary runtime's `sandbox_proxy_token`.
See [Sandbox Proxy Isolation](https://docs.mindroom.chat/deployment/sandbox-proxy/) for the execution model, credential leases, and non-Kubernetes deployment modes.

## Secrets Management

The hosted instance chart mounts provider keys as files under `/etc/secrets/` and sets the matching `*_API_KEY_FILE` variables:

```yaml
env:
  - name: ANTHROPIC_API_KEY_FILE
    value: "/etc/secrets/anthropic_key"
  - name: OPENROUTER_API_KEY_FILE
    value: "/etc/secrets/openrouter_key"
```

The standalone runtime chart instead reads `providerCredentials` Secret keys into direct provider environment variables.
Its `env` values shape is a map with `extra` and `envFrom`, not the list shown above for the hosted instance container.

For production SaaS instance provisioning, the instance chart is rendered with `instanceSecrets.create=false`, `instanceSecrets.name`, and a non-secret `instanceSecrets.hash`.
After Helm completes, the platform backend applies `mindroom-api-keys-{instance_id}` directly with Kubernetes so legacy chart-managed Secret pruning cannot remove it.
Tenant API keys and OIDC client secrets do not enter Helm release values or rendered Helm Secret manifests.
Tenant workloads are untrusted, so the instance Secret holds only instance-scoped credentials and never the platform's Supabase service-role key.
The one exception is the platform-wide Matrix OIDC client secret, which only Synapse mounts; the MindRoom container mounts just the provider key and registration secret files it reads.
The sandbox proxy token is random per instance.
Each provision removes Secret keys the provisioner no longer writes, because `kubectl apply` keeps keys dropped from `stringData`.
Upgrading from releases that copied platform credentials into instance Secrets requires rotating the Supabase service-role key and any platform provider keys copied by `cluster/scripts/update-api-keys.sh`, then re-provisioning every instance with `POST /system/provision` and its `instance_id`.
Standalone chart installs that set the retired `supabaseServiceKey` value keep that key in the chart-created Secret until it is deleted manually.

## Ingress

Each instance gets three hosts:

- `{customer}.{baseDomain}` - MindRoom dashboard and API
- `{customer}.api.{baseDomain}` - Direct API access
- `{customer}.matrix.{baseDomain}` - Matrix/Synapse server

## Platform Deployment

```bash
# Create values file from example
cp cluster/k8s/platform/values-staging.example.yaml cluster/k8s/platform/values-staging.yaml
# Edit with your configuration

helm upgrade --install platform ./cluster/k8s/platform \
  -f ./cluster/k8s/platform/values-staging.yaml \
  --namespace staging --create-namespace
```

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

Platform ingress hosts:

- `app.{domain}` - Platform frontend
- `api.{domain}` - Platform backend API
- `webhooks.{domain}/webhooks/stripe` - Stripe webhooks

The backend keys its rate limits and its authentication-failure lockout on the client address.
It takes that address from `X-Real-IP` only when the connection comes from a network in `trustedProxyCidrs` (`TRUSTED_PROXY_CIDRS`), and keys every other caller by its own connection address.
The default lists the private IPv4 ranges, so an in-cluster ingress-nginx controller, which overwrites `X-Real-IP`, is trusted, while instance pods cannot reach the backend port to use that trust.
Narrow the list to the controller's pod network when you know it, and include only proxies that overwrite `X-Real-IP`.
Without `TRUSTED_PROXY_CIDRS`, as in the Docker Compose setup, every caller is keyed by its connection address.

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
Keep this source stable because changing it changes future derived credential encryption keys.
New provisioned instances receive a derived credential encryption key by default.
When re-provisioning an existing instance, the provisioner preserves the current encryption state by reusing `credentials_encryption_key` from the existing instance Secret when present.
An existing instance whose MindRoom storage PVC no longer exists, such as one torn down after its grace period, starts on an empty volume and also receives the derived key.
To enable credential encryption for an existing keyless instance, include `"enable_credentials_encryption": true` in the `POST /system/provision` request body.
Treat that opt-in as a one-way switch until a plaintext migration exists.
If an existing instance still has plaintext credential files, enabling credential encryption makes those files unreadable and encrypted-mode saves refuse to overwrite them.
Clear or replace stale plaintext credential files before enabling the flag.
If an already-encrypted instance has lost its instance Secret, pass `"enable_credentials_encryption": true` during reprovisioning so Helm receives the stable derived key again.

## Subscription Lifecycle

Hosted instances follow their subscription, and one backend module (`services/instance_lifecycle.py`) owns that behavior.
Stripe subscription and invoice webhooks reconcile the account's instances in a background task after the webhook response, so Kubernetes or OpenRouter trouble never fails a webhook.
The nightly cleanup job at 03:00 UTC reconciles every subscription that owns an instance the same way, which also catches missed webhooks and retries failed steps.

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
A failed step is stored in `instances.lifecycle_error` and retried on the next run.
Before stopping, resuming, or redeploying a Stripe-billed instance, and for every Stripe-billed subscription during the nightly run, the lifecycle asks Stripe for the current subscription and corrects its stored status, tier, price, limits, and billing periods, so a lost or out-of-order webhook converges by the next night; if Stripe cannot be reached, nothing is stopped except the instances of an account pending deletion.
A correction is only written while the row is still bound to the Stripe subscription that was queried, so a resubscription that lands during the query is never overwritten.
A delayed creation event for a Stripe subscription older than the account's current one is ignored.
Right before teardown the job re-reads the subscription and skips the teardown when it is entitled again.
An instance's platform OpenRouter key must match its subscription tier's included budget (`included_ai_budget_usd` in `pricing-config.yaml`), and its `instances.tier`, which provisioning records only after a successful deploy, must match the subscription's tier, so a plan change or a resubscription on another tier never hands back a key or resources from a pricier tier.
Plan changes update the existing OpenRouter key's spending limit in place, preserving its identity and accumulated usage; a tier with no included budget sets a zero-dollar limit.
If OpenRouter reports that the stored key is gone or its instance Secret has no key value, provisioning revokes and clears the stale key before creating a replacement.
A subscription with no recorded positive succeeded payment is stored as `unpaid` when Stripe reports `past_due`.
When a price cannot be mapped to a tier, the lifecycle logs a warning and still refreshes status and trial end so inactive subscriptions are held.
A customer-stopped instance is not redeployed, because that would start it; after the customer starts it, the lifecycle redeploys it for its tier in the background.
Changing a plan's `included_ai_budget_usd` redeploys every running instance of that tier, one after another, on its next reconcile.
Re-provisioning never shrinks an instance's volumes, because Kubernetes refuses to shrink a PVC; a downgrade from `pro` keeps its larger volumes.
Checkout grants a plan's trial only to a Stripe customer who never had a trial, so cancelling and checking out again starts a paid subscription.
After migration `007` the database allows one instance per subscription (`instances.subscription_id` is unique), so concurrent provision requests on several backend replicas create at most one instance; the losing request gets `409`.
Re-provisioning a `deprovisioned` instance claims the row only while it is still `deprovisioned`, whether a customer's provision request or a lifecycle resume after teardown does it, so concurrent runs on several replicas deploy it once and mint one OpenRouter key.
A losing provision request gets `409`, and a losing lifecycle resume, including one a provision request started for a held instance, stops without enabling a key or clearing the hold.
Every provision records a newly created OpenRouter key only while the instance records no key, and before it writes the key into the instance Secret, so when concurrent redeploys of one instance each create a key, the first recorded key is kept and the others are deleted before their run publishes them or deploys the instance; a key whose record fails to save is deleted too.
If writing the instance Secret then fails, the provision deletes its key and clears the recorded key, unless another run has recorded a different key in the meantime.
A new instance or a redeploy that the lifecycle holds while it is being provisioned is scaled back to zero with its key disabled.
When a subscription with a trial is created for a customer who had an earlier trial, it is cancelled while that earlier subscription still runs and otherwise has its trial ended at once, so checkout sessions opened side by side can neither yield a second trial nor bill the customer twice; a redelivered event for such a cancelled subscription leaves the account's subscription alone.
Operator reprovisioning (`/system/provision`, admin provision) redeploys a held instance but keeps it stopped with its key disabled.
Each nightly task runs independently, so one failure does not skip the others, and every run is recorded in the `cleanup_runs` table.
The cleanup job only runs when `cleanupScheduler.enabled` is true (`ENABLE_CLEANUP_SCHEDULER`); the backend defaults it to off.
Admins see the last run, instances pending teardown, and stuck states on the admin portal's Lifecycle page (`GET /admin/instance-lifecycle`).
Customers whose instance is stopped for an inactive subscription see a dashboard banner with the teardown date and a link to billing.
The backend runs the scheduler in every replica, so keep the platform backend at one replica while the cleanup scheduler is enabled.

### Account Deletion

A customer's deletion request (`POST /my/gdpr/request-deletion`) first sets every renewing Stripe subscription of its customer to end at the end of its current billing period (`cancel_at_period_end`) and marks it with the `mindroom_ends_for_account_deletion` metadata key.
A subscription the customer had already set to end within its paid period (`cancel_at`) keeps that end; one set to end later is moved to the period end, and the marker remembers the customer's date so that cancelling the deletion restores it.
If Stripe fails, the request returns `502`, the subscriptions it had already set to end are set back, and the account is not deleted.
It then marks the account pending deletion and stops its instances with their platform OpenRouter keys disabled; the response says so when stopping failed and will be retried.
Only once the deletion is recorded does it cancel `incomplete` and `paused` subscriptions, which have no paid period to finish; a failure there is retried by the nightly run.
An account pending deletion never runs instances, whatever Stripe reports, so its instances stay stopped during the grace period even while its subscription is still paid, and the nightly run keeps them stopped even while Stripe is unreachable.
Such an account cannot provision or start instances, open a checkout or the billing portal, or cancel or reactivate its subscription (`409`) until the deletion is cancelled, and an instance being provisioned for it is kept stopped.
Each nightly run repeats these Stripe steps for accounts still inside their grace period, which retries a step the request could not finish and also covers deletions requested before these steps existed; it skips an account the customer restored meanwhile and undoes its own change when the restore lands while it runs.
Cancelling the deletion (`POST /my/gdpr/cancel-deletion`) restores only the account and lets the marked subscriptions renew again; its instances restart once a subscription is entitled, which for a subscription whose period ended meanwhile means a new checkout.
A customer who cancels or reactivates a subscription through `/my/subscription/cancel` or `/my/subscription/reactivate` also clears the marker, so a later cancelled deletion never renews a subscription the customer chose to end.
After the 7-day grace period, cancelling returns `409`, because `restore_account` refuses by the database clock.
The nightly job then claims the account with `claim_account_hard_delete`, which uses the same clock and makes `restore_account` refuse the account from then on, so a restore can never land during its teardown.
It then cancels every remaining Stripe subscription at once and uninstalls every instance of the account (Helm release, PVCs, instance Secrets, and the platform OpenRouter key).
`hard_delete_account`, which only acts on a claimed account, then deletes the account's instance, subscription, and audit-log rows.
Last, the job deletes the account's Supabase auth user through the admin API, which also removes the `accounts` row (`ON DELETE CASCADE`), so the email and login are gone and signing in cannot recreate the account.
Payment records and Stripe webhook event records are kept after the account is deleted with only their `account_id` cleared (`ON DELETE SET NULL`); they keep the Stripe customer and subscription identifiers, and webhook payloads can include the account ID (subscription metadata) and invoice contact details.
Soft delete keeps a `suspended` status and `restore_account` only restores a `deleted` one, so cancelling a deletion never lifts a suspension.
If a teardown, the hard delete, or the auth user deletion fails, the account keeps its `accounts` row, the run is recorded as failed with the error, and the next run retries from the start.
The admin portal's complete deletion (`DELETE /admin/accounts/{account_id}/complete`) marks the account pending deletion and claims it at once, runs the same teardown, calls `hard_delete_account`, and then deletes the auth user, which takes the account row with it.
When a step fails it answers `500` and keeps the account row, although Stripe billing may already be cancelled and some instances uninstalled, so retry it.
The nightly run, and any reconcile of an account pending deletion, marks instances that an older release's soft delete left `deprovisioned` while their deployment kept running as `running` again; the lifecycle then holds them, or keeps them running for an entitled subscription.
A held instance of an account pending deletion is never uninstalled by its own teardown date, even when `cleanupScheduler.teardownGraceDays` is shorter than 7 days; the account's cleanup removes it once the customer can no longer cancel, and until then its teardown date keeps moving forward, so a restored account's instance gets the full grace period.

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
