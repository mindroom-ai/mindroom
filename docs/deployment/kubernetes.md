---
icon: simple/kubernetes
---

# Kubernetes Deployment

This page covers running MindRoom on Kubernetes with Helm: a local `kind` quick start, the available charts, a production runtime-only install, worker backends for isolated tool execution, and backup and restore.
For the hosted multi-tenant platform (platform and instance charts), see [SaaS Platform](saas-platform.md).

## Quick Start: Local kind Cluster

Deploy MindRoom and a standalone Tuwunel Matrix homeserver on a local [`kind`](https://kind.sigs.k8s.io/) cluster and verify a live agent reply (no `sudo` required).

**Before you start:**

- Docker running, with [`kind`](https://kind.sigs.k8s.io/docs/user/quick-start/#installation), [`kubectl`](https://kubernetes.io/docs/tasks/tools/), and [`helm`](https://helm.sh/docs/intro/install/) on your `PATH`.
- `git`, `python3`, and `openssl`.
- `OPENAI_API_KEY` exported (for other providers, see [Provider API Keys from Kubernetes Secrets](https://github.com/mindroom-ai/mindroom/blob/main/cluster/k8s/runtime/README.md#provider-api-keys-from-kubernetes-secrets)).

### 1. Create the cluster and Secrets

```bash
: "${OPENAI_API_KEY:?export OPENAI_API_KEY first}"
git clone https://github.com/mindroom-ai/mindroom.git "$HOME/mindroom"
cd "$HOME/mindroom"
kind create cluster --name mindroom
kubectl create namespace mindroom
openssl rand -hex 32 | tr -d '\n' | kubectl -n mindroom create secret generic matrix-registration --from-file=MATRIX_REGISTRATION_TOKEN=/dev/stdin
printf %s "$OPENAI_API_KEY" | kubectl -n mindroom create secret generic llm-credentials --from-file=OPENAI_API_KEY=/dev/stdin
```

### 2. Install Tuwunel

Install the homeserver first with `mindroom.local` as the local server name (Tuwunel fixes `serverName` on first start and cannot change it without wiping its database):

```bash
cd "$HOME/mindroom"
helm upgrade --install matrix ./cluster/k8s/tuwunel --namespace mindroom \
  --set tuwunel.serverName=mindroom.local \
  --set tuwunel.registrationToken.existingSecret=matrix-registration \
  --wait --timeout 10m
```

### 3. Install the MindRoom runtime

This values file configures one model, one agent (`helper`), and one room (`lobby`).
`join_policy: public` lets the test user join `#lobby:mindroom.local` (see [Authorization](../authorization.md#responder-access)).
Custom `config.data` must also set `event_journal.backend: postgres` so MindRoom uses the chart's bundled PostgreSQL journal:

```bash
cd "$HOME/mindroom"
cat <<'YAML' > runtime-values.yaml
matrix:
  homeserverUrl: http://matrix-mindroom-tuwunel.mindroom.svc.cluster.local:8008
  serverName: mindroom.local
  registrationToken:
    existingSecret: matrix-registration
providerCredentials:
  - provider: openai
    existingSecret: llm-credentials
    key: OPENAI_API_KEY
config:
  data: |
    models:
      default:
        provider: openai
        id: gpt-6-luna
    rooms:
      lobby:
        display_name: Lobby
        description: Quick-start chat room
        join_policy: public
    agents:
      helper:
        display_name: Helper
        role: A concise, helpful assistant.
        rooms: [lobby]
    event_journal:
      backend: postgres
YAML
helm upgrade --install mindroom-runtime ./cluster/k8s/runtime --namespace mindroom -f runtime-values.yaml --wait --timeout 10m
```

### 4. Chat with the agent

```bash
cd "$HOME/mindroom" && python3 cluster/k8s/kind/chat_check.py
```

Expected output: `Reply from @mindroom_helper: pong` (wording may vary; exit status `0` is the check).
[`chat_check.py`](https://github.com/mindroom-ai/mindroom/blob/main/cluster/k8s/kind/chat_check.py) port-forwards Tuwunel, registers a throwaway test user from the `matrix-registration` Secret, joins `#lobby:mindroom.local`, and waits for `@mindroom_helper`'s reply.

### Uninstall

```bash
kind delete cluster --name mindroom
rm -f "$HOME/mindroom/runtime-values.yaml"
```

### Next steps

For production clusters, continue with [Runtime-Only Deployment](#runtime-only-deployment) (external ConfigMaps, PVCs, and Secrets), [Worker Backends](#worker-backends) (dedicated Kubernetes workers), and [Backup and Restore](#backup-and-restore).
To provision the hosted multi-tenant platform on Hetzner Cloud, see [SaaS Platform](saas-platform.md) and [`cluster/terraform/terraform-k8s/`](https://github.com/mindroom-ai/mindroom/blob/main/cluster/terraform/terraform-k8s/README.md).

## Charts

| Chart | Path | Use |
|-------|------|-----|
| Runtime | `cluster/k8s/runtime/` | MindRoom runtime only, for clusters that provide Matrix, storage, secrets, ingress, and platform services |
| Tuwunel | `cluster/k8s/tuwunel/` | Standalone Tuwunel homeserver (MindRoom fork) to pair with the runtime chart |
| Client | `cluster/k8s/client/` | Standalone MindRoom web client behind unprivileged nginx, for clusters that already provide a homeserver, ingress, and TLS |
| MatrixRTC | `cluster/k8s/matrixrtc/` | Optional LiveKit SFU and MatrixRTC authorization service for [voice calls](../voice-calls.md), proxied by the client chart |
| Instance | `cluster/k8s/instance/` | One hosted MindRoom runtime with bundled dashboard/API and a Tuwunel Matrix homeserver, deployed by the SaaS platform |
| Platform | `cluster/k8s/platform/` | SaaS control plane (API, frontend, provisioner) |

## Prerequisites

These apply to production clusters; for a local try, use the [quick start](#quick-start-local-kind-cluster).

- A Kubernetes cluster (tested with k3s via kube-hetzner)
- `kubectl` and `helm`
- NGINX Ingress Controller
- cert-manager for TLS certificates
- Hetzner Cloud Controller Manager and Hetzner CSI Driver when using `hcloud-volumes`

For Hetzner sizing, see [Hetzner Scaling Baseline](saas-platform.md#hetzner-scaling-baseline).

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
The hosted instance chart instead mounts provider keys as files under `/etc/secrets/` and sets the matching [`*_API_KEY_FILE`](../configuration/models.md#file-based-secrets) variables.

For passwordless agent accounts ([`appservice` mode](../matrix.md#agent-users)), set `matrix.managedAccountAuth: appservice` and point `matrix.appserviceToken` at a Secret holding the registration's `as_token`, and leave `matrix.registrationToken` unset (see [Matrix Managed Account Authentication](https://github.com/mindroom-ai/mindroom/blob/main/cluster/k8s/runtime/README.md#matrix-managed-account-authentication)).
On the Tuwunel chart, mount the complete registration YAML from a Secret through `tuwunel.appserviceRegistration`, whose `key` is also the mounted filename and must end in `.yaml` or `.yml`, and optionally disable password login with `login_with_password: false` in `tuwunel.settings` (see [Passwordless MindRoom Accounts](https://github.com/mindroom-ai/mindroom/blob/main/cluster/k8s/tuwunel/README.md#passwordless-mindroom-accounts)).

### Chart Reference

Each chart's README documents its full values: [runtime](https://github.com/mindroom-ai/mindroom/blob/main/cluster/k8s/runtime/README.md), [Tuwunel](https://github.com/mindroom-ai/mindroom/blob/main/cluster/k8s/tuwunel/README.md), [client](https://github.com/mindroom-ai/mindroom/blob/main/cluster/k8s/client/README.md), and [MatrixRTC](https://github.com/mindroom-ai/mindroom/blob/main/cluster/k8s/matrixrtc/README.md).
Optional features:

- [Background script gateway](https://github.com/mindroom-ai/mindroom/blob/main/cluster/k8s/runtime/README.md#background-script-gateway) (`scriptGateway`) lets dedicated workers run [background scripts](../tools/background-scripts.md) and [minimal-mode](../tools/agent-cli.md) shells without reaching the general API port.
- [Content bundles](https://github.com/mindroom-ai/mindroom/blob/main/cluster/k8s/runtime/README.md#content-bundles) (`contentBundles`, `config.bootstrapContentBundle`) ship config, plugins, and skills as digest-pinned images, including how to update a bootstrapped config.
- [Session and knowledge storage](https://github.com/mindroom-ai/mindroom/blob/main/cluster/k8s/runtime/README.md#session-and-knowledge-storage) (`sessionStorage`, `knowledgeStorage`) and [runtime state storage](https://github.com/mindroom-ai/mindroom/blob/main/cluster/k8s/runtime/README.md#runtime-state-storage) (`stateStorage`, `stateStorage.extraSubPaths`) move data to volumes of their own; on an existing install, copy the data first.
- [Layering values files](https://github.com/mindroom-ai/mindroom/blob/main/cluster/k8s/runtime/README.md#layering-values-files) explains the map form of `env.extra`, `env.envFrom`, `extraVolumes`, and `extraVolumeMounts`, which Helm merges key by key across values files, and lists which env values are rendered with `tpl`.
- [Approved Egress](approved-egress.md) gives workers human-approved temporary hostname grants.
- [Egress broker](https://github.com/mindroom-ai/mindroom/blob/main/cluster/k8s/runtime/README.md#egress-broker) (`egressBroker`) injects stored credentials into workers' outbound HTTPS requests, so workers never hold the secret.
- Tuwunel [structured settings](https://github.com/mindroom-ai/mindroom/blob/main/cluster/k8s/tuwunel/README.md#structured-settings) (`tuwunel.settings`) merge across values files, and [upgrades and database migrations](https://github.com/mindroom-ai/mindroom/blob/main/cluster/k8s/tuwunel/README.md#upgrades-and-database-migrations) explains how to upgrade the homeserver without corrupting its database.
- The client chart's [`matrixRTC` values](https://github.com/mindroom-ai/mindroom/blob/main/cluster/k8s/client/README.md#matrixrtc-calls) announce and proxy the MatrixRTC chart's call backend.
- [Operational log events](operational-log-events.md) feed log-based metrics and alerts once `MINDROOM_LOG_FORMAT=json` is set through `env.extra`.

## Worker Backends

Worker-routed tools such as `docker`, `filesystem`, `python`, and `shell` run in one of two backends.
Which tools are routed and how `worker_scope` shares workers are described in [Sandbox Proxy Isolation](sandbox-proxy.md).

| Helm value | Behavior | Best for |
|------------|----------|----------|
| `workers.backend: static_runner` (default) | One shared sandbox-runner sidecar inside the MindRoom pod | Simpler deployments |
| `workers.backend: kubernetes` | Dedicated worker Deployments and Services created on demand | Isolation between agents or users |

The hosted instance chart supports only `static_runner` (`workerBackend`) and fails rendering for any other backend, because its tenants share one namespace.
Deploy the runtime chart in a namespace of its own when an instance needs dedicated workers.

### Shared Sidecar Mode

The primary runtime reaches the sidecar over `localhost`, and all proxied tool calls share one runner process.
The sidecar shares agent workspaces with the primary but cannot read the credential store, Matrix state, or the credentials encryption key, and it is not an isolation boundary between agents.
For brokered credentials on hosted instances, see [Hosted instances](egress-broker.md#hosted-instances-sandbox-runner-sidecar).
To enable encrypted credential storage, set `workers.sandbox.credentialsEncryptionKey.existingSecret`; only the primary runtime receives that key.
See [Kubernetes shared sidecar](sandbox-proxy.md#kubernetes-shared-sidecar-workerbackend-static_runner) for its mounts and limits.

### Dedicated Worker Mode

With `workers.backend: kubernetes`, the primary runtime creates a worker Deployment and Service per worker on demand and routes tool calls to it.
Idle workers scale to zero, keeping agent data and worker caches.
The default image includes the `kubernetes` package this needs; a custom image built without all extras must install `mindroom[kubernetes]` in the primary runtime.
Each worker keeps its own caches and virtualenvs under `workers.kubernetes.storageSubpathPrefix`.
[Worker scopes](sandbox-proxy.md#worker-scopes) describes which workspaces each worker mounts, and [Credential leases](sandbox-proxy.md#credential-leases) describes how tool settings reach workers.
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
| `workers.kubernetes.image.repository`, `.tag`, `.digest` | `MINDROOM_KUBERNETES_WORKER_IMAGE` | Chart: the main MindRoom image (`image.repository`, `image.tag`, `image.digest`); required without Helm | Worker image; `.tag` and `.digest` take effect only with `.repository` and default to the main image's tag and digest |
| `workers.kubernetes.image.pullPolicy` | `MINDROOM_KUBERNETES_WORKER_IMAGE_PULL_POLICY` | Chart: `image.pullPolicy` (`Always`); otherwise `IfNotPresent` | Worker image pull policy; only `Always` pulls a mutable tag such as `:latest` again when a worker pod starts |
| `workers.kubernetes.namespace` | `MINDROOM_KUBERNETES_WORKER_NAMESPACE` | Chart: the release namespace; otherwise the pod namespace or `default` | Namespace for worker resources |
| _(storage claim)_ | `MINDROOM_KUBERNETES_WORKER_STORAGE_PVC_NAME` | Chart: the runtime storage claim; required without Helm | PVC holding agent workspaces |
| `storage.mountPath` | `MINDROOM_KUBERNETES_WORKER_STORAGE_MOUNT_PATH` | Chart: `/app/agent_data`; otherwise `/app/worker` | Where workers mount storage |
| `workers.kubernetes.storageSubpathPrefix` | `MINDROOM_KUBERNETES_WORKER_STORAGE_SUBPATH_PREFIX` | `workers` | Storage directory for worker-local caches |
| `workers.kubernetes.port` | `MINDROOM_KUBERNETES_WORKER_PORT` | `8766` | Internal Service and container port |
| `workers.kubernetes.readyTimeoutSeconds` | `MINDROOM_KUBERNETES_WORKER_READY_TIMEOUT_SECONDS` | `60` | How long the primary waits for a worker to become ready |
| _(`env.extra`)_ | `MINDROOM_KUBERNETES_WORKER_SETUP_TIMEOUT_SECONDS` | `180` | How long a call then waits for a worker that just started to finish preparing itself, before it runs anyway |
| `workers.kubernetes.idleTimeoutSeconds` | `MINDROOM_KUBERNETES_WORKER_IDLE_TIMEOUT_SECONDS` | `1800` | Idle time before a worker may scale to zero |
| `workers.cleanupIntervalSeconds` | `MINDROOM_WORKER_CLEANUP_INTERVAL_SECONDS` | Chart: `30`; otherwise `0` (no automatic cleanup) | How often idle-worker cleanup runs |
| `workers.kubernetes.reconcilePodTemplates` | `MINDROOM_KUBERNETES_WORKER_RECONCILE_POD_TEMPLATES` | `true` | Recreate workers whose image, env, or resources changed |
| `workers.kubernetes.namePrefix` | `MINDROOM_KUBERNETES_WORKER_NAME_PREFIX` | `mindroom-worker` | Prefix for worker resource names |
| `workers.kubernetes.runtimeClassName` | `MINDROOM_KUBERNETES_WORKER_RUNTIME_CLASS_NAME` | Cluster default | RuntimeClass for all dedicated workers, including background-script workers |
| `workers.kubernetes.resources` | `MINDROOM_KUBERNETES_WORKER_CPU_REQUEST`, `_CPU_LIMIT`, `_MEMORY_REQUEST`, `_MEMORY_LIMIT` | `100m`, `500m`, `256Mi`, `1Gi` | Worker container resources |
| `workers.kubernetes.scriptResources` | `MINDROOM_KUBERNETES_DEFAULT_SCRIPT_RESOURCE_PROFILE`, `MINDROOM_KUBERNETES_SCRIPT_RESOURCE_PROFILES_JSON` | `small`; profiles in `values.yaml` | The `small`, `standard`, and `large` [background script](../tools/background-scripts.md) resource profiles, all three required; a [dedicated subagent worker](../tools/agent-orchestration.md#dedicated-workers) or a [named worker](../tools/execution-and-coding.md#named-workers) that names a profile uses it too |
| `workers.kubernetes.nodeName`, `colocateWithControlPlaneNode` | `MINDROOM_KUBERNETES_WORKER_NODE_NAME`, `MINDROOM_KUBERNETES_WORKER_COLOCATE_WITH_CONTROL_PLANE_NODE` | Unset, `false` | Pin workers to one node (see [Storage Requirements](#storage-requirements)) |
| `workers.kubernetes.extraEnv` | `MINDROOM_KUBERNETES_WORKER_ENV_JSON` | `{}` | Extra environment variables for worker pods |
| _(`env.extra`)_ | `MINDROOM_KUBERNETES_WORKER_TMP_SIZE_LIMIT` | `1Gi` | Size of each worker's `/tmp` |
| _(`env.extra`)_ | `MINDROOM_KUBERNETES_WORKER_USER_RESOURCES_JSON` | `{}` | Per-requester worker resources |
| _(`env.extra`)_ | `MINDROOM_KUBERNETES_NAMED_WORKER_IDLE_TIMEOUT_SECONDS` | `600` | Time without a running command after which a [named worker](../tools/execution-and-coding.md#named-workers) is removed, at most the worker idle timeout |
| _(`env.extra`)_ | `MINDROOM_KUBERNETES_NAMED_WORKER_MAX_AGE_SECONDS` | `14400` | Time after its start at which a named worker is removed, whatever runs in it |
| _(`env.extra`)_ | `MINDROOM_KUBERNETES_NAMED_WORKERS_MAX` | `16` | Most named workers that may run at once for all agents and requesters together; `0` disables named workers, and negative values are invalid |

Changing the image, environment, resources, or RuntimeClass recreates scaled-down workers on the next cleanup pass and running workers on their next use, so no manual recycling is needed; finish active work before changing them.
Before selecting a RuntimeClass, verify that its handler is available on every eligible worker node and supports the configured storage driver, access mode, and mount behavior.

A worker that just started is ready as soon as it answers, and then prepares its Python environment, shell, and tool runtime in the background.
The call that started it shows `worker <name>: preparing (<n>s)` while it waits for that, for at most the setup timeout; a preparation that fails or takes longer never fails the worker, and the call then prepares what is missing itself.
A command that streams its output, as `workers run` does, and a viewer that only shows the worker do not wait for that: the worker launches such a command itself, without the tool runtime.
The tool runtime imports the plugins that set [`worker_preload`](../plugins.md#importing-once-per-worker) once for all the worker's calls.

Each worker's `/tmp` is disk-backed, and exceeding its size limit evicts that worker pod rather than refusing writes.
`MINDROOM_KUBERNETES_WORKER_TMP_SIZE_LIMIT` (for example `4Gi`) also requests and limits the same amount of container `ephemeral-storage`, which clusters such as GKE Autopilot otherwise cap at 1 GiB.
Tool subprocesses already get a `TMPDIR` on persistent storage, so only explicit `/tmp` paths use this space.

Each Kubernetes worker's `sandbox-runner` container (not sidecars or the pod as a whole; Docker workers do not get these) receives its own limits from the Kubernetes downward API as `MINDROOM_KUBERNETES_POD_CPU_LIMIT` (CPU limit rounded up to whole CPUs, or node allocatable when no limit is set) and `MINDROOM_KUBERNETES_POD_MEMORY_LIMIT_BYTES` (memory limit in bytes, or node allocatable when unset).
From those container markers, tool subprocesses, background scripts, plugin services, and `.mindroom/worker-env.sh` hooks receive `MINDROOM_WORKER_CPUS`, `MINDROOM_WORKER_MEMORY_BYTES`, `OMP_NUM_THREADS`, `PYTHON_CPU_COUNT`, and `PYTEST_XDIST_AUTO_NUM_WORKERS`, so GNU coreutils `nproc` (the worker image; BusyBox `nproc` ignores `OMP_NUM_THREADS`), OpenMP/BLAS, Python 3.13+ `os.cpu_count()`/`os.process_cpu_count()`, and `pytest -n auto` match the worker container instead of the node while keeping the runner process's own thread-pool budget unchanged.
Because `free` cannot be constrained through environment variables and still reports node memory, tools that need the container memory limit should read `MINDROOM_WORKER_MEMORY_BYTES` or cgroup v2 `/sys/fs/cgroup/memory.max`.
Precedence from lowest to highest is: primary/manifest/`extra_env_passthrough` values < derived pod values < operator `workers.kubernetes.extraEnv` (`MINDROOM_KUBERNETES_WORKER_ENV_JSON`; setting `PYTHON_CPU_COUNT` or `OMP_NUM_THREADS` there also affects the runner process itself) < `.mindroom/worker-env.sh` exports < explicit per-call shell environment (`tool:environment` set or unset).
The hook step applies only where the hook is sourced (shell commands, background scripts, and the Python tool); plugin services get the derived and operator values without it. A hook overrides by exporting a value: its `unset` does not remove an inherited one, as for any other variable, while a per-call unset does.
A `.mindroom/worker-env.sh` hook that sets only `MINDROOM_WORKER_CPUS` does not re-derive `OMP_NUM_THREADS`, `PYTHON_CPU_COUNT`, or `PYTEST_XDIST_AUTO_NUM_WORKERS`.
In `forkserver` mode (production Kubernetes workers), `run_python_code` runs in a Python process started before `.mindroom/worker-env.sh`, so a hook's `PYTHON_CPU_COUNT` updates `os.environ` there without changing `os.cpu_count()`; in `subprocess` mode (and in Python processes started from shell commands), a fresh interpreter starts after the hook and honors its `PYTHON_CPU_COUNT`.

`MINDROOM_KUBERNETES_WORKER_USER_RESOURCES_JSON` maps a Matrix user ID to `requests` and/or `limits` with `cpu` and/or `memory`, for example `{"@alice:example.org": {"limits": {"memory": "4Gi"}}}`.
Listed quantities replace the global values for that requester's `user` and `user_agent` workers; shared and unscoped workers, other requesters, and background-script profiles are unchanged.

`workers.kubernetes.extraEnv` drops MindRoom's own control variables and vendor telemetry variables, except `MINDROOM_SANDBOX_RUNNER_SUBPROCESS_TIMEOUT_SECONDS`, which tunes runner subprocess timeouts.
Workers do not inherit the runtime's `.env` or provider keys; for per-workspace variables such as `PATH` entries or package indexes, use the [workspace env hook](sandbox-proxy.md#workspace-env-hook-mindroomworker-envsh), which takes effect without redeploying.

### Brokered Egress

Set `egressBroker.enabled: true` in the runtime chart to let workers call authenticated HTTP APIs without holding the credential.
The runtime chart's [Egress Broker](https://github.com/mindroom-ai/mindroom/blob/main/cluster/k8s/runtime/README.md#egress-broker) section has its values and modes, and its [Self-service for users](https://github.com/mindroom-ai/mindroom/blob/main/cluster/k8s/runtime/README.md#self-service-for-users) section explains opening the page from MindRoom Chat and lists the [trusted upstream auth](trusted-upstream-auth.md) and gateway settings for signing users in through a gateway instead.
[Agent Keys](egress-broker.md) covers how the broker works and the users' Connections pages.

### Knowledge Source Visibility

Dedicated workers can read the knowledge-base source directories assigned to the agents they serve, mounted read-only at the same path relative to the worker storage mount.
A `user` worker sees the knowledge of every `worker_scope: user` agent; other workers see only their own agent's.

| Deployment | Worker storage mount | Visible path for `knowledge/reference` |
| --- | --- | --- |
| Runtime chart (`storage.mountPath`) | `/app/agent_data` | `/app/agent_data/knowledge/reference` |
| Direct backend without a mount override | `/app/worker` | `/app/worker/knowledge/reference` |

The worker sees the complete source directory, including files excluded from indexing, and MindRoom does not copy or clone it per agent.
Only sources inside the shared storage root are mounted.
A source inside a workspace or shared directory the worker already mounts stays reachable through that mount.
Sources that are missing, reached through a link, or inside another agent's workspace, a private instance, a shared directory the worker does not mount, or a worker directory are skipped with a logged warning or error.
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

With `egressBroker.enabled`, it also creates the `<fullname>-egress-broker` Service and, when `networkPolicy.create` is true, a NetworkPolicy that admits workers (or, with approved egress, the proxy pods) to the broker port on the primary.

That Role reaches every Deployment and Service in its namespace, so give each runtime release a namespace of its own.
Label that namespace with `pod-security.kubernetes.io/enforce=baseline` so admission rejects privileged containers, host namespaces, and `hostPath` volumes; the chart's runtime and worker pods satisfy that profile.

### Operations

To list active and idle workers or run an idle-worker cleanup pass, use the dashboard's [Workers API](../dashboard.md#workers-api).

## Backup and Restore

This section covers the runtime chart and the Tuwunel chart.
Several volumes and Secrets only work together, so back them up and restore them as units.

### What each volume holds

| Volume | Values | Contents |
|--------|--------|----------|
| `<fullname>-storage` | `storage` | The storage root (`MINDROOM_STORAGE_PATH`, default `/app/agent_data`), holding everything in [Data Persistence](storage.md#data-persistence) except what an enabled volume below holds instead, such as `encryption_keys/` and `sync_continuity/` with `stateStorage` |
| `<fullname>-state` | `stateStorage` | When enabled, `encryption_keys/` (each agent's Matrix device store with its E2EE keys and sync position) and `sync_continuity/`, plus each `stateStorage.extraSubPaths` directory, such as `tracking/` |
| `<fullname>-sessions` | `sessionStorage` | When enabled, the agent and team session databases (`MINDROOM_SESSION_STORAGE_PATH`) |
| `<fullname>-knowledge` | `knowledgeStorage` | When enabled, `knowledge_db/` |
| `<volumeName>-<fullname>-event-cache-postgres-0`, default `data-<fullname>-event-cache-postgres-0` | `eventCache.postgres.persistence` | The PostgreSQL event journal, which records which messages were already answered |
| `<approved egress name>-data`, default `<fullname>-egress-proxy-data` | `approvedEgress.persistence` | Temporary hostname grants of the approved egress proxy |
| Tuwunel `<fullname>-data` | Tuwunel chart `storage` | The homeserver's RocksDB database and its `media/` directory |

Back up the sessions volume together with the storage claim.

### Restore units

Restore each of these sets together, from the same point in time:

- **Credentials encryption key and credential stores.**
  With `MINDROOM_CREDENTIALS_ENCRYPTION_KEY` set (for example through `workers.sandbox.credentialsEncryptionKey.existingSecret`), credential files and OAuth stores are encrypted under that key.
  Under a different key MindRoom logs `Failed to load encrypted credentials` and will not overwrite them, so back up the key's Secret value with the storage claim.
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
The approved egress and knowledge volumes can be snapshotted on their own schedules.
For the Tuwunel volume, use an offline snapshot or Tuwunel's own database backups, which do not include media; see the fork's [backup guide](https://github.com/mindroom-ai/mindroom-tuwunel/blob/main/docs/backups.md).

### Rebuildable data

- `knowledge_db/` holds only knowledge indexes.
  A missing index is rebuilt in the background the next time the base is used, repeating every embedding call, and agents report the base as initializing meanwhile.
  The sources, including files uploaded through the dashboard, are not rebuildable unless they come from Git, in which case they are cloned again from the configured repository.
- `logs/` only holds runtime log files.
- `egress_broker/` holds the broker's CA, token signing key, and request log; deleting it loses the log, and the broker generates the rest again (see [Resetting broker state](egress-broker.md#resetting-broker-state)).
- The approved egress volume only holds temporary grants, which expire after at most `approvedEgress.maxTtlSeconds`.

Everything else on the storage, sessions, state, and journal volumes is not rebuildable.

### Restoring

1. Restore the volumes before installing the charts, then point the charts at them with `storage.existingClaim`, `stateStorage.existingClaim`, `sessionStorage.existingClaim`, `knowledgeStorage.existingClaim`, `approvedEgress.persistence.existingClaim`, and the Tuwunel chart's `storage.existingClaim`.
   The chart-managed PostgreSQL adopts a claim restored under `<volumeName>-<fullname>-event-cache-postgres-0`; set its original password as described in [Restore units](#restore-units).
   With an external database, restore the database and keep its URL Secret.
   To restore a `pg_dump` instead, install with a new journal volume and `replicaCount: 0`, load the dump, then set `replicaCount` back to 1.
2. Recreate the Secrets with their original values, including the credentials encryption key and the Matrix registration token or application-service registration.
3. Start Tuwunel first, then the runtime.

If the runtime log shows `The bound Matrix device store is missing`, `Matrix stream changed`, or `IngestionBatchSequenceError`, the device stores do not match the journal; an event journal binding error means `tracking/` and the journal database do not match.
Restore the matching backups instead of deleting state, because automatic device replacement is unsupported.
See [Moving a journal safely](storage.md#moving-a-journal-safely) for copying or adopting a journal database, and [Device recovery after the upgrade](upgrades.md#device-recovery-after-the-upgrade) for device-store recovery.

### Checking a restore

Before letting users back in, check that:

1. Files on the restored volumes keep the owners and modes of the backup, by default uid and gid 1000 for the runtime's files.
2. Every database under `tracking/` and a sample of session databases pass `PRAGMA quick_check`, with no `-journal` file beside them (see [Inspecting copies](#inspecting-copies)).
3. Tuwunel runs the image it ran when the backup was taken, so its first start runs no database migration, and answers `/_matrix/client/versions`.
4. The runtime becomes ready and logs none of the errors named under [Restore units](#restore-units) and [Restoring](#restoring).
5. A message to an agent in a test room gets exactly one reply, and older messages are not answered again.
6. A tool that uses a connected account works, and `!list_schedules` shows the expected schedules.
7. Your backup schedule covers the restored volumes.

### Inspecting copies

Run bulk reads, such as scans across session databases, on a restored copy instead of the live volumes.
Session databases use SQLite's rollback journal, so a long read of a live one blocks the agent's writes and can make them fail.

Copy each database together with any `-journal`, `-wal`, or `-shm` file beside it.
A `-journal` file left beside a database means a write was cut off: SQLite undoes it the next time the database is opened read-write, and refuses to read it read-only until then.
Check a copy with Python's `sqlite3` module, which the runtime image includes; it prints `ok` for an intact database:

```bash
python3 -c 'import sqlite3, sys; print(sqlite3.connect(sys.argv[1]).execute("PRAGMA quick_check").fetchone()[0])' /copy/path/to/database.db
```

On a read-only mount, open databases as `file:<path>?immutable=1`, which reads without locks or side files, and only when no `-journal` or `-wal` file sits beside them.

One-off pods that mount the storage volume, for example to copy or inspect it, should run as uid and gid 1000, the runtime image's user, so files they create stay writable for MindRoom.
Give them `fsGroupChangePolicy: OnRootMismatch`, as the chart's own pods have, or no `fsGroup`.
Otherwise, on volume types that support it, the kubelet changes the group of every file on the volume before the pod starts and gives that group read and write access, including to private files, which on a large volume also delays the start for a long time.

### Starting a copy beside the original

A restored copy holds the original's Matrix accounts, OAuth tokens, and schedules, so a copy with network access acts as the original:

- With the original's homeserver in reach, it answers messages and runs due schedules in real rooms.
- With a provider that rotates refresh tokens, the copy's first token refresh invalidates the token the original holds, and the user has to connect that account again.
- Tools with outside effects, such as sending email, run for real.

Start a copy while the original runs only in isolation:

- Its own namespaces for the runtime and its workers, each with a default-deny egress NetworkPolicy and rules that allow only cluster DNS and traffic within the copy's namespaces, on a cluster that enforces NetworkPolicy.
  Before starting MindRoom, check from a test pod in each namespace that outside connections fail.
- No cloud identity: no workload identity annotations on its service accounts and no access to the node's metadata server.
- Dummy values for provider API keys and other Secrets, except the keys the copied data needs, such as the credentials encryption key from [Restore units](#restore-units).
- With `appservice` accounts, new tokens in the copied homeserver's registration and the runtime, so the copy cannot sign in to the original homeserver as the application service.
- No Ingress, LoadBalancer Service, CronJobs, or tunnel connector; a connector started with the original's tunnel credentials joins that tunnel and receives users' traffic.

<a id="database-migrations"></a>See [SaaS Platform](saas-platform.md#secrets-management) and [Database Migrations](saas-platform.md#database-migrations).
