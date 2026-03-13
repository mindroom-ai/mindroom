# Sandbox Proxy Isolation

When agents have code-execution tools (`shell`, `file`, `python`), they can read and modify anything on the filesystem, including config files, credentials, and application code. The **sandbox proxy** isolates these tools by forwarding their calls to a separate worker runtime that has no direct access to the primary process secrets. This page describes the current sandboxed execution model.

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
1. The primary MindRoom runtime resolves the target worker from the configured backend plus worker scope.
1. The call is forwarded over HTTP to the target worker runtime.
1. The worker executes the tool locally against the agent's canonical state plus any worker-local caches and returns the result.
1. All other tools such as API tools or Matrix-bound tools execute in the primary MindRoom runtime as usual.

The worker runtime authenticates requests with a shared token (`MINDROOM_SANDBOX_PROXY_TOKEN`). For tools that need credentials, such as a shell tool that calls an authenticated API, the primary MindRoom runtime can create a short-lived **credential lease** that the worker consumes once. Credentials never become part of the normal tool arguments or the model prompt.

MindRoom currently ships two worker backend shapes:

- `static_runner`: one shared sandbox-runner process, usually a sidecar container or a local HTTP service.
- `kubernetes`: dedicated worker pods created on demand from the primary runtime, with one logical worker per worker key.

## State Ownership

Each agent has one canonical state root. That root is the source of truth for the agent's context files, workspace files, file-backed memory, mem0-backed state, session and history state, and learning state. All worker scopes read and write that same canonical agent state root. Worker runtimes may keep their own virtualenvs, caches, scratch files, and provider metadata, but those files are not authoritative agent state. Multiple runtimes may access the same canonical agent state root concurrently, so sensitive files and databases must tolerate concurrent writers or use explicit locking.

## Deployment modes

### Docker Compose (`static_runner`)

Add a `sandbox-runner` service alongside MindRoom. Both use the same image. The runner just has a different entrypoint and no access to `.env` or the primary data volume.

