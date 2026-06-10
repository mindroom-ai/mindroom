---
icon: lucide/shield
---

# Sandbox Proxy Isolation

When agents have code-execution tools (`shell`, `file`, `python`), they can read and modify anything on the filesystem, including config files, credentials, and application code.
The **sandbox proxy** isolates these tools by forwarding their calls to a separate worker runtime that has no direct access to the primary process secrets.
This page describes the current sandboxed execution model.

## How it works

```
┌──────────────────────────┐         HTTP          ┌──────────────────────────┐
│ Primary MindRoom runtime │  ── tool call ──▶     │ Worker runtime           │
│ has secrets              │  ◀── result ───       │ no primary secrets       │
│ has credentials          │                       │ leased credentials only  │
│ has orchestration state  │                       │ agent state + caches     │
└──────────────────────────┘                       └──────────────────────────┘
```

1. Agent invokes `shell.run_shell_command(...)` or another worker-routed tool.
2. The primary MindRoom runtime resolves the target worker from the configured backend plus worker scope.
3. The call is forwarded over HTTP to the target worker runtime.
4. The worker executes the tool against the agent's storage directory plus any worker-local caches and returns the result.
5. All other tools such as API tools or Matrix-bound tools execute in the primary MindRoom runtime as usual.

The static worker runtime authenticates requests with `MINDROOM_SANDBOX_PROXY_TOKEN`.
Kubernetes dedicated workers derive a separate runner token for each worker from that control-plane token and the worker key.
Compromising one dedicated worker token does not authorize requests to another dedicated worker runner.
For tools that need credentials, such as a shell tool that calls an authenticated API, the primary MindRoom runtime can create a short-lived **credential lease** that the worker consumes once.
Credentials never become part of the normal tool arguments or the model prompt.

MindRoom currently ships three worker backend shapes:

- `static_runner`: one shared sandbox-runner process, usually a sidecar container or a local HTTP service.
- `docker`: dedicated worker containers created on demand from the primary runtime, with one logical worker per worker key.
- `kubernetes`: dedicated worker pods created on demand from the primary runtime, with one logical worker per worker key.

## Where Agent Data Lives

Each agent stores all its persistent data (context files, workspace files, memory, sessions, learning) in one directory: `agents/<name>/`.
This directory is shared across all worker scopes — switching `worker_scope` changes how tool runtimes are isolated, not where agent data lives.
Worker runtimes may keep their own virtualenvs, caches, and scratch files, but those are not agent data.
Multiple runtimes may access the same agent directory concurrently, so files and databases there must tolerate concurrent access.

## Deployment modes

### Docker Compose (`static_runner`)

Add a `sandbox-runner` service alongside MindRoom.
Both use the same image.
The runner just has a different entrypoint and no access to `.env` or the primary data volume.

```yaml
services:
  mindroom:
    image: ghcr.io/mindroom-ai/mindroom:latest
    env_file: .env
    volumes:
      - ./config.yaml:/app/config.yaml:ro
      - ./mindroom_data:/app/mindroom_data
    environment:
      - MINDROOM_WORKER_BACKEND=static_runner
      - MINDROOM_SANDBOX_PROXY_URL=http://sandbox-runner:8766
      - MINDROOM_SANDBOX_PROXY_TOKEN=${MINDROOM_SANDBOX_PROXY_TOKEN}
      - MINDROOM_SANDBOX_EXECUTION_MODE=selective
      - MINDROOM_SANDBOX_PROXY_TOOLS=shell,file,python

  sandbox-runner:
    image: ghcr.io/mindroom-ai/mindroom:latest
    command: ["/app/run-sandbox-runner.sh"]
    user: "1000:1000"
    volumes:
      - sandbox-workspace:/app/workspace
    environment:
      - MINDROOM_SANDBOX_RUNNER_MODE=true
      - MINDROOM_SANDBOX_PROXY_TOKEN=${MINDROOM_SANDBOX_PROXY_TOKEN}
      - MINDROOM_CONFIG_PATH=/app/config.yaml
      - MINDROOM_STORAGE_PATH=/app/workspace/.mindroom

volumes:
  sandbox-workspace:
```

Do not mount the full `mindroom_data` tree into the runner because it contains credentials, Matrix encryption keys, sessions, and logs.

> [!IMPORTANT]
> The `sandbox-workspace` Docker volume is created as root by default.
> The runner runs as UID 1000, so you must fix ownership after first creating the volume:
> ```bash
> docker run --rm -v sandbox-workspace:/workspace busybox chown -R 1000:1000 /workspace
> ```
> Alternatively, omit the `user:` directive to run as root (less secure).

Key differences from the primary MindRoom runtime:
- **No `env_file`** — runner has no API keys, no Matrix credentials
- **Scratch workspace** — a dedicated volume for worker-local files (caches, virtualenvs)
- **`MINDROOM_STORAGE_PATH`** — pointed at a writable location inside the scratch workspace for tool registry and cache files

> [!WARNING]
> **Filesystem isolation depends on the worker backend.**
> Static shared-runner deployments should not mount the primary MindRoom storage tree into the runner.
> Local in-process execution still shares the primary process filesystem.
> Kubernetes dedicated workers restrict mounts so each runtime only sees its own agent's directory (for `shared`, `user_agent`, and unscoped modes).
> The `user` scope is intentionally broader: it shares one runtime across multiple agents per user, so agents in that runtime can see each other's files.
> Use `user_agent` for per-agent filesystem isolation.

