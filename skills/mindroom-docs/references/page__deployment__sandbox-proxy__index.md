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
1. The worker executes the tool against the agent's storage directory plus any worker-local caches and returns the result.
1. All other tools such as API tools or Matrix-bound tools execute in the primary MindRoom runtime as usual.

The worker runtime authenticates requests with a shared token (`MINDROOM_SANDBOX_PROXY_TOKEN`). For tools that need credentials, such as a shell tool that calls an authenticated API, the primary MindRoom runtime can create a short-lived **credential lease** that the worker consumes once. Credentials never become part of the normal tool arguments or the model prompt.

MindRoom currently ships two worker backend shapes:

- `static_runner`: one shared sandbox-runner process, usually a sidecar container or a local HTTP service.
- `kubernetes`: dedicated worker pods created on demand from the primary runtime, with one logical worker per worker key.

## Where Agent Data Lives

Each agent stores all its persistent data (context files, workspace files, memory, sessions, learning) in one directory: `agents/<name>/`. This directory is shared across all worker scopes — switching `worker_scope` changes how tool runtimes are isolated, not where agent data lives. Worker runtimes may keep their own virtualenvs, caches, and scratch files, but those are not agent data. Multiple runtimes may access the same agent directory concurrently, so files and databases there must tolerate concurrent access.

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
      - ./mindroom_data:/app/mindroom_data:rw
      - sandbox-workspace:/app/workspace
    environment:
      - MINDROOM_SANDBOX_RUNNER_MODE=true
      - MINDROOM_SANDBOX_PROXY_TOKEN=${MINDROOM_SANDBOX_PROXY_TOKEN}
      - MINDROOM_CONFIG_PATH=/app/config.yaml
      - MINDROOM_STORAGE_PATH=/app/workspace/.mindroom

volumes:
  sandbox-workspace:
```

Add a shared volume or bind mount for agent storage that both the main process and the runner can access. Keep secrets and other main-process-only files on separate mounts that are not exposed to the runner.

> [!IMPORTANT] The `sandbox-workspace` Docker volume is created as root by default. The runner runs as UID 1000, so you must fix ownership after first creating the volume: `bash docker run --rm -v sandbox-workspace:/workspace busybox chown -R 1000:1000 /workspace` Alternatively, omit the `user:` directive to run as root (less secure).

Key differences from the primary MindRoom runtime:

- **No `env_file`** — runner has no API keys, no Matrix credentials
- **Shared agent data** — the runner reads and writes the same agent storage directories used by the main process
- **Scratch workspace** — a dedicated volume for worker-local files (caches, virtualenvs)
- **`MINDROOM_STORAGE_PATH`** — pointed at a writable location inside the scratch workspace for tool registry and cache files

> [!WARNING] **Filesystem isolation depends on the worker backend.** Shared-runner and local backends expose all agent storage to the runner process — there is no per-agent filesystem boundary. Kubernetes dedicated workers restrict mounts so each runtime only sees its own agent's directory (for `shared`, `user_agent`, and unscoped modes). The `user` scope is intentionally broader: it shares one runtime across multiple agents per user, so agents in that runtime can see each other's files. Use `user_agent` for per-agent filesystem isolation.

### Kubernetes shared sidecar (`workerBackend: static_runner`)

In Kubernetes the shared runner can still run as a second container in the same pod, sharing `localhost` networking. This is the `workerBackend: static_runner` Helm mode. See `cluster/k8s/instance/templates/deployment-mindroom.yaml` for the full manifest. The sidecar gets:

- An `emptyDir` volume for worker-local scratch files and caches.
- Access to the same shared storage that holds agent data directories.
- Read-only access to config for plugin tool registration.
- No access to the primary secrets volume.

### Kubernetes dedicated workers (`workerBackend: kubernetes`)

In dedicated-worker mode the primary MindRoom runtime creates worker Deployments and Services on demand. Each worker pod runs the sandbox-runner app and is addressed through an internal cluster Service. Each dedicated worker needs access to its agent's storage directory. Worker-local files (caches, virtualenvs, metadata) are kept separate per worker. When a worker is idle, its Deployment scales to zero, but agent data and worker caches are preserved.

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

- `storageAccessMode` should be `ReadWriteMany` because multiple dedicated workers may need concurrent access to the same agent storage.
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

| Variable                                        | Description                                                                                                                                                           | Default                                       |
| ----------------------------------------------- | --------------------------------------------------------------------------------------------------------------------------------------------------------------------- | --------------------------------------------- |
| `MINDROOM_WORKER_BACKEND`                       | Worker backend name: `static_runner` or `kubernetes`                                                                                                                  | `static_runner`                               |
| `MINDROOM_SANDBOX_PROXY_URL`                    | URL of the shared sandbox runner when using `static_runner`                                                                                                           | *(none — proxy disabled for `static_runner`)* |
| `MINDROOM_SANDBOX_PROXY_TOKEN`                  | Shared auth token used by the worker runtime                                                                                                                          | *(required for worker-routed execution)*      |
| `MINDROOM_SANDBOX_EXECUTION_MODE`               | `selective`, `all`, `off`                                                                                                                                             | *(unset — uses proxy tools list)*             |
| `MINDROOM_SANDBOX_PROXY_TOOLS`                  | Comma-separated tool names to proxy                                                                                                                                   | `*` (all, unless mode is `selective`)         |
| `MINDROOM_SANDBOX_PROXY_TIMEOUT_SECONDS`        | HTTP timeout for proxy calls                                                                                                                                          | `120`                                         |
| `MINDROOM_ATTACHMENT_INLINE_SAVE_MAX_BYTES`     | Maximum attachment bytes the primary runtime will inline when saving context attachments into a worker workspace with `get_attachment(..., mindroom_output_path=...)` | `16777216` (16 MiB)                           |
| `MINDROOM_SANDBOX_CREDENTIAL_LEASE_TTL_SECONDS` | Credential lease lifetime                                                                                                                                             | `60`                                          |
| `MINDROOM_SANDBOX_CREDENTIAL_POLICY_JSON`       | JSON mapping tool selectors to credential services                                                                                                                    | `{}`                                          |

When `MINDROOM_WORKER_BACKEND=kubernetes`, the primary runtime resolves worker endpoints through the Kubernetes backend and does not use `MINDROOM_SANDBOX_PROXY_URL`. The Helm chart sets the Kubernetes backend environment variables automatically. If you deploy that mode without Helm, see [Kubernetes Deployment](https://docs.mindroom.chat/deployment/kubernetes/index.md) and `src/mindroom/workers/backends/kubernetes_config.py` for the required environment surface.

### Sandbox runner

| Variable                                             | Description                                                                                          | Default                                                      |
| ---------------------------------------------------- | ---------------------------------------------------------------------------------------------------- | ------------------------------------------------------------ |
| `MINDROOM_SANDBOX_RUNNER_PORT`                       | Port the sandbox runner listens on                                                                   | `8766`                                                       |
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

## Shell env and PATH

When `shell` runs through the sandbox proxy, it receives the stricter sandbox runtime env rather than the main process's ordinary committed runtime `.env`. Additional env is not forwarded implicitly: configure `extra_env_passthrough` with exact names or glob patterns for exported process env variables you want shell execution to inherit. `extra_env_passthrough` matches exported process env, not config-adjacent `.env` entries. To prevent the runner from leaking its own control-plane credentials to tools, shell passthrough drops names in a small explicit denylist (`MINDROOM_API_KEY`, `MINDROOM_LOCAL_CLIENT_SECRET`, `MINDROOM_SANDBOX_PROXY_TOKEN`, `MINDROOM_SANDBOX_STARTUP_MANIFEST_PATH`) and any name starting with `MINDROOM_SANDBOX_`. Everything else that matches your configured names or globs passes through, including service tokens and provider credentials. If you don't want a value to reach shell commands, don't match it with `extra_env_passthrough`.

If proxied shell commands need extra PATH entries such as wrapper directories, configure `shell_path_prepend`. This prepends the configured entries ahead of the runtime PATH while preserving the existing PATH order and removing duplicates. That keeps PATH handling deployment-specific instead of baking host-specific directories into the shell tool itself.

Shell commands that exceed their timeout return a background handle. Use `check_shell_command(handle)` to poll and `kill_shell_command(handle)` to stop the process. These handles are process-local to the sandbox runner: they survive multiple requests to the same runner process, but not runner restarts. To make that work, shell background-handle requests stay owned by the long-lived runner process even when `MINDROOM_SANDBOX_RUNNER_EXECUTION_MODE=subprocess`.

## Workspace env hook (`.mindroom/worker-env.sh`)

Agents can drop a shell script at `<workspace>/.mindroom/worker-env.sh` to set custom env for worker-routed tool calls without changing config or redeploying.

The runner sources this script with `bash` before each worker-routed `shell` or `python` request, then merges its exported env into the tool's execution environment.

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

`.mindroom/worker-env.sh` is sourced by bash that inherits the runner's process env, which contains tokens the runner needs to function (sandbox proxy auth, etc.). To prevent the runner from leaking its own control-plane credentials to tools, the overlay drops names in a small explicit denylist (`MINDROOM_API_KEY`, `MINDROOM_LOCAL_CLIENT_SECRET`, `MINDROOM_SANDBOX_PROXY_TOKEN`, `MINDROOM_SANDBOX_STARTUP_MANIFEST_PATH`) and any name starting with `MINDROOM_SANDBOX_`. Bash bookkeeping vars (`PWD`, `OLDPWD`, `SHLVL`, `_`, `PIPESTATUS`) are also dropped because they're noise, not values the script meant to export. Everything else passes through, including service tokens and provider credentials you intentionally export from the hook. If you don't want a value to reach tools, don't export it.

**Limits and failure handling:**

- Script ≤ 64 KiB; stdout and stderr capture each ≤ 256 KiB; total overlay ≤ 128 KiB; per-value ≤ 32 KiB.
- Hook execution times out after 10 seconds.
- Symlinks that escape the workspace are rejected.
- Any failure (non-zero exit, timeout, escape, missing `bash`) returns the tool call as `ok: false` with `failure_kind: "tool"` and an error mentioning `.mindroom/worker-env.sh`.
- Hook failures do not poison the worker; only the requesting tool call fails.

This hook works identically for the static sidecar and dedicated Kubernetes worker backends because it runs inside the sandbox runner per request. It is not a true container startup hook — it does not change pod templates, recreate Deployments, or alter Helm values. For an example, see `docs/tools/execution-and-coding.md`.

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
- With `workerBackend: static_runner`, the Kubernetes sidecar uses `emptyDir` scratch space and shares access to the same agent storage directories as the main process.
- With `workerBackend: kubernetes`, dedicated workers for `shared`, `user_agent`, and unscoped execution only mount their own agent's directory plus their worker scratch space. `user` mode intentionally mounts the broader `agents/` tree since it shares one runtime across agents.
- The primary MindRoom runtime does not mount the sandbox-runner router, so `/api/sandbox-runner/` exists only in runner or dedicated worker processes.

### Sandbox-runner API endpoints

These endpoints are served by the sandbox-runner process, not the primary MindRoom runtime. All requests require the `MINDROOM_SANDBOX_PROXY_TOKEN` as a Bearer token.

| Method | Endpoint                              | Description                                                     |
| ------ | ------------------------------------- | --------------------------------------------------------------- |
| POST   | `/api/sandbox-runner/leases`          | Create a one-time credential lease for an upcoming tool call    |
| POST   | `/api/sandbox-runner/execute`         | Execute a tool call with optional credential override via lease |
| GET    | `/api/sandbox-runner/workers`         | List known workers with lifecycle metadata                      |
| POST   | `/api/sandbox-runner/workers/cleanup` | Mark idle workers for cleanup without deleting persisted state  |

Credential leases are single-use: once consumed by an `/execute` call, the lease cannot be replayed.

## Per-agent configuration

MindRoom owns the default local-versus-worker routing policy. You can override which tools are routed through the sandbox proxy per agent (or set a default for all agents) in `config.yaml`.

Per-agent tool config overrides (inline `shell: {extra_env_passthrough: "DAWARICH_*"}` syntax in agent `tools` lists) are threaded through the sandbox proxy so workers receive the merged overrides alongside credentials and runtime overrides. See [Per-Agent Tool Configuration](https://docs.mindroom.chat/configuration/agents/#per-agent-tool-configuration) for the full syntax.

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

`worker_tools` controls which tools run in the sandbox proxy. `worker_scope` controls how those sandbox runtimes are shared between calls. Some credential-backed tools always stay local regardless of `worker_tools`: `gmail`, `google_calendar`, `google_sheets`, and `homeassistant`. Additionally, `google` and `spotify` are shared-only integrations that require `worker_scope` unset or `shared` but can still be proxied through the sandbox.

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

| Value        | Behavior                                               |
| ------------ | ------------------------------------------------------ |
| `shared`     | One runtime per agent, shared by all users             |
| `user`       | One runtime per user, shared across that user's agents |
| `user_agent` | One runtime per user+agent pair                        |

If `worker_scope` is unset, proxied tools still run in the sandbox, but each call gets a fresh runtime instead of a persistent one.

**Important notes:**

- `worker_scope` does **not** change where agent data is stored. All scopes read and write the same agent storage directory (`agents/<name>/`).
- The dashboard credential UI only works for unscoped agents and agents with `worker_scope=shared`. Agents using `user` or `user_agent` manage credentials through their worker runtime.
- `user` mode shares one runtime across multiple agents for a single user, so agents in that runtime can access each other's files. Use `user_agent` for per-agent isolation.

## Without configured worker routing

With `MINDROOM_WORKER_BACKEND=static_runner` and no `MINDROOM_SANDBOX_PROXY_URL`, tool calls execute directly in the primary MindRoom runtime process. This is fine for development but not recommended for production deployments where agents run untrusted code. With `MINDROOM_WORKER_BACKEND=kubernetes`, worker-routed tool calls fail closed when the backend is misconfigured instead of silently running locally.
