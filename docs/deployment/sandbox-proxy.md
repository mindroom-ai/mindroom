---
icon: lucide/shield
---

# Sandbox Proxy Isolation

Code-execution tools such as `shell`, `file`, and `python` can read and modify anything their process can reach, including config files, credentials, and application code.
The **sandbox proxy** runs these tools in a separate worker runtime that has none of the primary runtime's secrets.
Use it whenever agents may run untrusted code; without it, the operator trusts every agent with code-execution tools completely.
This page covers the worker backends and how to deploy them, which tools run in workers, worker scopes and isolation, the shell environment inside workers, credential leases, brokered egress, and the environment variable reference.

## How it works

When an agent calls a worker-routed tool, the primary runtime forwards the call to the worker selected by the backend and the agent's [worker scope](#worker-scopes), and the worker returns the result.
All other tools, such as API and Matrix tools, run in the primary runtime, so one agent can use both.
Each agent's system prompt includes a short **Tool Execution Environment** section that lists which of its tools run locally and which run in a worker.

Select the worker backend with `MINDROOM_WORKER_BACKEND`:

| Backend | What it runs | Isolation between agents |
|---------|--------------|--------------------------|
| `static_runner` | One shared sandbox-runner process, usually a sidecar container or a local HTTP service | None: all calls share one process and filesystem |
| `docker` | One dedicated worker container per worker key, created on demand by the primary runtime | Each worker sees only the workspaces of its worker key |
| `kubernetes` | One dedicated worker pod per worker key, created on demand by the primary runtime | Each worker sees only the workspaces of its worker key |