### Kubernetes shared sidecar (`workerBackend: static_runner`)

In Kubernetes the shared runner can still run as a second container in the same pod, sharing `localhost` networking.
This is the `workerBackend: static_runner` Helm mode.
See `cluster/k8s/instance/templates/deployment-mindroom.yaml` for the full manifest.
The sidecar gets:

- An `emptyDir` volume for worker-local scratch files and caches.
- Access to the same shared storage that holds agent data directories.
- Read-only access to config for plugin tool registration.
- No access to the primary secrets volume.

### Kubernetes dedicated workers (`workerBackend: kubernetes`)

In dedicated-worker mode the primary MindRoom runtime creates worker Deployments and Services on demand.
Each worker pod runs the sandbox-runner app and is addressed through an internal cluster Service.
Each dedicated worker needs access to its agent's storage directory.
Worker-local files (caches, virtualenvs, metadata) are kept separate per worker.
When a worker is idle, its Deployment scales to zero, but agent data and worker caches are preserved.
The runtime chart stores derived worker tokens and optional credential-encryption keys as per-worker entries in one chart-created worker-auth Secret when workers run in the release namespace.
If `workers.kubernetes.namespace` is set to a separate worker namespace, the runtime chart can instead manage per-worker auth Secrets in that namespace.
The hosted instance chart stores derived worker tokens and optional credential-encryption keys as per-worker entries in a pre-created tenant auth Secret.
The hosted instance worker-manager Role does not grant broad Secret API access in the shared `mindroom-instances` namespace.

Use the instance Helm chart with values like:

```yaml
workerBackend: kubernetes
workerCleanupIntervalSeconds: 30
storageAccessMode: ReadWriteMany
kubernetesWorkerPort: 8766
kubernetesWorkerReadyTimeoutSeconds: 60
kubernetesWorkerIdleTimeoutSeconds: 1800
sandbox_proxy_token: "replace-me"
```

Important notes for this mode:

- `storageAccessMode` should be `ReadWriteMany` because multiple dedicated workers may need concurrent access to the same agent storage.
- If you must keep `ReadWriteOnce`, set `controlPlaneNodeName` so the control plane and dedicated workers stay on the same node.
- `kubernetesWorkerImage` and `kubernetesWorkerImagePullPolicy` default to the main MindRoom image settings when left empty.
- The chart creates the worker-manager ServiceAccount, Role, RoleBinding, and worker-specific NetworkPolicy rules automatically when this backend is enabled.

  The runtime and hosted instance charts grant narrow access to one worker-auth Secret in shared runtime namespaces, while explicitly separate runtime worker namespaces may use per-worker auth Secret CRUD.
- The primary runtime does not need `MINDROOM_SANDBOX_PROXY_URL` in this mode because worker endpoints come from the Kubernetes worker handles.
- Dynamic worker pods default to `enableServiceLinks: false` so Kubernetes does not inject sibling Service names into the runner environment.
- Runner ingress defaults to allowing the MindRoom control-plane pod to reach worker runner ports, while worker-to-worker ingress is denied by NetworkPolicy.
- The authenticated `/api/workers` and `/api/workers/cleanup` endpoints on the primary runtime expose backend-neutral worker lifecycle information.

Untrusted code-execution tools may still share the runner container's process namespace and may be able to inspect the runner process environment through `/proc` on some container runtimes.
For dedicated Kubernetes workers, the exposed environment contains only that worker's derived runner token, not the shared control-plane token.
This leaves same-worker token exposure as a local containment risk, while per-worker credentials and NetworkPolicy limit cross-worker blast radius.

For the full Helm-side deployment guidance, see [Kubernetes Deployment](kubernetes.md).

### Host machine + Docker sandbox container

Run MindRoom directly on the host while isolating code-execution tools in a Docker container:

```bash
# 1. Start the sandbox runner container
docker run -d \
  --name mindroom-sandbox-runner \
  -p 8766:8766 \
  -e MINDROOM_WORKER_BACKEND=static_runner \
  -e MINDROOM_SANDBOX_RUNNER_MODE=true \
  -e MINDROOM_SANDBOX_PROXY_TOKEN=your-secret-token \
  -e MINDROOM_STORAGE_PATH=/app/workspace/.mindroom \
  ghcr.io/mindroom-ai/mindroom:latest \
  /app/run-sandbox-runner.sh

# 2. Start MindRoom on the host with proxy config
export MINDROOM_WORKER_BACKEND=static_runner
export MINDROOM_SANDBOX_PROXY_URL=http://localhost:8766
export MINDROOM_SANDBOX_PROXY_TOKEN=your-secret-token
export MINDROOM_SANDBOX_EXECUTION_MODE=selective
export MINDROOM_SANDBOX_PROXY_TOOLS=shell,file,python
mindroom run
```

Or add the proxy variables to your `.env` file:

```bash
MINDROOM_WORKER_BACKEND=static_runner
MINDROOM_SANDBOX_PROXY_URL=http://localhost:8766
MINDROOM_SANDBOX_PROXY_TOKEN=your-secret-token
MINDROOM_SANDBOX_EXECUTION_MODE=selective
MINDROOM_SANDBOX_PROXY_TOOLS=shell,file,python
```

This gives you the convenience of running MindRoom natively while keeping code-execution tools inside a container boundary.

