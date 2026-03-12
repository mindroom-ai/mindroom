---
icon: lucide/shield
---

# Sandbox Proxy Isolation

When agents have code-execution tools (`shell`, `file`, `python`), they can read and modify anything on the filesystem, including config files, credentials, and application code.
The **sandbox proxy** isolates these tools by forwarding their calls to a separate worker runtime that has no direct access to the primary process secrets.

## How it works

```
┌──────────────────────────┐         HTTP          ┌──────────────────────────┐
│ Primary MindRoom runtime │  ── tool call ──▶     │ Worker runtime           │
│ has secrets              │  ◀── result ───       │ no primary secrets       │
│ has credentials          │                       │ leased credentials only  │
│ has orchestration state  │                       │ worker-owned state       │
└──────────────────────────┘                       └──────────────────────────┘
```

1. Agent invokes `shell.run_shell_command(...)` or another worker-routed tool.
2. The primary MindRoom runtime resolves the target worker from the configured backend plus worker scope.
3. The call is forwarded over HTTP to the target worker runtime.
4. The worker executes the tool locally against its own state and returns the result.
5. All other tools such as API tools or Matrix-bound tools execute in the primary MindRoom runtime as usual.

The worker runtime authenticates requests with a shared token (`MINDROOM_SANDBOX_PROXY_TOKEN`).
For tools that need credentials, such as a shell tool that calls an authenticated API, the primary MindRoom runtime can create a short-lived **credential lease** that the worker consumes once.
Credentials never become part of the normal tool arguments or the model prompt.

MindRoom currently ships three worker backend shapes:

- `static_runner`: one shared sandbox-runner process, usually a sidecar container or a local HTTP service.
- `docker`: dedicated worker containers created on demand from the primary runtime, with one logical worker per worker key.
- `kubernetes`: dedicated worker pods created on demand from the primary runtime, with one logical worker per worker key.

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

> [!IMPORTANT]
> The `sandbox-workspace` Docker volume is created as root by default. The runner runs as UID 1000, so you must fix ownership after first creating the volume:
> ```bash
> docker run --rm -v sandbox-workspace:/workspace busybox chown -R 1000:1000 /workspace
> ```
> Alternatively, omit the `user:` directive to run as root (less secure).

Key differences from the primary MindRoom runtime:
- **No `env_file`** — runner has no API keys, no Matrix credentials
- **No data volume** — runner cannot access `mindroom_data/`
- **Scratch workspace** — a dedicated volume for file operations
- **`MINDROOM_STORAGE_PATH`** — pointed at a writable location inside the workspace so the tool registry can initialize without access to the primary data volume

### Kubernetes shared sidecar (`workerBackend: static_runner`)

In Kubernetes the shared runner can still run as a second container in the same pod, sharing `localhost` networking.
This is the `workerBackend: static_runner` Helm mode.
See `cluster/k8s/instance/templates/deployment-mindroom.yaml` for the full manifest.
The sidecar gets:

- An `emptyDir` volume for scratch workspace.
- Read-only access to config for plugin tool registration.
- No access to the primary secrets volume.

### Kubernetes dedicated workers (`workerBackend: kubernetes`)

In dedicated-worker mode the primary MindRoom runtime creates worker Deployments and Services on demand.
Each worker pod runs the sandbox-runner app and is addressed through an internal cluster Service.
The worker state is mounted from the shared instance PVC under a worker-specific subpath, so files, virtualenvs, caches, sessions, and other worker-owned state survive pod recreation.
Idle cleanup scales worker Deployments to zero while keeping the PVC-backed state intact.

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