```
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

Add a shared agent-state volume or bind mount that both the primary runtime and the runner can access. Keep secrets and other primary-only files on separate mounts that are not exposed to the runner.

> [!IMPORTANT] The `sandbox-workspace` Docker volume is created as root by default. The runner runs as UID 1000, so you must fix ownership after first creating the volume: `bash docker run --rm -v sandbox-workspace:/workspace busybox chown -R 1000:1000 /workspace` Alternatively, omit the `user:` directive to run as root (less secure).

Key differences from the primary MindRoom runtime:

- **No `env_file`** — runner has no API keys, no Matrix credentials
- **Shared agent-state access** — the runner reads and writes the same canonical agent state roots used by the primary runtime
- **Scratch workspace** — a dedicated volume for worker-local runtime files and caches
- **`MINDROOM_STORAGE_PATH`** — pointed at a writable location inside the worker-local workspace so the tool registry and cache files have a private runtime home

> [!WARNING] Shared-runner and local worker backends still expose broader shared storage to the runner process. Dedicated Kubernetes workers now narrow mounts for `shared`, `user_agent`, and unscoped dedicated execution, but `user` remains intentionally broader. For filesystem-capable tools such as `shell`, `file`, `python`, and `coding`, `base_dir` is not a hard security boundary unless the backend also narrows the visible mounts. `user` creates one persistent runtime per requester. Multiple agents may run inside that runtime. Those agents may access each other's mounted files inside that runtime. Treat `user` as a per-requester workstation or trust-sharing mode. Use `user_agent` if you need the clearest per-agent filesystem isolation.

### Kubernetes shared sidecar (`workerBackend: static_runner`)

In Kubernetes the shared runner can still run as a second container in the same pod, sharing `localhost` networking. This is the `workerBackend: static_runner` Helm mode. See `cluster/k8s/instance/templates/deployment-mindroom.yaml` for the full manifest. The sidecar gets:

- An `emptyDir` volume for worker-local scratch workspace and caches.
- Access to the same shared storage that exposes canonical agent state roots to the primary runtime.
- Read-only access to config for plugin tool registration.
- No access to the primary secrets volume.

### Kubernetes dedicated workers (`workerBackend: kubernetes`)

In dedicated-worker mode the primary MindRoom runtime creates worker Deployments and Services on demand. Each worker pod runs the sandbox-runner app and is addressed through an internal cluster Service. Dedicated workers must be able to access the canonical agent state roots for the agents they execute, while still keeping worker-local caches and metadata isolated by worker key. Idle cleanup scales worker Deployments to zero while preserving canonical agent state and any separately retained worker-local caches by policy.

Use the instance Helm chart with values like:

```
workerBackend: kubernetes
workerCleanupIntervalSeconds: 30
storageAccessMode: ReadWriteMany
kubernetesWorkerPort: 8766
kubernetesWorkerReadyTimeoutSeconds: 60
kubernetesWorkerIdleTimeoutSeconds: 1800
sandbox_proxy_token: "replace-me"
```

Important notes for this mode:

- `storageAccessMode` should be `ReadWriteMany` because multiple dedicated workers may need concurrent access to the same canonical agent state root.
- If you must keep `ReadWriteOnce`, set `controlPlaneNodeName` so the control plane and dedicated workers stay on the same node.
- `kubernetesWorkerImage` and `kubernetesWorkerImagePullPolicy` default to the main MindRoom image settings when left empty.
- The chart creates the worker-manager ServiceAccount, Role, RoleBinding, and worker-specific NetworkPolicy rules automatically when this backend is enabled.
- The primary runtime does not need `MINDROOM_SANDBOX_PROXY_URL` in this mode because worker endpoints come from the Kubernetes worker handles.
- The authenticated `/api/workers` and `/api/workers/cleanup` endpoints on the primary runtime expose backend-neutral worker lifecycle information.

For the full Helm-side deployment guidance, see [Kubernetes Deployment](https://docs.mindroom.chat/deployment/kubernetes/index.md).

### Host machine + Docker sandbox container

Run MindRoom directly on the host while isolating code-execution tools in a Docker container:

```
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

```
MINDROOM_WORKER_BACKEND=static_runner
MINDROOM_SANDBOX_PROXY_URL=http://localhost:8766
MINDROOM_SANDBOX_PROXY_TOKEN=your-secret-token
MINDROOM_SANDBOX_EXECUTION_MODE=selective
MINDROOM_SANDBOX_PROXY_TOOLS=shell,file,python
```

This gives you the convenience of running MindRoom natively while keeping code-execution tools inside a container boundary.

> [!TIP] If you use plugin tools that also need proxying, mount your `config.yaml` into the runner container so it can register them: `bash docker run -d \ --name mindroom-sandbox-runner \ -p 8766:8766 \ -v ./config.yaml:/app/config.yaml:ro \ -e MINDROOM_CONFIG_PATH=/app/config.yaml \ -e MINDROOM_SANDBOX_RUNNER_MODE=true \ -e MINDROOM_SANDBOX_PROXY_TOKEN=your-secret-token \ -e MINDROOM_STORAGE_PATH=/app/workspace/.mindroom \ ghcr.io/mindroom-ai/mindroom:latest \ /app/run-sandbox-runner.sh`

## Environment variable reference

### Primary MindRoom runtime (proxy client)