> [!TIP]
> If you use plugin tools that also need proxying, mount your `config.yaml` into the runner container so it can register them:
> ```bash
> docker run -d \
>   --name mindroom-sandbox-runner \
>   -p 8766:8766 \
>   -v ./config.yaml:/app/config.yaml:ro \
>   -e MINDROOM_CONFIG_PATH=/app/config.yaml \
>   -e MINDROOM_SANDBOX_RUNNER_MODE=true \
>   -e MINDROOM_SANDBOX_PROXY_TOKEN=your-secret-token \
>   -e MINDROOM_STORAGE_PATH=/app/workspace/.mindroom \
>   ghcr.io/mindroom-ai/mindroom:latest \
>   /app/run-sandbox-runner.sh
> ```

### Host machine + dedicated Docker workers (`MINDROOM_WORKER_BACKEND=docker`)

Use this when you want the primary MindRoom runtime on the host, but you want worker-routed tools to execute in dedicated Docker workers.
That most commonly means `shell`, `file`, and `python`, but other worker-safe tools can also be routed through workers when they only need worker state or config-referenced filesystem assets.
The Docker backend starts one worker container per worker key and reuses it until the container goes idle or the Docker launch configuration changes.
This is the simplest way to get one persistent container per agent without running Kubernetes.
MindRoom builds a projected read-only config snapshot for each worker from `MINDROOM_DOCKER_WORKER_HOST_CONFIG_PATH`, rewrites config-relative paths into that snapshot, copies only the referenced config-relative assets needed for that worker into the snapshot, and mounts only the snapshot root into the container.
MindRoom also sanitizes the projected worker `config.yaml`, removing sensitive config keys and authorization headers from the worker-visible snapshot before it is written.
Agent-scoped workers such as unscoped, `worker_scope: shared`, and `worker_scope: user_agent` snapshot only that agent's projected context files and assigned knowledge bases.
`worker_scope: user` intentionally shares one worker across multiple agents, so it keeps the broader shared projection for that worker.
Writable file-memory paths are rewritten into the worker's own state root instead of being mounted from the host config tree.
MindRoom also masks config-adjacent `.env` inside the worker container, so the raw file is not mounted into the worker.
Proxied `shell` and `python` requests still receive their execution env from the active runtime contract, so ordinary `.env` values can remain visible to those tools unless you remove them or override `execution_env`.
If a tool inside the worker still needs a secret that you stored directly in `config.yaml`, provide that secret through a supported worker-visible env or credential path instead of relying on the projected config copy.

MindRoom auto-installs the optional `docker` extra the first time this backend is used.
If you disable auto-install with `MINDROOM_NO_AUTO_INSTALL_TOOLS=1`, install it yourself with `uv sync --extra docker` in a source checkout or `pip install 'mindroom[docker]'`.
If you are testing unreleased code from a source checkout, start MindRoom from that checkout instead of the published PyPI build.
Use `uv run mindroom run` from the repo root, or `uvx --from /path/to/mindroom mindroom run`.
Use plain `uvx mindroom run` only after the version you want is published on PyPI.
When you test unreleased code, build a worker image from the same checkout so the primary runtime and worker containers run the same revision.

```bash
docker build -t mindroom:dev -f local/instances/deploy/Dockerfile.mindroom .
```

Set the backend environment in your shell or `.env`:

```bash
export MINDROOM_WORKER_BACKEND=docker
export MINDROOM_DOCKER_WORKER_IMAGE=mindroom:dev
export MINDROOM_SANDBOX_PROXY_TOKEN=replace-me-with-a-long-random-token

# Optional but useful for local debugging.
export MINDROOM_DOCKER_WORKER_NAME_PREFIX=mindroom-worker
export MINDROOM_DOCKER_WORKER_PUBLISH_HOST=127.0.0.1
export MINDROOM_DOCKER_WORKER_READY_TIMEOUT_SECONDS=60
```

For released versions, you can point `MINDROOM_DOCKER_WORKER_IMAGE` at the matching published image tag instead.

Then route the tools you want into workers and choose a worker scope:

```yaml
defaults:
  worker_tools: [shell, file, python]
  worker_scope: shared

agents:
  code:
    tools: [shell, file, python]

  research:
    tools: [shell, file, python]
```

`worker_scope: shared` is the setting to use when you want one persistent Docker container per agent.
`worker_scope: user_agent` creates one container per requester and agent.
`worker_scope: user` does not give you per-agent isolation, because all agents for one requester share the same worker state.

You can verify the setup by asking two different agents to run `hostname` and then checking Docker:

```bash
docker ps --format '{{.Names}}\t{{.ID}}' | grep '^mindroom-worker'
```

In a live validation, separate `code` and `research` requests produced separate worker containers, and a second `code` request reused the original `code` container.

## Environment variable reference

### Primary MindRoom runtime (proxy client)