- `storageAccessMode` should be `ReadWriteMany` for multi-node shared storage.
- If you must keep `ReadWriteOnce`, set `controlPlaneNodeName` so the control plane and dedicated workers stay on the same node.
- `kubernetesWorkerImage` and `kubernetesWorkerImagePullPolicy` default to the main MindRoom image settings when left empty.
- The chart creates the worker-manager ServiceAccount, Role, RoleBinding, and worker-specific NetworkPolicy rules automatically when this backend is enabled.
- The primary runtime does not need `MINDROOM_SANDBOX_PROXY_URL` in this mode because worker endpoints come from the Kubernetes worker handles.
- The authenticated `/api/workers` and `/api/workers/cleanup` endpoints on the primary runtime expose backend-neutral worker lifecycle information.

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
That most commonly means `shell`, `file`, and `python`, but other worker-safe tools can also be routed through workers when they only need worker state or files under the mounted config directory.
The Docker backend starts one worker container per worker key and reuses it until the container goes idle or the Docker launch configuration changes.
This is the simplest way to get one persistent container per agent without running Kubernetes.
MindRoom mounts the directory containing `MINDROOM_DOCKER_WORKER_HOST_CONFIG_PATH` read-only into each worker so config-relative plugin and knowledge paths keep working.
MindRoom also masks config-adjacent `.env` inside the worker container, so primary-runtime secrets stay local unless you pass worker-specific env vars explicitly.

MindRoom auto-installs the optional `docker` extra the first time this backend is used.
If you disable auto-install with `MINDROOM_NO_AUTO_INSTALL_TOOLS=1`, install it yourself with `uv sync --extra docker` in a source checkout or `pip install 'mindroom[docker]'`.
If you are testing unreleased code from a source checkout, start MindRoom from that checkout instead of the published PyPI build.
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

Then start MindRoom from the same checkout:

```bash
uv run mindroom run
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
| `MINDROOM_SANDBOX_PROXY_URL` | URL of the shared sandbox runner when using `static_runner` | _(none — proxy disabled for `static_runner`)_ |
| `MINDROOM_SANDBOX_PROXY_TOKEN` | Shared auth token used by the worker runtime | _(required for worker-routed execution)_ |
| `MINDROOM_SANDBOX_EXECUTION_MODE` | `selective`, `all`, `off` | _(unset — uses proxy tools list)_ |
| `MINDROOM_SANDBOX_PROXY_TOOLS` | Comma-separated tool names to proxy | `*` (all, unless mode is `selective`) |
| `MINDROOM_SANDBOX_PROXY_TIMEOUT_SECONDS` | HTTP timeout for proxy calls | `120` |
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
| `MINDROOM_DOCKER_WORKER_HOST_CONFIG_PATH` | Host path to `config.yaml`; its parent directory is mounted read-only into workers and `.env` is masked inside the container | Resolved `MINDROOM_CONFIG_PATH` when it exists |
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
| `MINDROOM_SANDBOX_RUNNER_MODE` | Set to `true` to indicate runner mode | `false` |
| `MINDROOM_SANDBOX_PROXY_TOKEN` | Shared auth token (must match primary) | _(required)_ |
| `MINDROOM_SANDBOX_RUNNER_EXECUTION_MODE` | `inprocess` or `subprocess` | `inprocess` |
| `MINDROOM_SANDBOX_RUNNER_SUBPROCESS_TIMEOUT_SECONDS` | Subprocess timeout | `120` |
| `MINDROOM_STORAGE_PATH` | Writable directory for tool registry init (e.g., `/app/workspace/.mindroom`) | `mindroom_data` next to config _(will fail if not writable)_ |
| `MINDROOM_CONFIG_PATH` | Path to config.yaml (for plugin tool registration) | _(optional)_ |

## Execution modes

| Mode | Behavior |
|------|----------|
| `selective` | Only tools listed in `MINDROOM_SANDBOX_PROXY_TOOLS` are proxied. Recommended. |
| `all` / `sandbox_all` | Every tool call goes through the proxy |
| `off` / `local` / `disabled` | Proxy disabled even if URL is set |
| _(unset)_ | If `MINDROOM_SANDBOX_PROXY_TOOLS` is `*` or unset, proxies all tools; if set to a list, proxies only those |

## Credential leases

Some proxied tools need credentials (e.g., a `shell` tool that runs `git push` and needs an SSH key). Rather than giving the runner permanent access to secrets, the primary MindRoom runtime creates a **credential lease** — a short-lived, single-use token that the runner exchanges for credentials during execution.

Configure which credentials are shared via `MINDROOM_SANDBOX_CREDENTIAL_POLICY_JSON`:

```bash
export MINDROOM_SANDBOX_CREDENTIAL_POLICY_JSON='{"shell": ["github"], "python": ["openai"]}'
```

This shares the `github` credential service with `shell` tool calls and `openai` with `python` tool calls. Credentials are never stored in the runner — each lease is consumed on use and expires after the configured TTL.

## Security considerations

- The worker runtime never gets the primary runtime API key files, Matrix client state, or orchestrator authority.
- The shared token authenticates all proxy traffic, so use a strong random value.
- Credential leases are single-use by default and expire after 60 seconds.
- The worker container `securityContext` drops all capabilities and disables privilege escalation.
- With `workerBackend: static_runner`, the Kubernetes sidecar uses `emptyDir` scratch space and has no persistent state of its own.
- With `workerBackend: kubernetes`, dedicated worker pods mount a worker-specific PVC subpath and keep worker-owned state across pod recreation.
- The primary MindRoom runtime does not mount the sandbox-runner router, so `/api/sandbox-runner/` exists only in runner or dedicated worker processes.

## Per-agent configuration

MindRoom owns the default local-versus-worker routing policy. You can override which tools are routed through the sandbox proxy per agent (or set a default for all agents) in `config.yaml`:

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
| `null` (omitted) | Use MindRoom's built-in default routing policy. Today that defaults to `coding`, `file`, `python`, and `shell` when those tools are enabled for the agent |
| `[]` (empty list) | Explicitly disable sandbox proxying for this agent |
| `["shell", "file"]` | Proxy exactly these tools for this agent |

Agent-level `worker_tools` overrides `defaults.worker_tools`.
Any tool can be listed in `worker_tools`, and MindRoom will attempt to route it through the worker runtime.
With `MINDROOM_WORKER_BACKEND=static_runner`, a sandbox proxy URL (`MINDROOM_SANDBOX_PROXY_URL`) must still be configured for proxying to take effect.
With `MINDROOM_WORKER_BACKEND=docker` or `MINDROOM_WORKER_BACKEND=kubernetes`, worker endpoints are resolved dynamically and `MINDROOM_SANDBOX_PROXY_URL` is not used.

## Worker Scope

`worker_tools` chooses which tools execute through the sandbox proxy.
`worker_scope` chooses which proxied calls share the same worker-owned storage root.
Some tools still stay local even if they are listed in `worker_tools`.
Currently that local-only set is `gmail`, `google_calendar`, `google_sheets`, and `homeassistant`.
`google` and `spotify` may be worker-routed, but only for unscoped agents or agents with `worker_scope=shared`.

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
    worker_scope: room_thread
```

