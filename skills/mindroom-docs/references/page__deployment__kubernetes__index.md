# Kubernetes Deployment

Deploy MindRoom on Kubernetes for production multi-tenant deployments.

## Architecture

MindRoom uses six Helm charts:

- **Instance Chart** (`cluster/k8s/instance/`) - Individual MindRoom runtime with bundled dashboard/API plus Matrix/Synapse
- **Platform Chart** (`cluster/k8s/platform/`) - SaaS control plane (API, frontend, provisioner)
- **Runtime Chart** (`cluster/k8s/runtime/`) - MindRoom runtime only, for clusters that provide Matrix, storage, secrets, ingress, and platform services externally
- **Tuwunel Chart** (`cluster/k8s/tuwunel/`) - Standalone Tuwunel homeserver (MindRoom fork) for clusters that pair the runtime chart with a chart-managed Matrix homeserver
- **Client Chart** (`cluster/k8s/client/`) - Standalone MindRoom web client behind unprivileged nginx, for clusters that already provide a homeserver, ingress, and TLS
- **MatrixRTC Chart** (`cluster/k8s/matrixrtc/`) - Optional LiveKit SFU and MatrixRTC authorization service for voice calls, paired with the client chart's MatrixRTC proxy

## Prerequisites

- Kubernetes cluster (tested with k3s via kube-hetzner)
- kubectl and helm installed
- NGINX Ingress Controller
- cert-manager (for TLS certificates)
- Hetzner Cloud Controller Manager and Hetzner CSI Driver when using `hcloud-volumes`