| Variable | Description | Default |
|----------|-------------|---------|
| `MINDROOM_WORKER_BACKEND` | Worker backend name: `static_runner`, `docker`, or `kubernetes` | `static_runner` |
| `MINDROOM_SANDBOX_PROXY_URL` | URL of the shared sandbox runner when using `static_runner` | _(none — plain static-runner installs execute locally)_ |
| `MINDROOM_SANDBOX_PROXY_TOKEN` | Static-runner bearer token and Kubernetes control-plane secret used to derive per-worker runner tokens | _(required for worker-routed execution)_ |
| `MINDROOM_SANDBOX_EXECUTION_MODE` | `selective`, `all`, `off` | _(unset — uses static proxy all-tools routing when `MINDROOM_SANDBOX_PROXY_URL` is set; otherwise uses default worker-routed execution tools)_ |
| `MINDROOM_SANDBOX_PROXY_TOOLS` | Comma-separated tool names to proxy when no agent-level `worker_tools` override is active | `*` for all mode or unset static-proxy mode, empty for selective mode or unset no-proxy mode |
| `MINDROOM_UNSAFE_ALLOW_LOCAL_EXECUTION_TOOLS` | Permit local execution of `coding`, `docker`, `file`, `python`, and `shell` when routing was explicitly requested but no worker/proxy backend is available | `false` |
| `MINDROOM_SANDBOX_PROXY_TIMEOUT_SECONDS` | HTTP timeout for proxy calls | `120` |
| `MINDROOM_ATTACHMENT_INLINE_SAVE_MAX_BYTES` | Maximum attachment bytes the primary runtime will inline when saving context attachments into a worker workspace with `get_attachment(..., mindroom_output_path=...)` | `16777216` (16 MiB) |
| `MINDROOM_SANDBOX_CREDENTIAL_LEASE_TTL_SECONDS` | Credential lease lifetime | `60` |
| `MINDROOM_SANDBOX_CREDENTIAL_POLICY_JSON` | JSON mapping tool selectors to credential services | `{}` |

When `MINDROOM_WORKER_BACKEND=docker` or `MINDROOM_WORKER_BACKEND=kubernetes`, the primary runtime resolves worker endpoints dynamically and does not use `MINDROOM_SANDBOX_PROXY_URL`.
The Helm chart sets the Kubernetes backend environment variables automatically.
If you deploy that mode without Helm, see [Kubernetes Deployment](kubernetes.md) and `src/mindroom/workers/backends/kubernetes_config.py` for the required environment surface.

### Dedicated Docker worker backend

| Variable | Description | Default |
|----------|-------------|---------|
| `MINDROOM_DOCKER_WORKER_IMAGE` | Container image used for dedicated Docker workers | _(required when `MINDROOM_WORKER_BACKEND=docker`)_ |
| `MINDROOM_DOCKER_WORKER_PORT` | Sandbox-runner port inside the worker container | `8766` |
| `MINDROOM_DOCKER_WORKER_STORAGE_MOUNT_PATH` | Worker root mount path inside the container | `/app/worker` |
| `MINDROOM_DOCKER_WORKER_CONFIG_PATH` | Config path inside the worker container | `/app/config-host/config.yaml` |
| `MINDROOM_DOCKER_WORKER_HOST_CONFIG_PATH` | Host path to `config.yaml` used to build the projected worker config snapshot; MindRoom mounts only the snapshot root, copies only the config-relative assets needed for that worker into it, masks `.env` inside the container, and removes sensitive config values plus auth headers from the worker-visible `config.yaml` | Resolved `MINDROOM_CONFIG_PATH` when it exists |
| `MINDROOM_DOCKER_WORKER_IDLE_TIMEOUT_SECONDS` | Idle timeout before a worker container is eligible for cleanup | `1800` |
| `MINDROOM_DOCKER_WORKER_READY_TIMEOUT_SECONDS` | Maximum wait for worker `/healthz` after startup | `60` |
| `MINDROOM_DOCKER_WORKER_NAME_PREFIX` | Prefix used for generated worker container names | `mindroom-worker` |
| `MINDROOM_DOCKER_WORKER_PUBLISH_HOST` | Host interface used when publishing worker ports | `127.0.0.1` |
| `MINDROOM_DOCKER_WORKER_ENDPOINT_HOST` | Hostname the primary runtime uses to call published worker ports | Same value as `MINDROOM_DOCKER_WORKER_PUBLISH_HOST` |
| `MINDROOM_DOCKER_WORKER_USER` | Container user for workers, or empty to use the image default | Current host uid:gid on POSIX, image default otherwise |
| `MINDROOM_DOCKER_WORKER_ENV_JSON` | JSON object of extra env vars injected into each worker container | `{}` |
| `MINDROOM_DOCKER_WORKER_LABELS_JSON` | JSON object of extra Docker labels applied to each worker container | `{}` |

### Sandbox runner

| Variable | Description | Default |
|----------|-------------|---------|
| `MINDROOM_SANDBOX_RUNNER_PORT` | Port the sandbox runner listens on | `8766` |
| `MINDROOM_SANDBOX_RUNNER_MODE` | Set to `true` to indicate runner mode | `false` |
| `MINDROOM_SANDBOX_PROXY_TOKEN` | Runner bearer token. Static runners use the shared primary token; Kubernetes dedicated workers receive a per-worker derived token. | _(required)_ |
| `MINDROOM_SANDBOX_RUNNER_EXECUTION_MODE` | `inprocess` or `subprocess` | `inprocess` |
| `MINDROOM_SANDBOX_RUNNER_SUBPROCESS_TIMEOUT_SECONDS` | Subprocess timeout | `120` |
| `MINDROOM_STORAGE_PATH` | Writable directory for tool registry init and worker-local caches (e.g., `/app/workspace/.mindroom`) | `mindroom_data` next to config _(will fail if not writable)_ |
| `MINDROOM_CONFIG_PATH` | Path to config.yaml (for plugin tool registration) | _(optional)_ |

## Execution modes

| Mode | Behavior |
|------|----------|
| `selective` | Only tools listed in `MINDROOM_SANDBOX_PROXY_TOOLS` are proxied. Recommended. |
| `all` / `sandbox_all` | Every tool call goes through the proxy |
| `off` / `local` / `disabled` | Proxy disabled even if URL is set, and execution tools may run in the primary runtime |
| _(unset)_ | With a configured static proxy URL, proxies all tools for legacy compatibility. With `MINDROOM_WORKER_BACKEND=docker` or `kubernetes`, routes default worker tools and fails closed if the backend is misconfigured. With plain `static_runner` and no proxy URL, tools execute locally. |

