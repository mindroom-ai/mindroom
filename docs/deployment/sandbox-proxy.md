---
icon: lucide/shield
---

# Sandbox Proxy Isolation

When agents have code-execution tools (`shell`, `file`, `python`), they can read and modify anything on the filesystem — config files, credentials, application code. The **sandbox proxy** isolates these tools by forwarding their calls to a separate process (the **sandbox runner**) that has no access to secrets or sensitive data.

## How it works

```
┌──────────────────────────┐         HTTP          ┌──────────────────────────┐
│ Primary MindRoom runtime │  ── tool call ──▶     │ Sandbox runner           │
│ has secrets              │  ◀── result ───       │ no secrets               │
│ has credentials          │                       │ no credentials           │
│ has persistent data      │                       │ writable scratch only    │
└──────────────────────────┘                       └──────────────────────────┘
```

1. Agent invokes `shell.run_shell_command(...)` (or file/python tool)
2. Primary MindRoom runtime detects the tool is in the proxy list
3. Call is forwarded over HTTP to the sandbox runner
4. Runner executes the tool locally and returns the result
5. All other tools (API tools, search, etc.) execute in the primary MindRoom runtime as usual

The runner authenticates requests with a shared token (`MINDROOM_SANDBOX_PROXY_TOKEN`). For tools that need credentials (e.g., a shell tool that calls an authenticated API), the primary MindRoom runtime can create a short-lived **credential lease** that the runner consumes once — credentials never persist in the runner's memory.

## Deployment modes

### Docker Compose (sidecar container)

Add a `sandbox-runner` service alongside MindRoom. Both use the same image; the runner just has a different entrypoint and no access to `.env` or the data volume.

```yaml
services:
  mindroom:
    image: ghcr.io/mindroom-ai/mindroom:latest
    env_file: .env
    volumes:
      - ./config.yaml:/app/config.yaml:ro
      - ./mindroom_data:/app/mindroom_data
    environment:
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

### Kubernetes (pod sidecar)

In Kubernetes the runner runs as a second container in the same pod, sharing `localhost` networking. See `cluster/k8s/instance/templates/deployment-mindroom.yaml` for the full manifest. The runner gets:
- An `emptyDir` volume for scratch workspace
- Read-only access to config (for plugin tool registration)
- No access to the secrets volume

### Host machine + Docker sandbox container

Run MindRoom directly on the host while isolating code-execution tools in a Docker container:

```bash
# 1. Start the sandbox runner container
docker run -d \
  --name mindroom-sandbox-runner \
  -p 8766:8766 \
  -e MINDROOM_SANDBOX_RUNNER_MODE=true \
  -e MINDROOM_SANDBOX_PROXY_TOKEN=your-secret-token \
  -e MINDROOM_STORAGE_PATH=/app/workspace/.mindroom \
  ghcr.io/mindroom-ai/mindroom:latest \
  /app/run-sandbox-runner.sh

# 2. Start MindRoom on the host with proxy config
export MINDROOM_SANDBOX_PROXY_URL=http://localhost:8766
export MINDROOM_SANDBOX_PROXY_TOKEN=your-secret-token
export MINDROOM_SANDBOX_EXECUTION_MODE=selective
export MINDROOM_SANDBOX_PROXY_TOOLS=shell,file,python
mindroom run
```

Or add the proxy variables to your `.env` file:

```bash
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

## Environment variable reference

### Primary MindRoom runtime (proxy client)

| Variable | Description | Default |
|----------|-------------|---------|
| `MINDROOM_SANDBOX_PROXY_URL` | URL of the sandbox runner | _(none — proxy disabled)_ |
| `MINDROOM_SANDBOX_PROXY_TOKEN` | Shared auth token | _(required when proxy URL is set)_ |
| `MINDROOM_SANDBOX_EXECUTION_MODE` | `selective`, `all`, `off` | _(unset — uses proxy tools list)_ |
| `MINDROOM_SANDBOX_PROXY_TOOLS` | Comma-separated tool names to proxy | `*` (all, unless mode is `selective`) |
| `MINDROOM_SANDBOX_PROXY_TIMEOUT_SECONDS` | HTTP timeout for proxy calls | `120` |
| `MINDROOM_SANDBOX_CREDENTIAL_LEASE_TTL_SECONDS` | Credential lease lifetime | `60` |
| `MINDROOM_SANDBOX_CREDENTIAL_POLICY_JSON` | JSON mapping tool selectors to credential services | `{}` |

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

- The sandbox runner **never has** API keys, Matrix credentials, or access to `mindroom_data/`
- The shared token authenticates all proxy traffic — use a strong random value
- Credential leases are single-use by default and expire after 60 seconds
- The runner's `securityContext` drops all capabilities and disables privilege escalation
- In Kubernetes, the runner uses `emptyDir` for scratch space — no persistent state
- The primary MindRoom runtime **does not** mount the sandbox runner router — the `/api/sandbox-runner/` endpoints exist only in the runner process

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

Agent-level `worker_tools` overrides `defaults.worker_tools`. A sandbox proxy URL (`MINDROOM_SANDBOX_PROXY_URL`) must still be configured for any proxying to take effect.

## Worker Scope

`worker_tools` chooses which tools execute through the sandbox proxy.
`worker_scope` chooses which proxied calls share the same worker-owned storage root.
Some credential-backed custom tools stay local even if they are listed in `worker_tools`.
Currently that local-only set is `gmail`, `google_calendar`, `google_sheets`, and `homeassistant`.

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
| `shared` | One shared worker state per agent |
| `user` | One worker state per requester |
| `user_agent` | One worker state per requester and agent |
| `room_thread` | One worker state per thread, or per room when no thread exists |

If `worker_scope` is unset, proxied tools still use the sandbox runner, but the request stays unscoped and no worker-specific storage root is selected.
`worker_scope` also affects dashboard credential support and OpenAI-compatible agent eligibility.
The dashboard credential UI only supports unscoped agents and agents with `worker_scope=shared`.
Agents using `user`, `user_agent`, or `room_thread` must treat credentials as runtime-owned worker state.

## Without sandbox proxy

When no `MINDROOM_SANDBOX_PROXY_URL` is set, all tools execute directly in the primary MindRoom runtime process. This is fine for development but not recommended for production deployments where agents run untrusted code.