See [Hetzner Scaling Baseline](https://docs.mindroom.chat/deployment/saas-platform/#hetzner-scaling-baseline) and [Instance Deployment](https://docs.mindroom.chat/deployment/saas-platform/#instance-deployment).

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

### Chart Reference

Each chart's README documents its values: [runtime](https://github.com/mindroom-ai/mindroom/blob/main/cluster/k8s/runtime/README.md), [Tuwunel](https://github.com/mindroom-ai/mindroom/blob/main/cluster/k8s/tuwunel/README.md), [client](https://github.com/mindroom-ai/mindroom/blob/main/cluster/k8s/client/README.md), and [MatrixRTC](https://github.com/mindroom-ai/mindroom/blob/main/cluster/k8s/matrixrtc/README.md).
Optional features are described in these sections:

- [Background script gateway](https://github.com/mindroom-ai/mindroom/blob/main/cluster/k8s/runtime/README.md#background-script-gateway) (`scriptGateway`) lets dedicated workers run [background scripts](https://docs.mindroom.chat/tools/background-scripts/).
- [Content bundles](https://github.com/mindroom-ai/mindroom/blob/main/cluster/k8s/runtime/README.md#content-bundles) (`contentBundles`, `config.bootstrapContentBundle`) ship config, plugins, and skills as digest-pinned images, including how to update a bootstrapped config.
- [Session and knowledge storage](https://github.com/mindroom-ai/mindroom/blob/main/cluster/k8s/runtime/README.md#session-and-knowledge-storage) (`sessionStorage`, `knowledgeStorage`) and [runtime state storage](https://github.com/mindroom-ai/mindroom/blob/main/cluster/k8s/runtime/README.md#runtime-state-storage) (`stateStorage`, `stateStorage.extraSubPaths`) move data to volumes of their own; on an existing install, copy the data first.
- [Layering values files](https://github.com/mindroom-ai/mindroom/blob/main/cluster/k8s/runtime/README.md#layering-values-files) explains the map form of `env.extra`, `env.envFrom`, `extraVolumes`, and `extraVolumeMounts`, which Helm merges key by key across values files.
- [Agent Vault server NetworkPolicy](https://github.com/mindroom-ai/mindroom/blob/main/cluster/k8s/runtime/README.md#agent-vault-server-networkpolicy) restricts the chart-managed vault, and [`jobNaming: contentHash`](https://github.com/mindroom-ai/mindroom/blob/main/cluster/k8s/runtime/README.md#agent-vault-access-grants) reruns its Jobs under `kubectl apply` workflows.
- Tuwunel [structured settings](https://github.com/mindroom-ai/mindroom/blob/main/cluster/k8s/tuwunel/README.md#structured-settings) (`tuwunel.settings`) merge across values files, and [upgrades and database migrations](https://github.com/mindroom-ai/mindroom/blob/main/cluster/k8s/tuwunel/README.md#upgrades-and-database-migrations) explains how to upgrade the homeserver without corrupting its database.
- The [MatrixRTC chart](https://github.com/mindroom-ai/mindroom/blob/main/cluster/k8s/matrixrtc/README.md) runs the [voice call](https://docs.mindroom.chat/voice-calls/) backend, which the client chart's [`matrixRTC` values](https://github.com/mindroom-ai/mindroom/blob/main/cluster/k8s/client/README.md#matrixrtc-calls) announce and proxy.
- [Operational log events](https://docs.mindroom.chat/deployment/operational-log-events/) feed log-based metrics and alerts once `MINDROOM_LOG_FORMAT=json` is set through the runtime chart's `env.extra`.

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
The runner reads and writes the same agent storage directories as the main process by mounting only the storage PVC's `agents` and `private_instances` directories at their usual paths, next to its own `sandbox-runner` storage root.
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
The runtime chart stores derived worker tokens as per-worker entries in one chart-created worker-auth Secret when workers run in the release namespace.
Dedicated workers never receive the credential encryption key, matching dedicated Docker workers.
With encrypted credential storage enabled, the worker credential stores and the `.shared_credentials` mirror the primary writes are encrypted with that key, so worker code cannot read them and tool settings reach the worker only through [credential leases](https://docs.mindroom.chat/deployment/sandbox-proxy/#credential-leases).
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
- Worker pod-template drift (image, env, resources) is reconciled automatically: each cleanup pass recreates scaled-down worker Deployments whose pod template no longer matches the configured spec, and running workers are recreated on their next use.
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
- Only services listed in `defaults.worker_grantable_credentials` are mirrored into a dedicated worker's shared credentials.
- That allowlist also decides which shared settings scoped calls may lease, but it does not limit unscoped calls: as with the `static_runner` backend, unscoped calls still lease the called tool's saved settings from the primary credential store, including the `openai` and `groq` provider keys, because those tools share their service names with the model providers (see [Credential leases](https://docs.mindroom.chat/deployment/sandbox-proxy/#credential-leases)).
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

## Backup and Restore

This section covers the runtime chart and the Tuwunel chart.
Several volumes and Secrets only work together, so plan backups as restore units rather than as independent volumes.

### What each volume holds

| Volume | Values | Contents |
|--------|--------|----------|
| `<fullname>-storage` | `storage` | The storage root (`MINDROOM_STORAGE_PATH`, default `/app/agent_data`): `matrix_state.yaml` with each agent's Matrix account, device ID, and access token; `tracking/` with the event journal binding file and, with `eventCache.backend: sqlite`, the journal itself; `credentials/` and `private_oauth/` credential files and OAuth stores; agent and team sessions, learning, memory, and workspaces; `control_state/`; `knowledge_db/` and `knowledge_git/`; `logs/`; and `encryption_keys/` and `sync_continuity/` unless `stateStorage` is enabled |
| `<fullname>-state` | `stateStorage` | When enabled, the `encryption_keys/` and `sync_continuity/` directories: each agent's Matrix device store, which holds its E2EE keys and its Matrix sync position, and the pending join and decrypt fences; also each `stateStorage.extraSubPaths` directory, such as `tracking/` |
| `<fullname>-sessions` | `sessionStorage` | When enabled, the agent and team session databases (`MINDROOM_SESSION_STORAGE_PATH`) |
| `<fullname>-knowledge` | `knowledgeStorage` | When enabled, `knowledge_db/` |
| `<volumeName>-<fullname>-event-cache-postgres-0`, default `data-<fullname>-event-cache-postgres-0` | `eventCache.postgres.persistence` | The PostgreSQL event journal: admitted Matrix events, turn records, the response outbox, and history debt |
| `<server.name>-data`, default `agent-vault-data` | `workers.kubernetes.agentVault.server.persistence` | The chart-managed Agent Vault server's data, protected by its master password |
| `<approved egress name>-data`, default `<fullname>-egress-proxy-data` | `approvedEgress.persistence` | Temporary hostname grants of the approved egress proxy |
| Tuwunel `<fullname>-data` | Tuwunel chart `storage` | The homeserver's RocksDB database and its `media/` directory |

Data that `stateStorage`, `sessionStorage`, or `knowledgeStorage` mount lives on those volumes instead of the storage claim.
Back up and restore the sessions volume, like a session store relocated with `MINDROOM_SESSION_STORAGE_PATH`, together with the storage claim.
[Docker Data Persistence](https://docs.mindroom.chat/deployment/storage/#data-persistence) describes the storage root's directories in more detail.

### Restore units

Restore each of these sets together, from the same point in time:

- **Credentials encryption key and credential stores.**
  With `MINDROOM_CREDENTIALS_ENCRYPTION_KEY` set (for example through `workers.sandbox.credentialsEncryptionKey.existingSecret`), credential files and OAuth stores on the storage claim are encrypted under that key.
  Under a different key MindRoom cannot decrypt them and logs `Failed to load encrypted credentials`, and with a different or missing key it refuses to overwrite them, so back up the key's Secret value with the storage claim.
- **Agent Vault volume and its master password.**
  The `AGENT_VAULT_MASTER_PASSWORD` key in `workers.kubernetes.agentVault.bootstrapSecretName` protects the vault's encryption key, so keep that Secret, including its `AGENT_VAULT_OWNER_PASSWORD`, with every vault snapshot.
- **Event journal, journal binding, and Matrix device stores.**
  `tracking/event_journal_binding.json` must name the journal database's generation, or startup refuses the journal as never used or as a different journal.
  The journal also records, per agent, the last Matrix sync batch admitted from that agent's device store and rejects batches out of sequence, so the journal and `encryption_keys/` must come from the same moment.
  Keep `matrix_state.yaml` and `sync_continuity/` with them.
  A restored chart-managed PostgreSQL volume keeps the password it was created with, while the chart generates a new random one when `<fullname>-event-cache-postgres-auth` no longer exists, so back up that password with the volume.
  To keep it, set `eventCache.postgres.auth.password` to it, or set both `eventCache.postgres.auth.existingSecret` and `eventCache.databaseUrl.existingSecret` to a Secret holding the password and the matching connection URL, because the auth Secret alone leaves the runtime without a database URL.
- **Homeserver and runtime Matrix state.**
  Each agent's access token and device exist on both sides, and the homeserver holds the device's published encryption keys.
  If the homeserver no longer knows an agent's stored token or device, or reports a different one, that agent does not start.
  If the runtime is older than the homeserver, encryption keys received after the runtime snapshot are lost, and the journal does not know about anything that happened after it, including which later messages were already answered.
  When both are lost, restore both from the same point.

### Snapshot consistency

Per-volume snapshots taken at different times while MindRoom runs are not a consistent restore set for the event journal and the device stores.
Take them from one quiet point instead:

1. Scale the runtime Deployment to zero and wait until its Pod has terminated, then scale dedicated worker Deployments (label `mindroom.ai/worker-id`) to zero and wait until their Pods have terminated, because workers write agent workspaces to the storage claim and a terminating Pod can still write.
2. Snapshot the storage claim, the sessions claim if enabled, the state claim, and the event journal volume, or dump the journal database with `pg_dump`.
   When the homeserver belongs to the same restore set, back it up in this window too, as described below for the Tuwunel volume.
3. Once the snapshots are taken or the dump has finished, scale the runtime back up; it recreates workers on demand.

A storage-level group snapshot that captures all coupled volumes at the same instant behaves like the whole runtime crashing at that instant, which the journal is designed to recover from: a batch committed before a crash is recognized when the device store delivers it again.
The Agent Vault, approved egress, and knowledge volumes are not coupled to the journal and can be snapshotted on their own schedules.
For the Tuwunel volume, prefer an offline snapshot or Tuwunel's own database backups, since copying the files of a running RocksDB database does not produce a consistent copy, and Tuwunel's online backups do not include media; see the fork's [backup guide](https://github.com/mindroom-ai/mindroom-tuwunel/blob/main/docs/backups.md).

### Rebuildable data

- `knowledge_db/` holds only the knowledge indexes and their metadata.
  When an index is missing, agents report the base as initializing and MindRoom rebuilds it in the background from the knowledge sources the next time the base is used, which repeats every embedding call.
  The sources themselves, including files uploaded through the dashboard, are not rebuildable unless they come from Git: a Git-backed base restores its files from the configured repository when its folder is missing or empty, reusing `<storage>/knowledge_git/` when it survives and initializing it again when it does not.
- `logs/` only holds runtime log files.
- The approved egress volume only holds temporary grants, which expire after at most `approvedEgress.maxTtlSeconds`, while static allowlists come from values.

Everything else on the storage, sessions, state, and journal volumes is not rebuildable.

### Restoring

1. Restore the volumes before installing the charts, then point the charts at them with `storage.existingClaim`, `stateStorage.existingClaim`, `sessionStorage.existingClaim`, `knowledgeStorage.existingClaim`, `workers.kubernetes.agentVault.server.persistence.existingClaim`, `approvedEgress.persistence.existingClaim`, and the Tuwunel chart's `storage.existingClaim`.
   A PersistentVolumeClaim restored under the StatefulSet's claim name, `<volumeName>-<fullname>-event-cache-postgres-0`, is adopted by the chart-managed PostgreSQL, so set its original password as described in [Restore units](#restore-units); with an external database, restore the database and keep its URL Secret.
   To restore a `pg_dump` instead, install with a new journal volume and `replicaCount: 0`, load the dump into it, then set `replicaCount` back to 1, so the runtime never opens a partly loaded journal.
2. Recreate the Secrets with their original values, including the credentials encryption key, the Agent Vault bootstrap Secret, and the Matrix registration token or application-service registration.
3. Start Tuwunel first, then the runtime.
   `The bound Matrix device store is missing`, `Matrix stream changed`, or `IngestionBatchSequenceError` in the runtime log means the device stores do not match the journal, and an event journal binding error means the binding file and journal database do not match; restore the matching backups rather than deleting state, because automatic device replacement is unsupported.

See [Moving a journal safely](https://docs.mindroom.chat/deployment/storage/#moving-a-journal-safely) for copying or adopting a journal database, and [Device recovery after the upgrade](https://docs.mindroom.chat/deployment/upgrades/#device-recovery-after-the-upgrade) for device-store recovery.

See [SaaS Platform](https://docs.mindroom.chat/deployment/saas-platform/#secrets-management) and [Database Migrations](https://docs.mindroom.chat/deployment/saas-platform/#database-migrations).