`MINDROOM_UNSAFE_ALLOW_LOCAL_EXECUTION_TOOLS=true` is an explicit escape hatch for local development that restores primary-runtime execution for `coding`, `docker`, `file`, `python`, and `shell` after routing was explicitly requested without a working worker/proxy backend.
Do not set it in hosted or multi-tenant deployments.

## Shell env and PATH

When `shell` runs through the sandbox proxy, it receives only a small non-secret system env by default, such as `PATH`, `HOME`, `USER`, `TMPDIR`, locale variables, proxy variables, and certificate path variables.
Committed runtime `.env` values and provider credentials are not forwarded implicitly.
Worker startup env also denies provider API keys such as `OPENAI_API_KEY` and `ANTHROPIC_API_KEY` by default.
Configure `extra_env_passthrough` with exact names or glob patterns for exported process env variables you want shell execution to inherit.
`extra_env_passthrough` matches exported process env, not config-adjacent `.env` entries.
To prevent runtime control material from reaching tools, shell passthrough drops credential seed declarations, Kubernetes worker backend config env names, runner control names including `MINDROOM_CREDENTIALS_ENCRYPTION_KEY`, and any name starting with `MINDROOM_SANDBOX_`.
Everything else that matches your configured names or globs passes through, including service tokens and provider credentials.
If you don't want a value to reach shell commands, don't match it with `extra_env_passthrough`.

If proxied shell commands need extra PATH entries such as wrapper directories, configure `shell_path_prepend`.
This prepends the configured entries ahead of the runtime PATH while preserving the existing PATH order and removing duplicates.
That keeps PATH handling deployment-specific instead of baking host-specific directories into the shell tool itself.

## Brokered worker egress

For tools that should call external APIs without receiving the real upstream credential, configure a worker egress broker.
MindRoom injects proxy and CA env into worker-routed `shell` and `python` requests; the worker process then uses the proxy for HTTP(S) traffic.
This does not inspect command lines or match API URLs, so URLs hidden inside bash scripts, Python code, package CLIs, or subprocesses still route through the broker.
Run the broker as a separate proxy service or sidecar.
It holds the upstream session or credential, while MindRoom workers receive only the broker's local proxy URL.

Example:

```yaml
worker_egress_brokers:
  agent_vault:
    proxy_url: http://egress-proxy:8080
    ca_bundle: /etc/ssl/agent-vault-ca.pem
    no_proxy: localhost,127.0.0.1,.svc

defaults:
  worker_tools: [shell, python]
  worker_scope: user_agent
  worker_egress_broker: agent_vault

agents:
  code:
    display_name: Code
    tools: [shell, python]
```

For each brokered worker request MindRoom adds:

- `HTTP_PROXY`, `HTTPS_PROXY`, `http_proxy`, and `https_proxy`
- `REQUESTS_CA_BUNDLE`, `CURL_CA_BUNDLE`, and `SSL_CERT_FILE` when `ca_bundle` is set
- `NO_PROXY` and `no_proxy` when `no_proxy` is set

Agent-level `worker_egress_broker` overrides `defaults.worker_egress_broker`.
Set `worker_egress_broker: false` on an agent to disable an inherited broker.
Unknown broker names fail config validation when the config is loaded.

Broker profiles are backend-neutral: the same config works for Docker/static runners and dedicated Kubernetes workers as long as the worker can reach `proxy_url` and read the configured CA path.
The proxy should be an adapter or broker that owns the real credential or broker session.
Do not put upstream API tokens in `extra_env_passthrough` or `.mindroom/worker-env.sh` unless you intentionally want the worker process to receive them.

### Per-worker Agent Vault egress (Kubernetes backend)

