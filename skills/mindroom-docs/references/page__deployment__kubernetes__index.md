# Kubernetes Deployment

This page covers running MindRoom on Kubernetes with Helm: the available charts, a runtime-only install, worker backends for isolated tool execution, and backup and restore.
For the hosted multi-tenant platform (platform and instance charts), see [SaaS Platform](https://docs.mindroom.chat/deployment/saas-platform/).

## Charts

| Chart | Path | Use |
|-------|------|-----|
| Runtime | `cluster/k8s/runtime/` | MindRoom runtime only, for clusters that provide Matrix, storage, secrets, ingress, and platform services |
| Tuwunel | `cluster/k8s/tuwunel/` | Standalone Tuwunel homeserver (MindRoom fork) to pair with the runtime chart |
| Client | `cluster/k8s/client/` | Standalone MindRoom web client behind unprivileged nginx, for clusters that already provide a homeserver, ingress, and TLS |
| MatrixRTC | `cluster/k8s/matrixrtc/` | Optional LiveKit SFU and MatrixRTC authorization service for [voice calls](https://docs.mindroom.chat/voice-calls/), proxied by the client chart |
| Instance | `cluster/k8s/instance/` | One hosted MindRoom runtime with bundled dashboard/API and Matrix/Synapse, deployed by the SaaS platform |
| Platform | `cluster/k8s/platform/` | SaaS control plane (API, frontend, provisioner) |

## Prerequisites

- A Kubernetes cluster (tested with k3s via kube-hetzner)
- `kubectl` and `helm`
- NGINX Ingress Controller
- cert-manager for TLS certificates
- Hetzner Cloud Controller Manager and Hetzner CSI Driver when using `hcloud-volumes`

For Hetzner sizing, see [Hetzner Scaling Baseline](https://docs.mindroom.chat/deployment/saas-platform/#hetzner-scaling-baseline).

## Runtime-Only Deployment

Use the runtime chart when you already operate the surrounding platform and only want Kubernetes to run the MindRoom runtime.
It does not create Matrix, ingress, a model gateway, or platform services.
For a chart-managed homeserver, deploy the Tuwunel chart alongside it and point `matrix.homeserverUrl`, `matrix.serverName`, and `matrix.registrationToken` at it as described in `cluster/k8s/tuwunel/README.md`.

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

The runtime chart reads provider API keys from Kubernetes Secrets listed in `providerCredentials` (see [Provider API Keys from Kubernetes Secrets](https://github.com/mindroom-ai/mindroom/blob/main/cluster/k8s/runtime/README.md#provider-api-keys-from-kubernetes-secrets)).
Its `env` value is a map with `extra` and `envFrom`, not a list of environment variables.
The hosted instance chart instead mounts provider keys as files under `/etc/secrets/` and sets the matching [`*_API_KEY_FILE`](https://docs.mindroom.chat/configuration/models/#file-based-secrets) variables.

For passwordless agent accounts ([`appservice` mode](https://docs.mindroom.chat/matrix/#agent-users)), set `matrix.managedAccountAuth: appservice` and point `matrix.appserviceToken` at a Secret holding the registration's `as_token`, and leave `matrix.registrationToken` unset (see [Matrix Managed Account Authentication](https://github.com/mindroom-ai/mindroom/blob/main/cluster/k8s/runtime/README.md#matrix-managed-account-authentication)).
On the Tuwunel chart, mount the complete registration YAML from a Secret through `tuwunel.appserviceRegistration`, whose `key` is also the mounted filename and must end in `.yaml` or `.yml`, and optionally disable password login with `login_with_password: false` in `tuwunel.settings` (see [Passwordless MindRoom Accounts](https://github.com/mindroom-ai/mindroom/blob/main/cluster/k8s/tuwunel/README.md#passwordless-mindroom-accounts)).

### Chart Reference

Each chart's README documents its full values: [runtime](https://github.com/mindroom-ai/mindroom/blob/main/cluster/k8s/runtime/README.md), [Tuwunel](https://github.com/mindroom-ai/mindroom/blob/main/cluster/k8s/tuwunel/README.md), [client](https://github.com/mindroom-ai/mindroom/blob/main/cluster/k8s/client/README.md), and [MatrixRTC](https://github.com/mindroom-ai/mindroom/blob/main/cluster/k8s/matrixrtc/README.md).
Optional features:

- [Background script gateway](https://github.com/mindroom-ai/mindroom/blob/main/cluster/k8s/runtime/README.md#background-script-gateway) (`scriptGateway`) lets dedicated workers run [background scripts](https://docs.mindroom.chat/tools/background-scripts/).
- [Content bundles](https://github.com/mindroom-ai/mindroom/blob/main/cluster/k8s/runtime/README.md#content-bundles) (`contentBundles`, `config.bootstrapContentBundle`) ship config, plugins, and skills as digest-pinned images, including how to update a bootstrapped config.
- [Session and knowledge storage](https://github.com/mindroom-ai/mindroom/blob/main/cluster/k8s/runtime/README.md#session-and-knowledge-storage) (`sessionStorage`, `knowledgeStorage`) and [runtime state storage](https://github.com/mindroom-ai/mindroom/blob/main/cluster/k8s/runtime/README.md#runtime-state-storage) (`stateStorage`, `stateStorage.extraSubPaths`) move data to volumes of their own; on an existing install, copy the data first.
- [Layering values files](https://github.com/mindroom-ai/mindroom/blob/main/cluster/k8s/runtime/README.md#layering-values-files) explains the map form of `env.extra`, `env.envFrom`, the Agent Vault server's `extraEnv` and `envFrom`, `extraVolumes`, and `extraVolumeMounts`, which Helm merges key by key across values files, and lists which env values are rendered with `tpl`.
- [Approved Egress](https://docs.mindroom.chat/deployment/approved-egress/) gives workers human-approved temporary hostname grants.
- [Agent Vault server NetworkPolicy](https://github.com/mindroom-ai/mindroom/blob/main/cluster/k8s/runtime/README.md#agent-vault-server-networkpolicy) restricts the chart-managed vault, and [`jobNaming: contentHash`](https://github.com/mindroom-ai/mindroom/blob/main/cluster/k8s/runtime/README.md#agent-vault-access-grants) reruns its Jobs under `kubectl apply` workflows.
- Tuwunel [structured settings](https://github.com/mindroom-ai/mindroom/blob/main/cluster/k8s/tuwunel/README.md#structured-settings) (`tuwunel.settings`) merge across values files, and [upgrades and database migrations](https://github.com/mindroom-ai/mindroom/blob/main/cluster/k8s/tuwunel/README.md#upgrades-and-database-migrations) explains how to upgrade the homeserver without corrupting its database.
- The client chart's [`matrixRTC` values](https://github.com/mindroom-ai/mindroom/blob/main/cluster/k8s/client/README.md#matrixrtc-calls) announce and proxy the MatrixRTC chart's call backend.
- [Operational log events](https://docs.mindroom.chat/deployment/operational-log-events/) feed log-based metrics and alerts once `MINDROOM_LOG_FORMAT=json` is set through `env.extra`.

## Worker Backends

Worker-routed tools such as `coding`, `docker`, `file`, `python`, and `shell` run in one of two backends.
Which tools are routed and how `worker_scope` shares workers are described in [Sandbox Proxy Isolation](https://docs.mindroom.chat/deployment/sandbox-proxy/).

| Helm value | Behavior | Best for |
|------------|----------|----------|
| `workers.backend: static_runner` (default) | One shared sandbox-runner sidecar inside the MindRoom pod | Simpler deployments |
| `workers.backend: kubernetes` | Dedicated worker Deployments and Services created on demand | Isolation between agents or users |

The hosted instance chart supports only `static_runner` (`workerBackend`) and fails rendering for any other backend, because its tenants share one namespace.
Deploy the runtime chart in a namespace of its own when an instance needs dedicated workers.

### Shared Sidecar Mode

The primary runtime reaches the sidecar over `localhost`, and all proxied tool calls share one runner process.
The sidecar shares agent workspaces with the primary but cannot read the credential store, Matrix state, or the credentials encryption key, and it is not an isolation boundary between agents.
To enable encrypted credential storage, set `workers.sandbox.credentialsEncryptionKey.existingSecret`; only the primary runtime receives that key.
See [Kubernetes shared sidecar](https://docs.mindroom.chat/deployment/sandbox-proxy/#kubernetes-shared-sidecar-workerbackend-static_runner) for its mounts and limits.

### Dedicated Worker Mode

With `workers.backend: kubernetes`, the primary runtime creates a worker Deployment and Service per worker on demand and routes tool calls to it.
Idle workers scale to zero, keeping agent data and worker caches.
Each worker keeps its own caches and virtualenvs under `workers.kubernetes.storageSubpathPrefix`.
[Worker scopes](https://docs.mindroom.chat/deployment/sandbox-proxy/#worker-scopes) describes which workspaces each worker mounts, and [Credential leases](https://docs.mindroom.chat/deployment/sandbox-proxy/#credential-leases) describes how tool settings reach workers.
`google_vertex_adc` tools must run in the primary runtime, because workers receive no Google application default credentials.

Typical Helm values:

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
    idleTimeoutSeconds: 1800
    readyTimeoutSeconds: 60
    runtimeClassName: ""
```

#### Worker settings

The runtime chart sets these environment variables from its values; set them directly when deploying the backend without Helm.
Further options, such as extra labels, annotations, volumes, containers, and the seccomp profile, are in `cluster/k8s/runtime/values.yaml` and `src/mindroom/workers/backends/kubernetes_config.py`.

| Helm value | Environment variable | Default | Effect |
|------------|----------------------|---------|--------|
| `workers.kubernetes.image` | `MINDROOM_KUBERNETES_WORKER_IMAGE` | Chart: the main MindRoom image; required without Helm | Worker image |
| `workers.kubernetes.namespace` | `MINDROOM_KUBERNETES_WORKER_NAMESPACE` | Chart: the release namespace; otherwise the pod namespace or `default` | Namespace for worker resources |
| _(storage claim)_ | `MINDROOM_KUBERNETES_WORKER_STORAGE_PVC_NAME` | Chart: the runtime storage claim; required without Helm | PVC holding agent workspaces |
| `storage.mountPath` | `MINDROOM_KUBERNETES_WORKER_STORAGE_MOUNT_PATH` | Chart: `/app/agent_data`; otherwise `/app/worker` | Where workers mount storage |
| `workers.kubernetes.storageSubpathPrefix` | `MINDROOM_KUBERNETES_WORKER_STORAGE_SUBPATH_PREFIX` | `workers` | Storage directory for worker-local caches |
| `workers.kubernetes.port` | `MINDROOM_KUBERNETES_WORKER_PORT` | `8766` | Internal Service and container port |
| `workers.kubernetes.readyTimeoutSeconds` | `MINDROOM_KUBERNETES_WORKER_READY_TIMEOUT_SECONDS` | `60` | How long the primary waits for a worker to become ready |
| `workers.kubernetes.idleTimeoutSeconds` | `MINDROOM_KUBERNETES_WORKER_IDLE_TIMEOUT_SECONDS` | `1800` | Idle time before a worker may scale to zero |
| `workers.cleanupIntervalSeconds` | `MINDROOM_WORKER_CLEANUP_INTERVAL_SECONDS` | Chart: `30`; otherwise `0` (no automatic cleanup) | How often idle-worker cleanup runs |
| `workers.kubernetes.reconcilePodTemplates` | `MINDROOM_KUBERNETES_WORKER_RECONCILE_POD_TEMPLATES` | `true` | Recreate workers whose image, env, or resources changed |
| `workers.kubernetes.namePrefix` | `MINDROOM_KUBERNETES_WORKER_NAME_PREFIX` | `mindroom-worker` | Prefix for worker resource names |
| `workers.kubernetes.runtimeClassName` | `MINDROOM_KUBERNETES_WORKER_RUNTIME_CLASS_NAME` | Cluster default | RuntimeClass for all dedicated workers, including background-script workers |
| `workers.kubernetes.resources` | `MINDROOM_KUBERNETES_WORKER_CPU_REQUEST`, `_CPU_LIMIT`, `_MEMORY_REQUEST`, `_MEMORY_LIMIT` | `100m`, `500m`, `256Mi`, `1Gi` | Worker container resources |
| `workers.kubernetes.scriptResources` | `MINDROOM_KUBERNETES_DEFAULT_SCRIPT_RESOURCE_PROFILE`, `MINDROOM_KUBERNETES_SCRIPT_RESOURCE_PROFILES_JSON` | `small`; profiles in `values.yaml` | The `small`, `standard`, and `large` [background script](https://docs.mindroom.chat/tools/background-scripts/) resource profiles, all three required |
| `workers.kubernetes.nodeName`, `colocateWithControlPlaneNode` | `MINDROOM_KUBERNETES_WORKER_NODE_NAME`, `MINDROOM_KUBERNETES_WORKER_COLOCATE_WITH_CONTROL_PLANE_NODE` | Unset, `false` | Pin workers to one node (see [Storage Requirements](#storage-requirements)) |
| `workers.kubernetes.extraEnv` | `MINDROOM_KUBERNETES_WORKER_ENV_JSON` | `{}` | Extra environment variables for worker pods |
| _(`env.extra`)_ | `MINDROOM_KUBERNETES_WORKER_TMP_SIZE_LIMIT` | `1Gi` | Size of each worker's `/tmp` |
| _(`env.extra`)_ | `MINDROOM_KUBERNETES_WORKER_USER_RESOURCES_JSON` | `{}` | Per-requester worker resources |

Changing the image, environment, resources, or RuntimeClass recreates scaled-down workers on the next cleanup pass and running workers on their next use, so no manual recycling is needed; finish active work before changing them.
Before selecting a RuntimeClass, verify that its handler is available on every eligible worker node and supports the configured storage driver, access mode, and mount behavior.

Each worker's `/tmp` is disk-backed, and exceeding its size limit evicts that worker pod rather than refusing writes.
`MINDROOM_KUBERNETES_WORKER_TMP_SIZE_LIMIT` (for example `4Gi`) also requests and limits the same amount of container `ephemeral-storage`, which clusters such as GKE Autopilot otherwise cap at 1 GiB.
Tool subprocesses already get a `TMPDIR` on persistent storage, so only explicit `/tmp` paths use this space.

`MINDROOM_KUBERNETES_WORKER_USER_RESOURCES_JSON` maps a Matrix user ID to `requests` and/or `limits` with `cpu` and/or `memory`, for example `{"@alice:example.org": {"limits": {"memory": "4Gi"}}}`.
Listed quantities replace the global values for that requester's `user` and `user_agent` workers; shared and unscoped workers, other requesters, and background-script profiles are unchanged.

`workers.kubernetes.extraEnv` drops MindRoom's own control variables and vendor telemetry variables, except `MINDROOM_SANDBOX_RUNNER_SUBPROCESS_TIMEOUT_SECONDS`, which tunes runner subprocess timeouts.
Workers do not inherit the runtime's `.env` or provider keys; for per-workspace variables such as `PATH` entries or package indexes, use the [workspace env hook](https://docs.mindroom.chat/deployment/sandbox-proxy/#workspace-env-hook-mindroomworker-envsh), which takes effect without redeploying.

### Knowledge Source Visibility

Dedicated workers can read the knowledge-base source directories assigned to the agents they serve, mounted read-only at the same path relative to the worker storage mount.
A `user` worker sees the knowledge of every `worker_scope: user` agent; other workers see only their own agent's.

| Deployment | Worker storage mount | Visible path for `knowledge/reference` |
| --- | --- | --- |
| Runtime chart (`storage.mountPath`) | `/app/agent_data` | `/app/agent_data/knowledge/reference` |
| Direct backend without a mount override | `/app/worker` | `/app/worker/knowledge/reference` |

The worker sees the complete source directory, including files excluded from indexing, and MindRoom does not copy or clone it per agent.
Only sources inside the shared storage root are mounted.
A source inside a workspace the worker already mounts stays reachable through that writable workspace mount.
Sources that are missing, reached through a link, or inside another agent's workspace, a private instance, or a worker directory are skipped with a logged warning or error.
A knowledge source that overlaps another knowledge source or another worker mount prevents the worker from starting.
Changing knowledge assignments recreates the affected workers.

### Storage Requirements

Dedicated workers mount the runtime's storage PVC.
Set `storage.accessModes` to `ReadWriteMany` so several workers can use it at once.
If your storage class only supports `ReadWriteOnce`, keep the runtime and workers on one node: set `workers.kubernetes.colocateWithControlPlaneNode: true`, or set `workers.kubernetes.nodeName` and pin the runtime to that same node with `nodeSelector` or `affinity`.

### RBAC And Network Policy

With `workers.backend: kubernetes`, the runtime chart creates:

- A worker-manager ServiceAccount for the primary runtime.
- A Role and RoleBinding that manage worker Deployments and Services in the worker namespace.
- Access to worker auth Secrets: one chart-created Secret in the release namespace, or per-worker Secrets when `workers.kubernetes.namespace` names a separate namespace.
- NetworkPolicy rules that let the primary reach the worker port and deny worker-to-worker runner traffic.

That Role reaches every Deployment and Service in its namespace, so give each runtime release a namespace of its own.
Label that namespace with `pod-security.kubernetes.io/enforce=baseline` so admission rejects privileged containers, host namespaces, and `hostPath` volumes; the chart's runtime and worker pods satisfy that profile.

### Operations

To list active and idle workers or run an idle-worker cleanup pass, use the dashboard's [Workers API](https://docs.mindroom.chat/dashboard/#workers-api).

## Backup and Restore

This section covers the runtime chart and the Tuwunel chart.
Several volumes and Secrets only work together, so back them up and restore them as units.

### What each volume holds

| Volume | Values | Contents |
|--------|--------|----------|
| `<fullname>-storage` | `storage` | The storage root (`MINDROOM_STORAGE_PATH`, default `/app/agent_data`): `matrix_state.yaml` with each agent's Matrix account and access token; `tracking/`; `credentials/` and `private_oauth/`; agent and team sessions, learning, memory, and workspaces; `control_state/`; `knowledge_db/` and `knowledge_git/`; `logs/`; and `encryption_keys/` and `sync_continuity/` unless `stateStorage` is enabled |
| `<fullname>-state` | `stateStorage` | When enabled, `encryption_keys/` (each agent's Matrix device store with its E2EE keys and sync position) and `sync_continuity/`, plus each `stateStorage.extraSubPaths` directory, such as `tracking/` |
| `<fullname>-sessions` | `sessionStorage` | When enabled, the agent and team session databases (`MINDROOM_SESSION_STORAGE_PATH`) |
| `<fullname>-knowledge` | `knowledgeStorage` | When enabled, `knowledge_db/` |
| `<volumeName>-<fullname>-event-cache-postgres-0`, default `data-<fullname>-event-cache-postgres-0` | `eventCache.postgres.persistence` | The PostgreSQL event journal, which records which messages were already answered |
| `<server.name>-data`, default `agent-vault-data` | `workers.kubernetes.agentVault.server.persistence` | The chart-managed Agent Vault server's data |
| `<approved egress name>-data`, default `<fullname>-egress-proxy-data` | `approvedEgress.persistence` | Temporary hostname grants of the approved egress proxy |
| Tuwunel `<fullname>-data` | Tuwunel chart `storage` | The homeserver's RocksDB database and its `media/` directory |

Back up the sessions volume together with the storage claim.
[Data Persistence](https://docs.mindroom.chat/deployment/storage/#data-persistence) describes the storage root's directories in more detail.

### Restore units

Restore each of these sets together, from the same point in time:

- **Credentials encryption key and credential stores.**
  With `MINDROOM_CREDENTIALS_ENCRYPTION_KEY` set (for example through `workers.sandbox.credentialsEncryptionKey.existingSecret`), credential files and OAuth stores are encrypted under that key.
  Under a different key MindRoom logs `Failed to load encrypted credentials` and will not overwrite them, so back up the key's Secret value with the storage claim.
- **Agent Vault volume and its bootstrap Secret.**
  The Secret named by `workers.kubernetes.agentVault.bootstrapSecretName`, with its `AGENT_VAULT_MASTER_PASSWORD` and `AGENT_VAULT_OWNER_PASSWORD`, must accompany every vault snapshot.
- **Event journal, `tracking/`, and Matrix device stores.**
  The journal, `tracking/event_journal_binding.json`, `encryption_keys/`, `matrix_state.yaml`, and `sync_continuity/` must come from the same moment, or startup refuses the journal or the device stores.
  A restored chart-managed PostgreSQL volume keeps its original password, while the chart generates a new one when `<fullname>-event-cache-postgres-auth` is missing, so back up that password too.
  To reuse it, set `eventCache.postgres.auth.password`, or set both `eventCache.postgres.auth.existingSecret` and `eventCache.databaseUrl.existingSecret` to a Secret holding the password and the matching connection URL.
- **Homeserver and runtime Matrix state.**
  If the homeserver no longer knows an agent's stored access token or device, that agent does not start.
  If the runtime snapshot is older than the homeserver, encryption keys received since then are lost and agents may answer messages again.
  When both are lost, restore both from the same point.

### Snapshot consistency

Volume snapshots taken at different times while MindRoom runs are not a consistent restore set.
Take them from one quiet point:

1. Scale the runtime Deployment to zero and wait until its Pod has terminated, then scale dedicated worker Deployments (label `mindroom.ai/worker-id`) to zero and wait until their Pods have terminated, because workers write to the storage claim.
2. Snapshot the storage claim, the sessions claim if enabled, the state claim, and the event journal volume, or dump the journal database with `pg_dump`.
   When the homeserver belongs to the same restore set, back it up in this window too.
3. Scale the runtime back up; it recreates workers on demand.

A storage-level group snapshot that captures all these volumes at the same instant is also safe without stopping MindRoom.
The Agent Vault, approved egress, and knowledge volumes can be snapshotted on their own schedules.
For the Tuwunel volume, use an offline snapshot or Tuwunel's own database backups, which do not include media; see the fork's [backup guide](https://github.com/mindroom-ai/mindroom-tuwunel/blob/main/docs/backups.md).

### Rebuildable data

- `knowledge_db/` holds only knowledge indexes.
  A missing index is rebuilt in the background the next time the base is used, repeating every embedding call, and agents report the base as initializing meanwhile.
  The sources, including files uploaded through the dashboard, are not rebuildable unless they come from Git, in which case they are cloned again from the configured repository.
- `logs/` only holds runtime log files.
- The approved egress volume only holds temporary grants, which expire after at most `approvedEgress.maxTtlSeconds`.

Everything else on the storage, sessions, state, and journal volumes is not rebuildable.

### Restoring

1. Restore the volumes before installing the charts, then point the charts at them with `storage.existingClaim`, `stateStorage.existingClaim`, `sessionStorage.existingClaim`, `knowledgeStorage.existingClaim`, `workers.kubernetes.agentVault.server.persistence.existingClaim`, `approvedEgress.persistence.existingClaim`, and the Tuwunel chart's `storage.existingClaim`.
   The chart-managed PostgreSQL adopts a claim restored under `<volumeName>-<fullname>-event-cache-postgres-0`; set its original password as described in [Restore units](#restore-units).
   With an external database, restore the database and keep its URL Secret.
   To restore a `pg_dump` instead, install with a new journal volume and `replicaCount: 0`, load the dump, then set `replicaCount` back to 1.
2. Recreate the Secrets with their original values, including the credentials encryption key, the Agent Vault bootstrap Secret, and the Matrix registration token or application-service registration.
3. Start Tuwunel first, then the runtime.

If the runtime log shows `The bound Matrix device store is missing`, `Matrix stream changed`, or `IngestionBatchSequenceError`, the device stores do not match the journal; an event journal binding error means `tracking/` and the journal database do not match.
Restore the matching backups instead of deleting state, because automatic device replacement is unsupported.
See [Moving a journal safely](https://docs.mindroom.chat/deployment/storage/#moving-a-journal-safely) for copying or adopting a journal database, and [Device recovery after the upgrade](https://docs.mindroom.chat/deployment/upgrades/#device-recovery-after-the-upgrade) for device-store recovery.

See [SaaS Platform](https://docs.mindroom.chat/deployment/saas-platform/#secrets-management) and [Database Migrations](https://docs.mindroom.chat/deployment/saas-platform/#database-migrations).