| Variable                                        | Description                                                 | Default                                       |
| ----------------------------------------------- | ----------------------------------------------------------- | --------------------------------------------- |
| `MINDROOM_WORKER_BACKEND`                       | Worker backend name: `static_runner` or `kubernetes`        | `static_runner`                               |
| `MINDROOM_SANDBOX_PROXY_URL`                    | URL of the shared sandbox runner when using `static_runner` | *(none — proxy disabled for `static_runner`)* |
| `MINDROOM_SANDBOX_PROXY_TOKEN`                  | Shared auth token used by the worker runtime                | *(required for worker-routed execution)*      |
| `MINDROOM_SANDBOX_EXECUTION_MODE`               | `selective`, `all`, `off`                                   | *(unset — uses proxy tools list)*             |
| `MINDROOM_SANDBOX_PROXY_TOOLS`                  | Comma-separated tool names to proxy                         | `*` (all, unless mode is `selective`)         |
| `MINDROOM_SANDBOX_PROXY_TIMEOUT_SECONDS`        | HTTP timeout for proxy calls                                | `120`                                         |
| `MINDROOM_SANDBOX_CREDENTIAL_LEASE_TTL_SECONDS` | Credential lease lifetime                                   | `60`                                          |
| `MINDROOM_SANDBOX_CREDENTIAL_POLICY_JSON`       | JSON mapping tool selectors to credential services          | `{}`                                          |