For per-user/per-agent isolation, the Kubernetes worker backend gives each dedicated worker its own Agent Vault identity against a single shared Agent Vault server — no per-worker bridge pod, Service, or NetworkPolicy.
When enabled, each worker pod gets an init container that logs in with the instance owner credential (read from the bootstrap Secret's `AGENT_VAULT_OWNER_PASSWORD` key, mounted only on the init container), creates the worker's vault if missing (`worker_id_for_key(worker_key, prefix=vaultNamePrefix)`), then creates — or rotates — a proxy-role Agent Vault agent for that vault and writes its token to an in-pod `emptyDir`.
The sandbox runner reads that token at execution time and composes `http://<token>:@<proxy host>` for python/shell only (Agent Vault accepts the token as the proxy basic-auth username), so credentials are injected in transit.

```bash
MINDROOM_KUBERNETES_AGENT_VAULT_ENABLED=true
MINDROOM_KUBERNETES_AGENT_VAULT_CLI_IMAGE=infisical/agent-vault:<pinned>
MINDROOM_KUBERNETES_AGENT_VAULT_OWNER_EMAIL=vault-owner@example.test
# optional overrides and their defaults:
MINDROOM_KUBERNETES_AGENT_VAULT_VAULT_NAME_PREFIX=agent-vault
MINDROOM_KUBERNETES_AGENT_VAULT_API_URL=http://agent-vault:14321
MINDROOM_KUBERNETES_AGENT_VAULT_PROXY_URL=http://agent-vault:14322
MINDROOM_KUBERNETES_AGENT_VAULT_BOOTSTRAP_SECRET_NAME=agent-vault-bootstrap
```

The proxy token lives only in the worker pod (in the `emptyDir` file and the python/shell subprocess env), never as a Kubernetes Secret, never in the primary, and never in process arguments; every worker restart rotates it.
The agent's shell can read its own proxy token, but that token is proxy-role: it cannot read or decrypt credentials through the Agent Vault API and cannot reach any other worker's vault (see the isolation smoke below), so the isolation boundary is the per-worker vault scope rather than a network hop.

For HTTPS, Agent Vault terminates TLS at its proxy, so workers must trust the vault root CA.
Publish the CA (from `agent-vault ca fetch`) as a ConfigMap with key `ca.pem` and set `MINDROOM_KUBERNETES_AGENT_VAULT_WORKER_CA_CONFIGMAP_NAME` (chart: `workers.kubernetes.agentVault.workerCaConfigMapName`).
Worker pods mount it at `/etc/agent-vault/ca.pem` and the runner exports `REQUESTS_CA_BUNDLE`/`CURL_CA_BUNDLE`/`SSL_CERT_FILE` for python/shell egress.

If you also enable the worker egress-proxy / approved-egress NetworkPolicies, they restrict worker egress and do not open the Agent Vault ports.
Allow the worker pods and their init container to reach the Agent Vault api (`apiUrl`) and proxy (`proxyUrl`) ports — e.g. via `egressProxy.networkPolicy.extraEgress` — or the init container's health-check loop times out and the pod never starts.

For non-Kubernetes deployments, use a `static` worker egress broker pointing at a shared proxy you run yourself (the broker only sets proxy env; it does not provision anything).

### Self-service vault access

The `agent_vault_access` tool lets a user ask their own agent for a link to manage that agent's vault.
It resolves the caller's worker target to that worker's vault (`worker_id_for_key(worker_key, prefix)`, matching `agentVault.vaultNamePrefix`), grants the caller's Agent Vault account membership of that vault through the API, and returns the gated UI link.
Configure it per deployment:

```bash
MINDROOM_AGENT_VAULT_ACCESS_API_URL=http://agent-vault:14321
MINDROOM_AGENT_VAULT_ACCESS_ADMIN_TOKEN=<owner or admin session/agent token>
MINDROOM_AGENT_VAULT_ACCESS_UI_BASE_URL=https://example.com/agent-vault
MINDROOM_AGENT_VAULT_ACCESS_EMAIL_DOMAIN=example.com
MINDROOM_AGENT_VAULT_ACCESS_VAULT_NAME_PREFIX=agent-vault  # must match workers.kubernetes.agentVault.vaultNamePrefix
```

The tool maps a requester's Matrix localpart to `localpart@EMAIL_DOMAIN` for the account grant.
That mapping only decides *UI management access*; it never changes which worker reaches which vault, so the runtime secret boundary stays the per-worker vault scope plus the in-pod proxy-role token.
The grant is idempotent and requires the user to have already registered and verified an Agent Vault account.

### Validating the isolation model

To validate the multi-identity isolation model end-to-end with Docker and the real `infisical/agent-vault` image, run:

```bash
uv run python tests/manual/agent_vault_isolation_live_smoke.py
```

The isolation smoke provisions one vault plus one proxy-role Agent Vault agent per worker identity, then proves each agent token injects only its own vault's credential, gets no injection for another vault's service, cannot list or decrypt another vault's credentials via the API, and cannot decrypt even its own vault's credentials.
It also proves garbage and missing proxy session tokens are refused.

Shell commands that exceed their timeout return a background handle.
Use `check_shell_command(handle)` to poll and `kill_shell_command(handle)` to stop the process.
These handles are process-local to the sandbox runner: they survive multiple requests to the same runner process, but not runner restarts.
To make that work, shell background-handle requests stay owned by the long-lived runner process even when `MINDROOM_SANDBOX_RUNNER_EXECUTION_MODE=subprocess`.

## Workspace home contract

For worker-routed shell or python requests with a resolved workspace, MindRoom sets `HOME` and `MINDROOM_AGENT_WORKSPACE` to that workspace before running the tool.
This includes agent-routed calls, worker-keyed calls whose prepared runtime has a `base_dir`, and unkeyed static-sidecar calls with an explicit absolute `base_dir` override.
It also sets `XDG_CONFIG_HOME`, `XDG_DATA_HOME`, and `XDG_STATE_HOME` under that workspace.
Workspace identity variables and worker cache variables are owned by MindRoom for the request.
`HOME`, `MINDROOM_AGENT_WORKSPACE`, `XDG_CONFIG_HOME`, `XDG_DATA_HOME`, and `XDG_STATE_HOME` stay under the workspace.
`XDG_CACHE_HOME`, `PIP_CACHE_DIR`, `UV_CACHE_DIR`, and `PYTHONPYCACHEPREFIX` stay under the worker cache directory when a worker root exists.
`VIRTUAL_ENV` is preserved from the active worker environment and is not pointed at the agent workspace.
MindRoom reasserts these owned variables after request env passthrough and after `.mindroom/worker-env.sh`, so hooks can read them but cannot redirect them.
The practical contract is that `pwd`, `~`, `Path.home()`, attachment `mindroom_output_path` saves, and file/coding relative paths all refer to the same workspace.
For example, after `get_attachment("att_...", mindroom_output_path="incoming/file.txt")`, worker-routed shell can read both `incoming/file.txt` and `~/incoming/file.txt`.

## Workspace env hook (`.mindroom/worker-env.sh`)

Agents can drop a shell script at `<workspace>/.mindroom/worker-env.sh` to set custom env for worker-routed tool calls without changing config or redeploying.

The runner sources this script with `bash` after applying the workspace home contract and before each worker-routed `shell` or `python` request, then merges its exported env into the tool's execution environment.

**Discovery:**

- For agent-routed worker requests, the hook lives at the resolved agent workspace root as `.mindroom/worker-env.sh`.
- For shared and unscoped agents that means `agents/<agent>/workspace/.mindroom/worker-env.sh`.
- For private agents that means `private_instances/<scope>/<agent>/workspace/.mindroom/worker-env.sh`.
- For `worker_scope: user`, the hook follows the per-request workspace, so one shared user runtime can pick up different hooks as it works in different agent workspaces.
- For unkeyed static-sidecar proxy calls (no `worker_key`), the hook is discovered from `tool_init_overrides["base_dir"]` only when that value is an absolute path; relative strings are ignored on this path because there is no canonical workspace root to resolve them against.

**Semantics:**

- Edits take effect on the next worker-routed tool call. No pod restart, no config reload, no Helm change.
- The script must `export FOO=bar` for values to overlay; bare `FOO=bar` does not persist (no `set -a`).
- Filesystem side effects inside the worker sandbox are allowed because the hook is arbitrary agent-editable shell.
- Shell aliases, functions, and `cd` do not persist — only exported env crosses the boundary.

**Filtering:**

`.mindroom/worker-env.sh` is sourced by bash that inherits the runner's process env, which contains tokens the runner needs to function (sandbox proxy auth, etc.).
To prevent runtime control material from reaching tools, the overlay drops credential seed declarations, Kubernetes worker backend config env names, runner control names including `MINDROOM_CREDENTIALS_ENCRYPTION_KEY`, and any name starting with `MINDROOM_SANDBOX_`.
Bash bookkeeping vars (`PWD`, `OLDPWD`, `SHLVL`, `_`, `PIPESTATUS`) are also dropped because they're noise, not values the script meant to export.
After MindRoom-owned env names are reasserted, other exported values pass through, including service tokens and provider credentials you intentionally export from the hook.
If you don't want a value to reach tools, don't export it.

**Limits and failure handling:**

- Script ≤ 64 KiB; stdout and stderr capture each ≤ 256 KiB; total overlay ≤ 128 KiB; per-value ≤ 32 KiB.
- Hook execution times out after 10 seconds.
- Symlinks that escape the workspace are rejected.
- Any failure (non-zero exit, timeout, escape, missing `bash`) returns the tool call as `ok: false` with `failure_kind: "tool"` and an error mentioning `.mindroom/worker-env.sh`.
- Hook failures do not poison the worker; only the requesting tool call fails.

This hook works identically for the static sidecar and dedicated Kubernetes worker backends because it runs inside the sandbox runner per request.
It is not a true container startup hook — it does not change pod templates, recreate Deployments, or alter Helm values.
For an example, see `docs/tools/execution-and-coding.md`.

## Credential leases

Some proxied tools need credentials, such as a `shell` tool that runs `git push` and needs an SSH key.
Rather than giving the runner permanent access to secrets, the primary MindRoom runtime creates a **credential lease**.
That lease is a short-lived, single-use token that the runner exchanges for credentials during execution.

Configure which credentials are shared via `MINDROOM_SANDBOX_CREDENTIAL_POLICY_JSON`:

```bash
export MINDROOM_SANDBOX_CREDENTIAL_POLICY_JSON='{"shell": ["github"], "python": ["openai"]}'
```

This shares the `github` credential service with `shell` tool calls and `openai` with `python` tool calls.
Credentials are never stored in the runner.
Each lease is consumed on use and expires after the configured TTL.

## Security considerations

- The worker runtime never gets the primary runtime API key files, Matrix client state, or orchestrator authority.
- The sandbox token authenticates proxy traffic, so use a strong random value.

  Kubernetes dedicated workers derive per-worker runner tokens from the control-plane token.
- Credential leases are single-use by default and expire after 60 seconds.
- The worker container `securityContext` drops all capabilities and disables privilege escalation.
- With `workerBackend: static_runner`, the Kubernetes sidecar uses `emptyDir` scratch space and shares access to the same agent storage directories as the main process.
- With `workerBackend: kubernetes`, dedicated workers for `shared`, `user_agent`, and unscoped execution only mount their own agent's directory plus their worker scratch space. `user` mode intentionally mounts the broader `agents/` tree since it shares one runtime across agents.
- The primary MindRoom runtime does not mount the sandbox-runner router, so `/api/sandbox-runner/` exists only in runner or dedicated worker processes.

### Sandbox-runner API endpoints

These endpoints are served by the sandbox-runner process, not the primary MindRoom runtime.
All requests require the runner's `MINDROOM_SANDBOX_PROXY_TOKEN` in the `x-mindroom-sandbox-token` header.

| Method | Endpoint | Description |
|--------|----------|-------------|
| POST | `/api/sandbox-runner/leases` | Create a one-time credential lease for an upcoming tool call |
| POST | `/api/sandbox-runner/execute` | Execute a tool call with optional credential override via lease |
| GET | `/api/sandbox-runner/workers` | List known workers with lifecycle metadata |
| POST | `/api/sandbox-runner/workers/cleanup` | Mark idle workers for cleanup without deleting persisted state |

Credential leases are single-use: once consumed by an `/execute` call, the lease cannot be replayed.

## Per-agent configuration

MindRoom owns the default local-versus-worker routing policy.
You can override which tools are routed through the sandbox proxy per agent, or set a default for all agents, in `config.yaml`.
Per-agent tool config overrides, such as inline `shell: {extra_env_passthrough: "DAWARICH_*"}` syntax in agent `tools` lists, are threaded through the sandbox proxy so workers receive the merged overrides alongside credentials and runtime overrides.
See [Per-Agent Tool Configuration](../configuration/agents.md#per-agent-tool-configuration) for the full syntax.

```yaml
defaults:
  worker_tools: [shell, file]        # route shell+file through the sandbox proxy for all agents by default

agents:
  code:
    tools: [file, shell, calculator]
    # inherits worker_tools from defaults → shell and file proxied

  research:
    tools: [web_search, calculator]
    worker_tools: []                 # explicitly no proxying

  untrusted:
    tools: [shell, file, python]
    worker_tools: [shell, file, python]   # proxy everything
```

The `worker_tools` field has three states:

| Value | Behavior |
|-------|----------|
| `null` (omitted) | Use MindRoom's built-in default routing policy. Today that defaults to `coding`, `docker`, `file`, `python`, and `shell` when those tools are enabled for the agent |
| `[]` (empty list) | Explicitly disable sandbox proxying for this agent |
| `["shell", "file"]` | Proxy exactly these tools for this agent |

Agent-level `worker_tools` overrides `defaults.worker_tools`.
Registry-backed tools can be listed in `worker_tools`, and MindRoom will attempt to route them through the worker runtime.
Some local-only tools stay in the primary runtime even when listed: `attachments`, `gmail`, `google_calendar`, `google_drive`, `google_sheets`, and `homeassistant`.
With `MINDROOM_WORKER_BACKEND=static_runner`, a sandbox proxy URL (`MINDROOM_SANDBOX_PROXY_URL`) must be configured for selected execution tools to run.
Without that URL, explicitly selected worker-routed tools fail closed unless `MINDROOM_SANDBOX_EXECUTION_MODE=off|local|disabled` or `MINDROOM_UNSAFE_ALLOW_LOCAL_EXECUTION_TOOLS=true` is set.
If `worker_tools` is omitted and no static proxy URL is configured, simple local installs run those tools in the primary MindRoom process.
With `MINDROOM_WORKER_BACKEND=docker` or `MINDROOM_WORKER_BACKEND=kubernetes`, worker endpoints are resolved dynamically and `MINDROOM_SANDBOX_PROXY_URL` is not used.

## Worker Scope

`worker_tools` controls which tools run in the sandbox proxy.
`worker_scope` controls how those sandbox runtimes are shared between calls.
Some credential-backed tools always stay local regardless of `worker_tools`: `gmail`, `google_calendar`, `google_drive`, `google_sheets`, and `homeassistant`.
Additionally, `spotify` is a shared-only integration that requires `worker_scope` unset or `shared` but can still be proxied through the sandbox.
The built-in `memory`, `delegate`, and `self_config` tools are also created directly in the primary runtime today and are not routed through `worker_tools`.

You can set `worker_scope` per agent or in `defaults`:

```yaml
defaults:
  worker_tools: [shell, file]
  worker_scope: user_agent

agents:
  code:
    tools: [shell, file]
    # inherits worker_scope=user_agent

  reviewer:
    tools: [shell, file]
    worker_scope: shared

  bridge_helper:
    tools: [shell]
    worker_scope: user
```

The supported values are:

| Value | Behavior |
|-------|----------|
| `shared` | One runtime per agent, shared by all users |
| `user` | One runtime per user, shared across that user's agents |
| `user_agent` | One runtime per user+agent pair |

If `worker_scope` is unset, proxied tools still use the sandbox runner and the request stays unscoped.
With `MINDROOM_WORKER_BACKEND=static_runner`, no worker-specific storage root is selected.
With `MINDROOM_WORKER_BACKEND=docker` or `MINDROOM_WORKER_BACKEND=kubernetes`, MindRoom still provisions one unscoped worker per agent and tenant/account.
`worker_scope` also affects dashboard credential support and OpenAI-compatible agent eligibility.

**Important notes:**

- `worker_scope` does **not** change where agent data is stored.
  All scopes read and write the same agent storage directory (`agents/<name>/`).
- The dashboard's generic credential forms only work for unscoped agents and agents with `worker_scope=shared`.
  OAuth providers that support scoped dashboard flows, such as the Google Drive, Gmail, Calendar, and Sheets providers, are the exception.
  For those providers, the dashboard can connect scoped `user` and `user_agent` credentials, but the Google tools still execute in the primary MindRoom runtime.
  Tools without a scoped OAuth provider still manage `user` and `user_agent` credentials through their worker runtime.
- `user` mode shares one runtime across multiple agents for a single user, so agents in that runtime can access each other's files.
  Use `user_agent` for per-agent isolation.

## Without configured worker routing

With `MINDROOM_WORKER_BACKEND=static_runner` and no `MINDROOM_SANDBOX_PROXY_URL`, tool calls execute directly in the primary MindRoom runtime process.
This is fine for development but not recommended for production deployments where agents run untrusted code.
With `MINDROOM_WORKER_BACKEND=docker` or `MINDROOM_WORKER_BACKEND=kubernetes`, worker-routed tool calls fail closed when the backend is misconfigured instead of silently running locally.
