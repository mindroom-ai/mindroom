---
icon: lucide/ship
---

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

Fresh creation omits an instance ID and receives a generated ID; an existing instance for the subscription is returned or re-provisioned when deprovisioned.
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

```bash
helm upgrade --install instance-1 ./cluster/k8s/instance \
  --namespace mindroom-instances \
  --create-namespace \
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
For chart-managed worker egress with human-approved temporary hostname grants, see [Approved Egress](approved-egress.md).

## Worker Backends

The instance and runtime charts support two worker backend modes for worker-routed tools such as `coding`, `docker`, `file`, `python`, and `shell`.

The dedicated-worker provisioning flow is implemented today.

Both modes store agent data in the same per-agent directory structure.

| Helm value | Behavior | Best for |
|------------|----------|----------|
| `workerBackend: static_runner` | Runs one shared sandbox-runner sidecar inside the main MindRoom pod | Simpler deployments |
| `workerBackend: kubernetes` | Creates dedicated worker Deployments and Services on demand | Stronger runtime isolation per agent (filesystem isolation depends on `worker_scope`) |

### Shared Sidecar Mode

`workerBackend: static_runner` is the default.
The primary runtime talks to a shared sidecar over `localhost`.
This keeps the deployment simple, but all proxied tool calls share the same runner process.
The runner reads and writes the same agent storage directories as the main process by mounting only the storage PVC's `agents` and `private_instances` directories over its own `sandbox-runner` directory.
It cannot see the credential store, Matrix state, or other primary runtime state, and it never receives the credential encryption key.
The primary leases each proxied tool's saved settings to the runner per call.
When encrypted credential storage is enabled in Helm, configure the credential encryption key through a Secret-backed chart value; only the primary runtime receives it.
See [Kubernetes shared sidecar](sandbox-proxy.md#kubernetes-shared-sidecar-workerbackend-static_runner) for the exact mounts and remaining limits.

### Dedicated Worker Mode

`workerBackend: kubernetes` enables the built-in Kubernetes worker backend.
The primary runtime creates worker Deployments and Services on demand and routes tool calls to the matching worker.
Each worker pod runs the sandbox-runner app and accesses the same agent storage directory as every other runtime for that agent.
Worker-local files (caches, virtualenvs, metadata) are kept separate per worker.
When a worker is idle, its Deployment scales to zero, but agent data and worker caches are preserved.
The runtime chart stores derived worker tokens and optional credential-encryption keys as per-worker entries in one chart-created worker-auth Secret when workers run in the release namespace.
If `workers.kubernetes.namespace` is set to a separate worker namespace, the runtime chart can instead manage per-worker auth Secrets in that namespace.
The hosted instance chart stores derived worker tokens and optional credential-encryption keys as per-worker entries in a pre-created tenant auth Secret.
The hosted instance worker-manager Role does not grant broad Secret API access in the shared `mindroom-instances` namespace.

> [!WARNING]
> **Filesystem isolation depends on `worker_scope`.**
> With `shared`, `user_agent`, or unscoped execution, each worker can only see its own agent's storage directory — this is the strongest isolation available.
> With `user`, the worker can see all agents' storage because it shares one runtime across multiple agents for a single user.
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
| Instance chart (`storagePath`) | `/mindroom_data` | `/mindroom_data/knowledge/reference` |
| Direct backend without a mount override | `/app/worker` | `/app/worker/knowledge/reference` |

Both charts set `MINDROOM_KUBERNETES_WORKER_STORAGE_MOUNT_PATH`; the direct-backend fallback applies when that environment override is absent.
Custom chart values or runtime environment settings can select another root.
The worker mounts that directory from the existing worker-storage PVC with `subPath: <relative-path>` and `readOnly: true`.
The mount exposes the complete source directory, including files excluded from semantic indexing by include patterns, exclude patterns, or extension filters.
MindRoom does not copy or clone the source per agent.

If the source already lies inside a storage root visible to the worker's existing scope, the existing mount provides access and MindRoom does not add a nested duplicate mount.
Sources outside the shared worker-storage root are ignored so existing configurations continue to work without granting access to host-only paths.
Mount plans that would overlap another knowledge source or contain an existing scoped mount fail closed before a Deployment is created.
The final knowledge mount list is part of the worker pod-template hash, so reconciliation recreates workers whose mounted assignments are stale.

Typical Helm values look like:

```yaml
workerBackend: kubernetes
workerCleanupIntervalSeconds: 30
storageAccessMode: ReadWriteMany
controlPlaneNodeName: ""
kubernetesWorkerImage: ""
kubernetesWorkerImagePullPolicy: ""
kubernetesWorkerServiceAccountName: ""
kubernetesWorkerNamePrefix: "mindroom-worker"
kubernetesWorkerStorageSubpathPrefix: "workers"
kubernetesWorkerPort: 8766
kubernetesWorkerReadyTimeoutSeconds: 60
kubernetesWorkerIdleTimeoutSeconds: 1800
kubernetesWorkerRuntimeClassName: ""
sandbox_proxy_token: "replace-me"
```

The runtime chart exposes the same concepts under the nested `workers.*` values.

Important behavior and constraints:

- `kubernetesWorkerImage` and `kubernetesWorkerImagePullPolicy` default to the main MindRoom image settings when left empty.
- `workerCleanupIntervalSeconds` controls how often the primary runtime runs idle-worker cleanup.
- Worker pod-template drift (image, env, resources) is reconciled automatically: each cleanup pass recreates scaled-down worker Deployments whose pod template no longer matches the configured spec, and running workers are recreated on their next provisioning after they scale down.
- Reconciliation is controlled by `workers.kubernetes.reconcilePodTemplates` in the runtime chart (`MINDROOM_KUBERNETES_WORKER_RECONCILE_POD_TEMPLATES`, default on), so worker Deployments do not need manual recycling after image or pod-template changes.
- `kubernetesWorkerIdleTimeoutSeconds` controls when a worker is considered idle and eligible to scale down.
- `kubernetesWorkerReadyTimeoutSeconds` controls how long the primary runtime waits for a worker Deployment to become ready.
- `kubernetesWorkerPort` is the internal Service and container port used by dedicated workers.
- `kubernetesWorkerRuntimeClassName` selects one Kubernetes RuntimeClass for the entire dedicated-worker pool, including background-script workers. The runtime chart uses `workers.kubernetes.runtimeClassName`; direct deployments can set `MINDROOM_KUBERNETES_WORKER_RUNTIME_CLASS_NAME`. Leave it empty for the cluster default.
- Before selecting a RuntimeClass, verify that its handler is available on every eligible worker node and supports the configured storage driver, access mode, and mount behavior. Changing the value participates in worker reconciliation and can recreate existing workers when they are next ensured, so finish active work before changing it.
- Dedicated workers need access to the shared instance PVC so they can reach agent storage directories.
- For `shared`, `user_agent`, and unscoped execution, mounts are narrowed to just the target agent's directory plus the worker's scratch space.
- Shared credentials are copied into each dedicated worker as needed instead of exposing the whole shared credentials directory inside agent-isolated pods.
- Dedicated workers start with no shared credentials by default.
- Only services listed in `defaults.worker_grantable_credentials` are available inside a dedicated worker.
- `google_vertex_adc` is intentionally unsupported for dedicated workers because workers do not receive ADC files or `GOOGLE_APPLICATION_CREDENTIALS`; keep Vertex ADC usage in the primary runtime.
- `workers.kubernetes.extraEnv` and `MINDROOM_KUBERNETES_WORKER_ENV_JSON` are filtered before reaching worker pods or startup manifests: generated worker env, runtime control env, Kubernetes backend config env, and vendor telemetry env are dropped.
- The one sandbox-control value intentionally allowed through Kubernetes worker extra env is `MINDROOM_SANDBOX_RUNNER_SUBPROCESS_TIMEOUT_SECONDS`, so operators can tune runner subprocess timeouts.
- Dedicated worker runtime env stays deny-by-default for provider and arbitrary `.env` values, while basic runtime plumbing such as `PATH`, `VIRTUAL_ENV`, and linker vars is set separately.
- This matches the broader sandbox-proxy contract for `python` and `shell`: proxied execution is intentionally stricter than direct local execution and does not inherit ordinary runtime `.env` or provider env by default.
- For agent-editable per-workspace env (extra PATH entries, package indexes, npm cache dirs, etc.), use the request-time `.mindroom/worker-env.sh` overlay documented in [Sandbox Proxy Isolation](sandbox-proxy.md#workspace-env-hook-mindroomworker-envsh). The overlay is sourced inside the running worker per request, so it does not change the worker Deployment, the startup manifest, the pod-template hash, or any Helm value, and does not require a worker restart when edited.
- MindRoom-owned workspace identity, cache, and virtualenv env names remain controlled by the worker runtime and cannot be redirected by `.mindroom/worker-env.sh`: `HOME`, `MINDROOM_AGENT_WORKSPACE`, `XDG_CONFIG_HOME`, `XDG_DATA_HOME`, `XDG_STATE_HOME`, `XDG_CACHE_HOME`, `PIP_CACHE_DIR`, `UV_CACHE_DIR`, `PYTHONPYCACHEPREFIX`, and `VIRTUAL_ENV`.
- Worker-local caches may still live under `kubernetesWorkerStorageSubpathPrefix/<worker-dir>/`.

### Storage Requirements

Dedicated workers need access to the same PVC as the primary runtime.
Set `storageAccessMode: ReadWriteMany` so multiple workers can access agent storage concurrently.
If your storage class only supports `ReadWriteOnce`, set `controlPlaneNodeName` so the control plane and dedicated workers stay on the same node.
The chart enforces this constraint during template rendering.
For the hosted SaaS instance chart, keep the default `static_runner` backend on single-node clusters.
Switching hosted instances to dedicated Kubernetes workers on a multi-node cluster requires either a real `ReadWriteMany` storage class or explicit node pinning.

### RBAC And Network Policy

When `workerBackend: kubernetes` is enabled, the chart creates:

- A worker-manager ServiceAccount for the primary runtime.
- A Role and RoleBinding that allow managing worker Deployments and Services in the instance namespace.
- In the runtime chart's default same-namespace mode, a chart-created worker-auth Secret plus narrow `get` and `patch` access to only that Secret.
- In the runtime chart's explicit separate worker namespace mode, Secret CRUD for per-worker auth Secrets in that worker namespace.
- In the hosted instance chart, a pre-created tenant worker-auth Secret plus narrow `get` and `patch` access to only that Secret.
- NetworkPolicy rules that allow the primary runtime to reach the internal worker port while denying worker-to-worker runner ingress.

### Operations

The authenticated dashboard API exposes `/api/workers` to list active or idle workers and `/api/workers/cleanup` to trigger cleanup manually.
Dedicated workers are internal-only cluster Services and are authenticated with per-worker runner tokens derived from the primary runtime's `sandbox_proxy_token`.
See [Sandbox Proxy Isolation](sandbox-proxy.md) for the execution model, credential leases, and non-Kubernetes deployment modes.

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
| `/system/instances/{id}/uninstall` | DELETE | Remove an instance |
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
To enable credential encryption for an existing keyless instance, include `"enable_credentials_encryption": true` in the `POST /system/provision` request body.
Treat that opt-in as a one-way switch until a plaintext migration exists.
If an existing instance still has plaintext credential files, enabling credential encryption makes those files unreadable and encrypted-mode saves refuse to overwrite them.
Clear or replace stale plaintext credential files before enabling the flag.
If an already-encrypted instance has lost its instance Secret, pass `"enable_credentials_encryption": true` during reprovisioning so Helm receives the stable derived key again.

## Deployment Scripts

```bash
cd saas-platform
./deploy.sh platform-frontend          # Deploy platform frontend
./deploy.sh platform-backend           # Deploy platform backend
./redeploy-mindroom.sh         # Redeploy all customer MindRoom instances
```

## Multi-Tenant Architecture

Each customer instance gets:

- Separate Kubernetes deployment in `mindroom-instances` namespace
- Isolated PersistentVolumeClaim for data
- Own Matrix/Synapse server (SQLite)
- Independent ConfigMap configuration
- Dedicated ingress routes

Platform services run in `mindroom-{environment}` namespace.
The hosted SaaS chart currently runs Synapse per tenant, with server names such as `{customer}.mindroom.chat`.
The public `mindroom.chat` Matrix server is the separate Tuwunel server on `hetzner-matrix`.
Do not reuse `mindroom.chat` as the default SaaS tenant homeserver until the product has an explicit shared-homeserver mode with tenant isolation, provisioning, room ownership, SSO, and deprovisioning semantics.
The runtime-only chart is the better starting point for a future bring-your-own-homeserver or shared-homeserver mode.