Requests to workers are authenticated with `MINDROOM_SANDBOX_PROXY_TOKEN`.
Docker and Kubernetes workers each get their own token derived from it, so a compromised worker token cannot be used against another worker.
Credentials reach workers only through short-lived [credential leases](#credential-leases), never through tool arguments or the model prompt.

## Deployment modes

<a id="sandbox-proxy-isolation_1"></a>

### Docker Compose (`static_runner`)

Add a `sandbox-runner` service that uses the same image as MindRoom, with a different entrypoint and no access to `.env` or the primary data volume.
This shared runner gives tools scratch storage only and cannot see the agent workspaces under `agents/<agent>/workspace`, so use [dedicated Docker workers](#host-machine-dedicated-docker-workers-mindroom_worker_backenddocker) when file or shell work must persist in the agent workspace.

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
      - MINDROOM_SANDBOX_PROXY_URL=http://sandbox-relay:8766
      - MINDROOM_SANDBOX_PROXY_TOKEN=${MINDROOM_SANDBOX_PROXY_TOKEN}
      - MINDROOM_SANDBOX_EXECUTION_MODE=selective
      - MINDROOM_SANDBOX_PROXY_TOOLS=shell,file,python
    networks:
      - mindroom-network

  sandbox-runner:
    image: ghcr.io/mindroom-ai/mindroom:latest
    command: ["/app/run-sandbox-runner.sh"]
    user: "1000:1000"
    volumes:
      - sandbox-workspace:/app/workspace
    environment:
      - MINDROOM_SANDBOX_RUNNER_MODE=true
      - MINDROOM_SANDBOX_PROXY_TOKEN=${MINDROOM_SANDBOX_PROXY_TOKEN}
      - MINDROOM_STORAGE_PATH=/app/workspace/.mindroom
    networks:
      - sandbox-network

  # Forwards only the runner port, so the runner shares no network with MindRoom.
  sandbox-relay:
    image: busybox:1.36
    user: "65534:65534"
    cap_drop:
      - ALL
    sysctls:
      net.ipv4.ip_forward: 0
    command: ["tcpsvd", "-c", "256", "-C", "128", "0.0.0.0", "8766", "nc", "sandbox-runner", "8766"]
    networks:
      - mindroom-network
      - sandbox-network

volumes:
  sandbox-workspace:

networks:
  mindroom-network:
  sandbox-network:
```

Keep these properties when you adapt the example:

- Do not give the runner an `env_file` or mount the `mindroom_data` tree, which holds API keys, credentials, Matrix encryption keys, sessions, and logs.
- Do not attach the runner to a network shared with MindRoom, its homeserver, or its databases, because tool code could then call those services directly.
- Keep the per-address connection cap (`-C`) on the relay so tool code cannot use up every relay slot MindRoom needs, and keep IP forwarding disabled on the relay.
- Set `MINDROOM_API_KEY` on MindRoom, because the runner keeps outbound access and can still reach MindRoom through published host ports or its public URL.
- Point `MINDROOM_STORAGE_PATH` at a writable location inside the scratch volume.

> [!IMPORTANT]
> The `sandbox-workspace` Docker volume is created as root by default.
> The runner runs as UID 1000, so fix ownership after first creating the volume:
> ```bash
> docker compose run --rm --user root --entrypoint chown sandbox-runner -R 1000:1000 /app/workspace
> ```
> Alternatively, omit the `user:` directive to run as root (less secure).

### Host machine + Docker sandbox container

Run MindRoom directly on the host while code-execution tools run in one shared Docker container:

```bash
# 1. Start the sandbox runner container
export MINDROOM_SANDBOX_PROXY_TOKEN="$(openssl rand -hex 32)"
docker run -d \
  --name mindroom-sandbox-runner \
  -p 127.0.0.1:8766:8766 \
  -e MINDROOM_WORKER_BACKEND=static_runner \
  -e MINDROOM_SANDBOX_RUNNER_MODE=true \
  -e MINDROOM_SANDBOX_PROXY_TOKEN="$MINDROOM_SANDBOX_PROXY_TOKEN" \
  -e MINDROOM_STORAGE_PATH=/app/workspace/.mindroom \
  ghcr.io/mindroom-ai/mindroom:latest \
  /app/run-sandbox-runner.sh

# 2. Start MindRoom on the host with proxy config
export MINDROOM_WORKER_BACKEND=static_runner
export MINDROOM_SANDBOX_PROXY_URL=http://localhost:8766
export MINDROOM_SANDBOX_EXECUTION_MODE=selective
export MINDROOM_SANDBOX_PROXY_TOOLS=shell,file,python
mindroom run
```

You can put the same `MINDROOM_*` variables in your `.env` file instead of exporting them.

The runner container needs no config file.
Do not mount your `config.yaml` or its `.env` into it, because tool code there can read everything the container can.
Install proxied plugin tools in the runner image, or mount only the plugin directory at the path the config's plugin entry resolves to.

<a id="optional-docker-worker-isolation"></a>

### Host machine + dedicated Docker workers (`MINDROOM_WORKER_BACKEND=docker`)

Use this to run the primary runtime on the host, including with `uvx mindroom run`, and give each worker key its own persistent Docker container, without Kubernetes.
A container is reused until its launch configuration changes.
Idle containers keep running unless `MINDROOM_WORKER_CLEANUP_INTERVAL_SECONDS` is set to a positive interval, which then stops containers idle longer than `MINDROOM_DOCKER_WORKER_IDLE_TIMEOUT_SECONDS`.

MindRoom installs the optional `docker` extra the first time this backend is used.
If you disable auto-install with `MINDROOM_NO_AUTO_INSTALL_TOOLS=1`, install it with `uv sync --extra docker` in a source checkout or `pip install 'mindroom[docker]'`.

Use the published image tag that matches your MindRoom version, or build a worker image from the checkout MindRoom runs from:

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

Then route tools into workers and choose a [worker scope](#worker-scopes):

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

To verify, ask two agents to run `hostname`, then list the worker containers:

```bash
docker ps --format '{{.Names}}\t{{.ID}}' | grep '^mindroom-worker'
```

Each worker gets a read-only copy of the [live config snapshot](#live-config-snapshots) built from `MINDROOM_DOCKER_WORKER_HOST_CONFIG_PATH`, together with the config-relative plugins and knowledge it needs.
Agent-scoped workers get only their agent and its assigned knowledge bases, while `worker_scope: user` workers get every agent that shares them.
Workers never see `~/.mindroom` or the config-adjacent `.env`.
The called tool's own inline overrides still reach the worker, but if it needs a secret from `.env` or from elsewhere in `config.yaml`, provide it through a [credential lease](#credential-leases), [environment passthrough](#shell-environment-and-path), or `MINDROOM_DOCKER_WORKER_ENV_JSON` instead.
Because worker code can reach the host network, `mindroom run` adds a generated `MINDROOM_API_KEY` to `.env` when a dedicated worker backend is configured and the dashboard has no credential; an explicitly empty `MINDROOM_API_KEY=` keeps open access.

### Kubernetes shared sidecar (`workerBackend: static_runner`)

In Kubernetes the shared runner runs as a second container in the MindRoom pod and is reached over `localhost`.
This is the default `static_runner` mode of both Helm charts; see [Kubernetes Deployment](kubernetes.md#shared-sidecar-mode) for the chart values and `cluster/k8s/instance/templates/deployment-mindroom.yaml` for the manifest.

The sidecar mounts only these parts of the storage PVC:

- The `agents` and `private_instances` directories, read-write at their usual paths, so agent workspaces persist and stay shared with the primary runtime.
- Its own `sandbox-runner` directory as its storage root, for worker-local files, virtualenvs, and caches.

It gets no config file and resolves agents from the [live config snapshot](#live-config-snapshots), so agents added or changed after the pod started work immediately.
It cannot read the credential store, Matrix encryption keys and access tokens, or other primary state, and it never receives the credentials-encryption key.
The runtime chart rejects a file-sourced config inside `agents`, `private_instances`, or `sandbox-runner`, because the sidecar can write those directories.

> [!WARNING]
> The sidecar protects the primary runtime's secrets and state, but it is not an isolation boundary between agents.
> All proxied tool calls share one runner process and user, and the sidecar sees every agent's state directory, including workspaces, sessions, learning data, and memory.
> The sidecar also shares the pod network namespace, so the primary API must require authentication that tool code cannot forge, such as platform authentication, `MINDROOM_API_KEY`, or trusted-upstream authentication with `requireJwt`; header-only trusted-upstream authentication is forgeable from the sidecar.
> Without Supabase authentication, both charts give the primary a generated `MINDROOM_API_KEY`.
> Use dedicated Kubernetes workers through the runtime chart when per-agent filesystem and credential isolation are required.

### Kubernetes dedicated workers (`workers.backend: kubernetes`)

In this mode the primary runtime creates a worker Deployment and Service per worker key on demand, and scales idle workers to zero while keeping agent data and worker caches.
See [Kubernetes Deployment](kubernetes.md#dedicated-worker-mode) for chart support, Helm values, storage access modes, RBAC, and NetworkPolicy.

Dedicated workers mount no config and resolve agents from the [live config snapshot](#live-config-snapshots).
They never receive the credentials-encryption key, so tool settings reach them only through [credential leases](#credential-leases).
Worker pods can reach the primary API over the pod network, so the runtime chart gives the primary a generated `MINDROOM_API_KEY` that workers never receive, unless the explicit opt-out is configured.

### Keeping worker versions matched

Run the same MindRoom release in the primary runtime and in every runner and worker image.
MindRoom refuses dedicated Docker worker images whose protocol does not match the primary runtime; it pulls the configured image once and recreates the worker before failing.
If the pull fails, as it does for a locally built tag such as `mindroom:dev`, or the image is still incompatible, rebuild the image from the checkout the primary runs from or select the published tag that matches your MindRoom version.
Plain `uvx mindroom run` runs the published PyPI release, so when testing unreleased code, run MindRoom from the checkout you build the image from (`uv run mindroom run` from the repo root, or `uvx --from /path/to/mindroom mindroom run`).
When upgrading from older releases, see [Workspace-only worker mounts](upgrades.md#workspace-only-worker-mounts), [Config-free runners and workers](upgrades.md#config-free-runners-and-workers), and [Requester-scoped worker keys](upgrades.md#requester-scoped-worker-keys).

## Live config snapshots

Runners never receive the primary's config file, the directory around it, its `.env`, or a ConfigMap holding it.
With the `static_runner` and `kubernetes` backends the primary sends the part of its live config that runners need with every request, so config changes reach runners without restarting them.
Docker workers get the same fields as a projected read-only config file, as described under [dedicated Docker workers](#host-machine-dedicated-docker-workers-mindroom_worker_backenddocker).

The snapshot holds only what runners need to resolve the agent, its tools, workspace, `file_access`, `worker_scope`, knowledge paths, and plugin paths.
Everything else stays in the primary, including models, MCP servers, plugin settings, memory provider settings, Git sources, instructions, rooms, teams, access policy, and the inline overrides of every tool other than the one being called.
A worker-routed call carries the called tool's own inline overrides, such as `shell: {extra_env_passthrough: "DAWARICH_*"}` in an agent's `tools` list (see [Per-Agent Tool Configuration](../tools/index.md#per-agent-tool-configuration)), and saved tool settings reach the runner as [credential leases](#credential-leases).

Runners load plugin tools from the snapshot's plugin paths and skip plugins they cannot find, so plugin code must exist in the runner's filesystem.
The charts mount no config directory, so plugin directories beside the primary's config are invisible to the `static_runner` sidecar and Kubernetes workers; install such plugins as Python packages in the runner image.

## Worker Routing

`worker_tools` decides which tools run in workers; the field itself is described in [Agent Configuration](../configuration/agents.md).
An agent's `worker_tools` list takes precedence, followed by `defaults.worker_tools`; an explicit empty list runs everything in the primary runtime.
When both are omitted, the [execution mode](#execution-modes) environment settings decide.

```yaml
defaults:
  worker_tools: [shell, file]        # route shell and file through workers for all agents

agents:
  code:
    tools: [file, shell, calculator]
    # inherits worker_tools from defaults

  research:
    tools: [duckduckgo, calculator]
    worker_tools: []                 # run everything in the primary runtime

  untrusted:
    tools: [shell, file, python]
    worker_tools: [shell, file, python]
```

<a id="worker-routed-execution"></a>

The tools that default to workers when no list is set are `browser_mcp`, `coding`, `docker`, `file`, `python`, and `shell`.
Any other registered tool can be listed in `worker_tools`, except tools that need the primary runtime or room context, which stay in the primary runtime even when listed.
These include the Matrix, scheduling, todo, attachment, and thread tools, `gmail` and the other Google tools, `github`, `atlassian`, `slack`, `spotify`, `homeassistant`, `reasoning`, `daytona`, `e2b`, `mem0`, `claude_agent`, `config_manager`, `oauth_connections`, and `agent_vault_access`, plus `browserbase`, `composio`, `duckdb`, `pandas`, `sql`, and `zep`, which keep a session, connection, or other state between calls.
The built-in `memory`, `delegate`, and `self_config` tools also always run in the primary runtime.
The `browser` tool chooses per call: `target: desktop` stays in the primary runtime, while `target: host` follows the worker routing.

### Execution modes

These environment settings apply only when both the agent's `worker_tools` and `defaults.worker_tools` are omitted.

| `MINDROOM_SANDBOX_EXECUTION_MODE` | Behavior |
|------|----------|
| `selective` / `sandbox_selective` | Route eligible tools listed in `MINDROOM_SANDBOX_PROXY_TOOLS`, or all eligible tools for `*`; an empty list routes nothing |
| `all` / `sandbox_all` | Route every eligible tool enabled for the agent |
| `off` / `local` / `disabled` | Run tools in the primary runtime even if a proxy URL or dedicated backend is configured |
| _(unset)_ | A non-empty `MINDROOM_SANDBOX_PROXY_TOOLS` controls routing; otherwise a configured static proxy URL routes all eligible tools, the `docker` and `kubernetes` backends route the tools that default to workers, and `static_runner` without a proxy URL runs everything locally |

Any other value is rejected with `Invalid MINDROOM_SANDBOX_EXECUTION_MODE`.
With `static_runner`, no `MINDROOM_SANDBOX_PROXY_URL`, and no setting that requests routing, tools run directly in the primary runtime.
That is fine for development but not for production deployments where agents run untrusted code.
When routing is requested but cannot be satisfied, tool calls fail instead of running locally: with `static_runner` this happens when no proxy URL is set, and with `docker` or `kubernetes` when the backend is misconfigured.

`MINDROOM_UNSAFE_ALLOW_LOCAL_EXECUTION_TOOLS=true` lets tools that default to workers run locally when `static_runner` routing is requested without a proxy URL.
It never bypasses dedicated Docker or Kubernetes workers or a configured proxy URL.
Do not set it in hosted or multi-tenant deployments.

## Worker scopes

`worker_scope` controls how worker runtimes are shared between calls; the field is described in [Agent Configuration](../configuration/agents.md).
It changes how tool runtimes are isolated, not where agent data lives.

| `worker_scope` | Worker runtime | What a dedicated Docker or Kubernetes worker mounts |
|----------------|----------------|------------------------------------------------------|
| _(unset)_ | Docker and Kubernetes: one persistent worker per agent; `static_runner`: no worker-specific storage | That agent's workspace |
| `shared` | One per agent, shared by all users | That agent's workspace |
| `user` | One per user, shared across that user's agents | The workspaces of every non-private agent on `worker_scope: user`, plus that user's own private workspaces of `private.per: user` agents |
| `user_agent` | One per user and agent pair | That agent's workspace |

Use `user_agent` when you need per-agent filesystem isolation.
Treat `user` as a shared workstation: it mounts every `worker_scope: user` agent's workspace, including files other requesters leave there, but never agents on other scopes.
A `user`-scope tool call whose `base_dir` points at an agent on another scope fails with HTTP 400 (`base_dir must stay inside a visible workspace or the worker root`).

### Filesystem isolation and agent data

Each non-private agent's data (context files, workspace, memory, sessions, learning) lives in `agents/<name>/` whatever its `worker_scope`, and every runtime for that agent reads and writes the same directory.
Private agents keep one state root per requester under `private_instances/<scope-key>/<agent>/`.
Worker runtimes also keep their own virtualenvs, caches, and scratch files, which are not agent data.

What a worker can see depends on the backend:

- **Local execution** shares the primary process filesystem.
- **The Kubernetes chart's `static_runner` sidecar** sees every agent's state directory, including the whole `private_instances` directory; a Compose or host-container runner sees only what you mount into it, so the examples above see only scratch storage.
- **Dedicated Docker and Kubernetes workers** mount only the workspaces listed in the [scope table](#worker-scopes), their own scratch space, and read-only assigned knowledge, never the sessions, memory, or learning data beside the workspaces.
  For private agents they mount only the private root (`private.root`), never the state root around it.

When an agent has a workspace (the `file` memory backend or `private`), `shell`, `file`, `python`, and `coding` use it as their default working directory (`base_dir`); this is a convenience, not a boundary.

A missing or linked workspace is not mounted.
Assigned knowledge that is missing, reached through a link, or inside another agent's workspace, a private instance, or a worker root is not mounted either, and the worker sees an empty knowledge folder.
Kubernetes knowledge mounts are described in [Knowledge Source Visibility](kubernetes.md#knowledge-source-visibility).
Any change to a dedicated worker's launch configuration, mounts, environment, or tool and plugin config recreates the worker on its next use, which ends its tmux sessions, background shells, and computer sessions.
Adding, removing, or re-scoping a `worker_scope: user` agent changes every user worker's mounts, so those workers are recreated too.

### Dashboard credentials and worker scopes

The dashboard's generic credential forms work only for unscoped agents and agents with `worker_scope: shared`.
The Google Drive, Docs, Gmail, Calendar, Sheets, and Tasks OAuth providers are an exception: the dashboard can connect scoped `user` and `user_agent` credentials, while the tools still execute in the primary runtime.
GitHub managed OAuth credentials always use the requester's `user` scope, independently of the agent's `worker_scope`.
Saved tool settings are stored by the primary per agent for `shared`, per requester across agents for `user`, and per requester-agent pair for `user_agent`, and reach worker-routed calls through [credential leases](#credential-leases).
Tools without a scoped OAuth provider have no dashboard form for `user` and `user_agent` settings, so configure them in authored config (see [Per-Agent Tool Configuration](../tools/index.md#per-agent-tool-configuration)) or through shared settings granted with `defaults.worker_grantable_credentials`.
Only unscoped and `shared` agents are available through the [OpenAI-compatible API](../openai-api.md#model-selection).

### Shared-only integrations

`spotify` and `homeassistant` require `worker_scope` unset or `shared`, and always run in the primary runtime.
Configured MCP `mcp_<server_id>` tools work on every worker scope (see [MCP](../mcp.md#oauth-backed-remote-mcp)); [Where Connections Are Stored](../oauth-framework.md#where-connections-are-stored) lists which OAuth connection each scope uses.

## Shell environment and PATH

Worker-routed `shell` receives only a small non-secret system environment by default, such as `PATH`, `HOME`, `USER`, `TMPDIR`, locale, proxy, and certificate path variables.
Runtime `.env` values and provider API keys such as `OPENAI_API_KEY` and `ANTHROPIC_API_KEY` are not forwarded.
Worker-routed `python` receives only allowed runtime names.

To pass more variables to `shell`, set the shell tool's `extra_env_passthrough` to exact names or glob patterns of exported process variables; it does not match config-adjacent `.env` entries.
Everything that matches passes through, including service tokens and provider credentials, except MindRoom's own control variables such as credential seeds, Kubernetes worker backend settings, `MINDROOM_CREDENTIALS_ENCRYPTION_KEY`, and any name starting with `MINDROOM_SANDBOX_`.
Use `shell_path_prepend` to add directories, such as wrapper directories, ahead of the existing `PATH`.
Both options are documented with the [shell tool](../tools/execution-and-coding.md#shell).

### Workspace home contract

For worker-routed `shell` and `python` calls with a resolved workspace, MindRoom sets `HOME`, `MINDROOM_AGENT_WORKSPACE`, `XDG_CONFIG_HOME`, `XDG_DATA_HOME`, and `XDG_STATE_HOME` to the workspace or directories under it.
`XDG_CACHE_HOME`, `PIP_CACHE_DIR`, `UV_CACHE_DIR`, and `PYTHONPYCACHEPREFIX` point at the worker cache directory when a worker root exists, and `VIRTUAL_ENV` stays on the worker's environment.
Neither passthrough nor `.mindroom/worker-env.sh` can redirect these variables.
As a result `pwd`, `~`, `Path.home()`, attachment `mindroom_output_path` saves, and relative paths in the `file` and `coding` tools all refer to the same workspace.
For example, after `get_attachment("att_...", mindroom_output_path="incoming/file.txt")`, worker-routed shell can read both `incoming/file.txt` and `~/incoming/file.txt`.

### Workspace env hook (`.mindroom/worker-env.sh`)

Agents can set custom environment variables for worker-routed `shell` and `python` calls, such as `PATH` entries, package indexes, or npm cache locations, by writing a script to `.mindroom/worker-env.sh` at the root of the agent workspace, without changing config or redeploying.
It works the same with every worker backend.
For an example, see the [shell tool](../tools/execution-and-coding.md#shell).

The hook lives at:

- `agents/<agent>/workspace/.mindroom/worker-env.sh` for shared and unscoped agents.
- `private_instances/<scope>/<agent>/<private.root>/.mindroom/worker-env.sh` for private agents, where `private.root` defaults to `<agent>_data`.
- The current request's workspace for `worker_scope: user`, so one shared user runtime picks up each agent's own hook.

How it behaves:

- The runner sources the script with `bash` before each call, and edits take effect on the next call without restarts, config reloads, or Helm changes.
- Only exported variables apply: write `export FOO=bar`, because plain `FOO=bar`, aliases, functions, and `cd` do not carry over.
- The script starts from the prepared request environment, not the runner's full environment.
- Exported values pass through, including tokens you export on purpose, except MindRoom's control variables (the same ones dropped from [shell passthrough](#shell-environment-and-path)), bash bookkeeping variables such as `PWD` and `SHLVL`, the [workspace home](#workspace-home-contract) variables, and values larger than 32 KiB.

Limits and errors:

- The script may be at most 64 KiB, each of stdout and stderr at most 256 KiB, and the exported overlay at most 128 KiB.
- The hook times out after 10 seconds.
- Symlinks that escape the workspace are rejected.
- Any failure, such as a non-zero exit, timeout, escape, or missing `bash`, fails only that tool call, with `ok: false`, `failure_kind: "tool"`, and an error mentioning `.mindroom/worker-env.sh`.

## Credential leases

A **credential lease** delivers saved tool settings and credentials to one worker-routed call as toolkit configuration.
Leases are single-use and expire after `MINDROOM_SANDBOX_CREDENTIAL_LEASE_TTL_SECONDS` (default 60, clamped to 1 to 3600).
Only fields that the receiving toolkit declares are applied.

Every worker-routed call leases the called tool's saved settings, because runners have no access to the primary's credential store or its encryption key:

- Scoped calls lease the settings saved for their scope; shared settings are leased only for services listed in `defaults.worker_grantable_credentials`.
- Unscoped calls lease the called tool's settings from the primary credential store, including the `openai` and `groq` provider keys, because those tools share their service names with the model providers; `defaults.worker_grantable_credentials` does not limit them.
- `defaults.worker_grantable_credentials` also decides which shared credentials are copied into dedicated workers.

`MINDROOM_SANDBOX_CREDENTIAL_POLICY_JSON` leases additional services: it maps tool names, `tool.function` selectors, or `*` to lists of credential service names.
Values in a dedicated worker's own credential store apply only inside that worker and only where no lease sets a value, so clear worker credential stores if older values there should stop applying.

Leases do not export API keys into shell environments, configure Git authentication, or install SSH keys.
For shell authentication, use [environment passthrough](#shell-environment-and-path), the [workspace env hook](#workspace-env-hook-mindroomworker-envsh), or [brokered egress](#brokered-worker-egress).

## Brokered worker egress

To let worker-routed `shell` and `python` call external APIs without ever receiving the real credential, route their egress through a proxy that injects the credential in transit.
This works at the network layer, so it covers URLs inside scripts, package CLIs, and subprocesses.
There are two supported shapes:

- **Per-worker Agent Vault egress** (Kubernetes backend), which gives each worker its own vault for per-user or per-agent isolation, described below.
- **A shared credential-injecting proxy** that you run, configured through `MINDROOM_KUBERNETES_WORKER_ENV_JSON`, the chart's `egressProxy` integration, or the worker proxy environment on other backends.
  The proxy holds the real credential and workers receive only its URL.

[Approved egress](approved-egress.md) and the `mindroom-egress-proxy` image restrict which hosts workers can reach but do not inject credentials; to combine them with Agent Vault, see [Agent Vault Chaining](approved-egress.md#agent-vault-chaining).

Do not put upstream API tokens in `extra_env_passthrough` or `.mindroom/worker-env.sh` unless you intend the worker process to receive them.

### Per-worker Agent Vault egress (Kubernetes backend)

Each dedicated worker gets its own vault, created if missing, and a fresh proxy-role Agent Vault token on one shared Agent Vault server.
Before enabling it, create the Secret named by `MINDROOM_KUBERNETES_AGENT_VAULT_BOOTSTRAP_SECRET_NAME` (chart: `workers.kubernetes.agentVault.bootstrapSecretName`, default `agent-vault-bootstrap`) with both `AGENT_VAULT_MASTER_PASSWORD` and `AGENT_VAULT_OWNER_PASSWORD`.
Create it in the worker namespace too when that differs from the release namespace.
`python` and `shell` egress then goes through the Agent Vault proxy with that token, so credentials stored in the worker's vault are injected in transit.

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

The proxy token exists only inside the worker pod and changes on every worker restart.
Tool code can read its own token, but a proxy-role token cannot read or decrypt credentials through the Agent Vault API and cannot reach another worker's vault.

For HTTPS, workers must trust the Agent Vault root CA.
Publish the CA from `agent-vault ca fetch` as a ConfigMap with key `ca.pem` and set `MINDROOM_KUBERNETES_AGENT_VAULT_WORKER_CA_CONFIGMAP_NAME` (chart: `workers.kubernetes.agentVault.workerCaConfigMapName`); `python` and `shell` then get `REQUESTS_CA_BUNDLE`, `CURL_CA_BUNDLE`, and `SSL_CERT_FILE` pointing at it.

When you also use [approved egress](approved-egress.md), Squid must be the first hop; see [Agent Vault Chaining](approved-egress.md#agent-vault-chaining).
Workers still need access to the Agent Vault API (`apiUrl`, usually port `14321`) to obtain their token.
The chart adds that rule when it manages the Agent Vault server; for an external server, add an egress rule for the API endpoint only, and do not add direct worker egress to the proxy port (`proxyUrl`, usually `14322`) when approved egress owns dynamic grants.
The chart-managed server's own NetworkPolicy is described under `workers.kubernetes.agentVault.server.networkPolicy` in `cluster/k8s/runtime/README.md`.

### Self-service vault access

The `agent_vault_access` tool lets a user ask their agent for a link to manage that agent's vault.
For `user` and `user_agent` scopes it grants the requester's Agent Vault account admin access to the worker's vault and returns the UI link.
For shared scopes it only returns the vault name and UI link (`"access": "operator_managed"`), which grants nothing by itself and tells the operator which vault backs the agent.

```bash
MINDROOM_AGENT_VAULT_ACCESS_API_URL=http://agent-vault:14321
MINDROOM_AGENT_VAULT_ACCESS_ADMIN_TOKEN=<owner or admin session/agent token>
MINDROOM_AGENT_VAULT_ACCESS_UI_BASE_URL=https://example.com/agent-vault
MINDROOM_AGENT_VAULT_ACCESS_EMAIL_DOMAIN=example.com
MINDROOM_AGENT_VAULT_ACCESS_VAULT_NAME_PREFIX=agent-vault  # must match workers.kubernetes.agentVault.vaultNamePrefix
```

Shared scopes need only the UI base URL and the vault name prefix; `user` and `user_agent` scopes also need the API URL, admin token, and email domain.
Instead of the inline admin token, `MINDROOM_AGENT_VAULT_ACCESS_ADMIN_TOKEN_FILE` can point at a file holding it; the file takes precedence and is re-read on every call, so rotating the Secret needs no restart.
When `MINDROOM_AGENT_VAULT_ACCESS_OWNER_EMAIL` names the Agent Vault owner account, self-service grants keep that account admin on the worker vault too.
The requester's account is `<Matrix localpart>@<EMAIL_DOMAIN>`, and only requesters on the runtime's own homeserver can self-grant.
The user must already have registered and verified that Agent Vault account.
This mapping controls only who may manage a vault in the UI, never which worker uses which vault.

## Stopping and background commands

Stopping a response also stops its in-flight worker calls, such as `run_shell_command` and `run_python_code`.
Tools that run inside the runner process itself, such as `file` under the runner's `inprocess` execution mode, and persistent browser actions finish before the stop takes effect.
Shell commands that exceed their timeout keep running in the background and survive across requests to the same runner, but a runner or worker restart stops them; see the [shell tool](../tools/execution-and-coding.md#shell) for handles.

## Browsers in workers

Dedicated Docker and Kubernetes workers with `worker_scope: shared`, `user`, or `user_agent` keep a headless browser session across calls, including open tabs and the browser profile, when Worker Computer is disabled.
Include `browser` in the agent's `worker_tools` to use it.
A change to the browser configuration or the prepared process environment closes the session before the next call.
The `static_runner` backend and unscoped workers start a fresh browser for each call.
An explicit `target: host` uses the worker even when `default_target: desktop` is set.

[Worker Computer](../tools/worker-computer.md) adds a visible headed browser and Chat viewer to dedicated Docker and Kubernetes workers; the `static_runner` backend does not support it.

## Worker tool results

Worker-routed tools can return images, audio, video, and files.
The worker downloads public HTTP(S) media URLs itself, following at most five redirects, and refuses local and metadata-service destinations; the primary runtime never reads a worker path or follows a media URL from the result.
Each result may contain at most eight media items, 10 MiB per item, and 20 MiB of media in total, plus at most 4,194,304 characters of text and 64 KiB of JSON metadata.
Unsupported resources or oversized results produce a tool error.

## Security considerations

- Workers never get the primary runtime's API key files, Matrix client state, credentials-encryption key, or orchestrator authority; they receive only the credentials leased under [Credential leases](#credential-leases).
- Use a strong random `MINDROOM_SANDBOX_PROXY_TOKEN`; runners accept requests and config snapshots only when authenticated with it or a token derived from it.
- Dedicated Docker and Kubernetes workers have a read-only root filesystem with a private writable `/tmp` of 1 GiB, so tool code cannot replace the runner's code; tool extras install into the worker's own virtualenv instead.
  On Docker, `/tmp` refuses writes past the limit; on Kubernetes, exceeding it evicts that worker pod.
  Shared `static_runner` sidecars and the Compose runner keep a writable image.
- Kubernetes worker containers drop all capabilities, disable privilege escalation, and use the pod's `RuntimeDefault` seccomp profile.
  Runtimes that block Chromium's namespace sandbox need the profile described in [Worker Computer](../tools/worker-computer.md#browser-and-container-sandboxing).
- Dedicated Docker workers can use the hardened `MINDROOM_DOCKER_WORKER_SECURITY_POLICY=computer`, which Worker Computer requires; see [Browser and container sandboxing](../tools/worker-computer.md#browser-and-container-sandboxing).

## Environment variable reference

### Primary MindRoom runtime (proxy client)

| Variable | Description | Default |
|----------|-------------|---------|
| `MINDROOM_WORKER_BACKEND` | Worker backend: `static_runner`, `docker`, or `kubernetes` | `static_runner` |
| `MINDROOM_SANDBOX_PROXY_URL` | URL of the shared sandbox runner for `static_runner`; unused by `docker` and `kubernetes` | _(none)_ |
| `MINDROOM_SANDBOX_PROXY_TOKEN` | Static-runner bearer token, and the secret from which Docker and Kubernetes derive per-worker tokens | _(required for worker routing)_ |
| `MINDROOM_SANDBOX_EXECUTION_MODE` | `selective`, `all`, or `off`; see [Execution modes](#execution-modes) | _(unset)_ |
| `MINDROOM_SANDBOX_PROXY_TOOLS` | Comma-separated tools to route when no `worker_tools` list is set | `*` for `all` or an unset mode with a proxy URL, otherwise empty |
| `MINDROOM_UNSAFE_ALLOW_LOCAL_EXECUTION_TOOLS` | Let worker-default tools run locally when `static_runner` routing is requested without a proxy URL | `false` |
| `MINDROOM_SANDBOX_PROXY_TIMEOUT_SECONDS` | HTTP timeout for proxy calls | `120` |
| `MINDROOM_ATTACHMENT_INLINE_SAVE_MAX_BYTES` | Largest attachment saved into a worker workspace with `get_attachment(..., mindroom_output_path=...)`, and largest Atlassian `confluence_download_attachment` download | `16777216` (16 MiB) |
| `MINDROOM_SANDBOX_CREDENTIAL_LEASE_TTL_SECONDS` | Credential lease lifetime | `60` |
| `MINDROOM_SANDBOX_CREDENTIAL_POLICY_JSON` | JSON mapping tool selectors to credential services | `{}` |
| `MINDROOM_WORKER_COMPUTER_ENABLED` | Enable the [Worker Computer](../tools/worker-computer.md) persistent browser and display | `false` |
| `MINDROOM_COMPUTER_ALLOWED_ORIGINS` | JSON list of Chat origins allowed to open worker computers | `[]` |

The Helm chart sets the Kubernetes backend variables.
To deploy that backend without Helm, see [Worker settings](kubernetes.md#worker-settings).

### Dedicated Docker worker backend

| Variable | Description | Default |
|----------|-------------|---------|
| `MINDROOM_DOCKER_WORKER_IMAGE` | Container image for dedicated Docker workers | _(required for `docker`)_ |
| `MINDROOM_DOCKER_WORKER_SECURITY_POLICY` | `runtime_default` or `computer`; Worker Computer requires `computer` | `runtime_default` |
| `MINDROOM_DOCKER_WORKER_PORT` | Sandbox-runner port inside the container | `8766` |
| `MINDROOM_DOCKER_WORKER_STORAGE_MOUNT_PATH` | Worker root mount path inside the container | `/app/worker` |
| `MINDROOM_DOCKER_WORKER_CONFIG_PATH` | Config path inside the container | `/app/config-host/config.yaml` |
| `MINDROOM_DOCKER_WORKER_HOST_CONFIG_PATH` | Host `config.yaml` from which each worker's projected config is built | Resolved `MINDROOM_CONFIG_PATH` when it exists |
| `MINDROOM_DOCKER_WORKER_IDLE_TIMEOUT_SECONDS` | Idle time before a worker container may be cleaned up | `1800` |
| `MINDROOM_DOCKER_WORKER_READY_TIMEOUT_SECONDS` | Maximum wait for a new worker to become ready | `60` |
| `MINDROOM_DOCKER_WORKER_NAME_PREFIX` | Prefix for worker container names | `mindroom-worker` |
| `MINDROOM_DOCKER_WORKER_PUBLISH_HOST` | Host interface for published worker ports | `127.0.0.1` |
| `MINDROOM_DOCKER_WORKER_ENDPOINT_HOST` | Hostname the primary runtime uses to reach published worker ports | Value of `MINDROOM_DOCKER_WORKER_PUBLISH_HOST` |
| `MINDROOM_DOCKER_WORKER_USER` | Container user, or empty for the image default | Current host uid:gid on POSIX, image default otherwise |
| `MINDROOM_DOCKER_WORKER_ENV_JSON` | JSON object of extra environment variables for each worker container | `{}` |
| `MINDROOM_DOCKER_WORKER_LABELS_JSON` | JSON object of extra Docker labels for each worker container | `{}` |

### Sandbox runner

| Variable | Description | Default |
|----------|-------------|---------|
| `MINDROOM_SANDBOX_RUNNER_PORT` | Port the runner listens on | `8766` |
| `MINDROOM_SANDBOX_RUNNER_MODE` | Set to `true` to run as a sandbox runner | `false` |
| `MINDROOM_SANDBOX_PROXY_TOKEN` | Runner bearer token; dedicated workers receive their derived token automatically | _(required)_ |
| `MINDROOM_SANDBOX_RUNNER_EXECUTION_MODE` | `inprocess`, `subprocess` (a fresh process per call), or `forkserver` (a fresh forked process per call from a warm template) | `inprocess`; dedicated workers use `forkserver` |
| `MINDROOM_SANDBOX_RUNNER_SUBPROCESS_TIMEOUT_SECONDS` | Per-call timeout for `subprocess` and `forkserver` execution | `120` |
| `MINDROOM_STORAGE_PATH` | Writable directory for the tool registry and worker-local caches, such as `/app/workspace/.mindroom`; startup fails if it is not writable | `mindroom_data` next to the config |
| `MINDROOM_CONFIG_PATH` | Config used for requests that carry no [live config snapshot](#live-config-snapshots) | _(optional)_ |