The supported values are:

| Value | Behavior |
|-------|----------|
| `shared` | One shared worker state per agent, which is the setting to use when you want one persistent Docker container per agent |
| `user` | One worker state per requester, shared across agents |
| `user_agent` | One worker state per requester and agent |
| `room_thread` | One worker state per thread, or per room when no thread exists |

If `worker_scope` is unset, proxied tools still use the sandbox runner and the request stays unscoped.
With `MINDROOM_WORKER_BACKEND=static_runner`, no worker-specific storage root is selected.
With `MINDROOM_WORKER_BACKEND=docker` or `MINDROOM_WORKER_BACKEND=kubernetes`, MindRoom still provisions one unscoped worker per agent and tenant/account.
`worker_scope` also affects dashboard credential support and OpenAI-compatible agent eligibility.
The dashboard credential UI only supports unscoped agents and agents with `worker_scope=shared`.
Agents using `user`, `user_agent`, or `room_thread` must treat credentials as runtime-owned worker state.

## Without configured worker routing

With `MINDROOM_WORKER_BACKEND=static_runner` and no `MINDROOM_SANDBOX_PROXY_URL`, tool calls execute directly in the primary MindRoom runtime process.
This is fine for development but not recommended for production deployments where agents run untrusted code.
With `MINDROOM_WORKER_BACKEND=docker` or `MINDROOM_WORKER_BACKEND=kubernetes`, worker-routed tool calls fail closed when the backend is misconfigured instead of silently running locally.