When `MINDROOM_WORKER_BACKEND=kubernetes`, the primary runtime resolves worker endpoints through the Kubernetes backend and does not use `MINDROOM_SANDBOX_PROXY_URL`. The Helm chart sets the Kubernetes backend environment variables automatically. If you deploy that mode without Helm, see [Kubernetes Deployment](https://docs.mindroom.chat/deployment/kubernetes/index.md) and `src/mindroom/workers/backends/kubernetes_config.py` for the required environment surface.

### Sandbox runner

| Variable                                             | Description                                                                                          | Default                                                      |
| ---------------------------------------------------- | ---------------------------------------------------------------------------------------------------- | ------------------------------------------------------------ |
| `MINDROOM_SANDBOX_RUNNER_MODE`                       | Set to `true` to indicate runner mode                                                                | `false`                                                      |
| `MINDROOM_SANDBOX_PROXY_TOKEN`                       | Shared auth token (must match primary)                                                               | *(required)*                                                 |
| `MINDROOM_SANDBOX_RUNNER_EXECUTION_MODE`             | `inprocess` or `subprocess`                                                                          | `inprocess`                                                  |
| `MINDROOM_SANDBOX_RUNNER_SUBPROCESS_TIMEOUT_SECONDS` | Subprocess timeout                                                                                   | `120`                                                        |
| `MINDROOM_STORAGE_PATH`                              | Writable directory for tool registry init and worker-local caches (e.g., `/app/workspace/.mindroom`) | `mindroom_data` next to config *(will fail if not writable)* |
| `MINDROOM_CONFIG_PATH`                               | Path to config.yaml (for plugin tool registration)                                                   | *(optional)*                                                 |

## Execution modes

| Mode                         | Behavior                                                                                                   |
| ---------------------------- | ---------------------------------------------------------------------------------------------------------- |
| `selective`                  | Only tools listed in `MINDROOM_SANDBOX_PROXY_TOOLS` are proxied. Recommended.                              |
| `all` / `sandbox_all`        | Every tool call goes through the proxy                                                                     |
| `off` / `local` / `disabled` | Proxy disabled even if URL is set                                                                          |
| *(unset)*                    | If `MINDROOM_SANDBOX_PROXY_TOOLS` is `*` or unset, proxies all tools; if set to a list, proxies only those |

## Credential leases

Some proxied tools need credentials (e.g., a `shell` tool that runs `git push` and needs an SSH key). Rather than giving the runner permanent access to secrets, the primary MindRoom runtime creates a **credential lease** — a short-lived, single-use token that the runner exchanges for credentials during execution.

Configure which credentials are shared via `MINDROOM_SANDBOX_CREDENTIAL_POLICY_JSON`:

```
export MINDROOM_SANDBOX_CREDENTIAL_POLICY_JSON='{"shell": ["github"], "python": ["openai"]}'
```

This shares the `github` credential service with `shell` tool calls and `openai` with `python` tool calls. Credentials are never stored in the runner — each lease is consumed on use and expires after the configured TTL.

## Security considerations

- The worker runtime never gets the primary runtime API key files, Matrix client state, or orchestrator authority.
- The shared token authenticates all proxy traffic, so use a strong random value.
- Credential leases are single-use by default and expire after 60 seconds.
- The worker container `securityContext` drops all capabilities and disables privilege escalation.
- With `workerBackend: static_runner`, the Kubernetes sidecar uses `emptyDir` scratch space for worker-local runtime files and still accesses the canonical agent state roots used by the primary runtime.
- With `workerBackend: kubernetes`, dedicated worker pods mount only the addressed agent root for `shared`, `user_agent`, and unscoped dedicated execution, while `user` intentionally mounts the broader `agents/` tree and worker-local caches remain isolated by worker key.
- The primary MindRoom runtime does not mount the sandbox-runner router, so `/api/sandbox-runner/` exists only in runner or dedicated worker processes.

## Per-agent configuration

MindRoom owns the default local-versus-worker routing policy. You can override which tools are routed through the sandbox proxy per agent (or set a default for all agents) in `config.yaml`:

```
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

| Value               | Behavior                                                                                                                                                  |
| ------------------- | --------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `null` (omitted)    | Use MindRoom's built-in default routing policy. Today that defaults to `coding`, `file`, `python`, and `shell` when those tools are enabled for the agent |
| `[]` (empty list)   | Explicitly disable sandbox proxying for this agent                                                                                                        |
| `["shell", "file"]` | Proxy exactly these tools for this agent                                                                                                                  |

Agent-level `worker_tools` overrides `defaults.worker_tools`. With `MINDROOM_WORKER_BACKEND=static_runner`, a sandbox proxy URL (`MINDROOM_SANDBOX_PROXY_URL`) must still be configured for proxying to take effect. With `MINDROOM_WORKER_BACKEND=kubernetes`, worker endpoints are resolved dynamically and `MINDROOM_SANDBOX_PROXY_URL` is not used.

## Worker Scope

`worker_tools` chooses which tools execute through the sandbox proxy. `worker_scope` chooses which proxied calls reuse the same worker runtime. Some credential-backed custom tools stay local even if they are listed in `worker_tools`. Currently that local-only set is `gmail`, `google_calendar`, `google_sheets`, and `homeassistant`.

You can set `worker_scope` per agent or in `defaults`:

```
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

| Value        | Behavior                                                                                       |
| ------------ | ---------------------------------------------------------------------------------------------- |
| `shared`     | One shared worker runtime per agent                                                            |
| `user`       | One worker runtime per requester, potentially reused across multiple agents for that requester |
| `user_agent` | One worker runtime per requester and agent                                                     |

If `worker_scope` is unset, proxied tools still use the sandbox runner, but the request stays unscoped and no scoped reusable worker runtime is selected. `worker_scope` also affects dashboard credential support and OpenAI-compatible agent eligibility. The implemented model is that `memory_file_path`, `context_files`, file-backed memory, mem0-backed state, sessions, and learning all resolve through the same canonical agent state root regardless of worker scope. The dashboard credential UI only supports unscoped agents and agents with `worker_scope=shared`. Agents using `user` or `user_agent` must treat credentials as runtime-owned worker state. For filesystem-capable tools, `user` is not an agent-level filesystem isolation boundary. It is a runtime reuse mode. It creates one persistent runtime per requester, and multiple agents may run inside that runtime. Those agents may access each other's mounted files inside that runtime. Use `user_agent` if you need the clearest per-agent filesystem isolation.

## Without configured worker routing

With `MINDROOM_WORKER_BACKEND=static_runner` and no `MINDROOM_SANDBOX_PROXY_URL`, tool calls execute directly in the primary MindRoom runtime process. This is fine for development but not recommended for production deployments where agents run untrusted code. With `MINDROOM_WORKER_BACKEND=kubernetes`, worker-routed tool calls fail closed when the backend is misconfigured instead of silently running locally.
