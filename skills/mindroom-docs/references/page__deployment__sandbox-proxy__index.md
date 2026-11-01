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
      - MINDROOM_SANDBOX_PROXY_TOOLS=shell,file,python,coding
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
export MINDROOM_SANDBOX_PROXY_TOOLS=shell,file,python,coding
mindroom run
```

You can put the same `MINDROOM_*` variables in your `.env` file instead of exporting them.

The runner container needs no config file.
Do not mount your `config.yaml` or its `.env` into it, because tool code there can read everything the container can.
Install proxied plugin tools in the runner image, or mount only the plugin directory at the path the config's plugin entry resolves to.



### Host machine + dedicated Docker workers (`MINDROOM_WORKER_BACKEND=docker`)

Use this to run the primary runtime on the host, including with `uvx mindroom run`, and give each worker key its own persistent Docker container, without Kubernetes.
A container is reused until its launch configuration changes.
That covers only the config fields and files the worker sees, so edits to models, prompts, or other agents do not restart it, and an edit reaches workers once MindRoom has applied it.
A change to the registered tools, such as adding a plugin or an MCP server, restarts every Docker worker on its next use, which ends background commands running in it.
Idle containers keep running unless `MINDROOM_WORKER_CLEANUP_INTERVAL_SECONDS` is set to a positive interval, which then stops containers idle longer than `MINDROOM_DOCKER_WORKER_IDLE_TIMEOUT_SECONDS`.
When MindRoom shuts down, it removes its worker containers but keeps each worker's files, and the worker's next call starts a new container.

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

Each worker gets a read-only copy of the [live config snapshot](#live-config-snapshots) of the config MindRoom has applied, together with the config-relative plugins and knowledge it needs, resolved against the directory of `MINDROOM_DOCKER_WORKER_HOST_CONFIG_PATH`.
Agent-scoped workers get only their agent and its assigned knowledge bases, while `worker_scope: user` workers get every agent that shares them.
Workers never see `~/.mindroom` or the config-adjacent `.env`.
The called tool's own inline overrides still reach the worker, but if it needs a secret from `.env` or from elsewhere in `config.yaml`, provide it through a [credential lease](#credential-leases), [environment passthrough](#shell-environment-and-path), or `MINDROOM_DOCKER_WORKER_ENV_JSON` instead.
Because worker code can reach the host network, `mindroom run` adds a generated `MINDROOM_API_KEY` to `.env` when a dedicated worker backend is configured and the dashboard has no credential; an explicitly empty `MINDROOM_API_KEY=` keeps open access.

### Kubernetes shared sidecar (`workerBackend: static_runner`)

In Kubernetes the shared runner runs as a second container in the MindRoom pod and is reached over `localhost`.
This is the default `static_runner` mode of both Helm charts; see [Kubernetes Deployment](https://docs.mindroom.chat/deployment/kubernetes/#shared-sidecar-mode) for the chart values and `cluster/k8s/instance/templates/deployment-mindroom.yaml` for the manifest.

The sidecar mounts only these parts of the storage PVC:

- The `agents`, `private_instances`, and `shared_directories` directories, read-write at their usual paths, so agent workspaces and [shared directories](https://docs.mindroom.chat/tools/execution-and-coding/#shared_directories) persist and stay shared with the primary runtime.
- Its own `sandbox-runner` directory as its storage root, for worker-local files, virtualenvs, and caches.

It gets no config file and resolves agents from the [live config snapshot](#live-config-snapshots), so agents added or changed after the pod started work immediately.
It cannot read the credential store, Matrix encryption keys and access tokens, or other primary state, and it never receives the credentials-encryption key.
The runtime chart rejects a file-sourced config inside `agents`, `private_instances`, `shared_directories`, or `sandbox-runner`, because the sidecar can write those directories.

> [!WARNING]
> The sidecar protects the primary runtime's secrets and state, but it is not an isolation boundary between agents.
> All proxied tool calls share one runner process and user, and the sidecar sees every agent's state directory, including workspaces, sessions, and memory.
> The sidecar also shares the pod network namespace, so the primary API must require authentication that tool code cannot forge, such as platform authentication, `MINDROOM_API_KEY`, or trusted-upstream authentication.
> Without Supabase authentication, both charts give the primary a generated `MINDROOM_API_KEY`.
> Use dedicated Kubernetes workers through the runtime chart when per-agent filesystem and credential isolation are required.

### Kubernetes dedicated workers (`workers.backend: kubernetes`)

In this mode the primary runtime creates a worker Deployment and Service per worker key on demand, and scales idle workers to zero while keeping agent data and worker caches.
See [Kubernetes Deployment](https://docs.mindroom.chat/deployment/kubernetes/#dedicated-worker-mode) for chart support, Helm values, storage access modes, RBAC, and NetworkPolicy.

Dedicated workers mount no config and resolve agents from the [live config snapshot](#live-config-snapshots).
They never receive the credentials-encryption key, so tool settings reach them only through [credential leases](#credential-leases).
Worker pods can reach the primary API over the pod network, so the runtime chart gives the primary a generated (or `apiAuth.existingSecret`) `MINDROOM_API_KEY` that workers never receive, unless the explicit opt-out is configured.

### Keeping worker versions matched

Run the same MindRoom release in the primary runtime and in every runner and worker image.
MindRoom refuses dedicated Docker and Kubernetes worker images whose protocol does not match the primary runtime, and replaces the worker once before failing, so a worker left running under a mutable tag such as `:latest` from a release with another worker protocol is replaced on its next use.
A Docker worker from an older release is recreated from a fresh pull of the configured image, and one from a newer release without a pull, while a Kubernetes worker gets a new pod, which pulls the tag again only under the [worker image pull policy](https://docs.mindroom.chat/deployment/kubernetes/#worker-settings) `Always`.
If the pull fails, as it does for a locally built tag such as `mindroom:dev`, or the image is still incompatible, rebuild the image from the checkout the primary runs from or select the published tag that matches your MindRoom version.
A worker from a newer release is refused too, and its error reports a higher protocol than the expected one, for example a `:latest` worker that started after a release the primary does not run yet; upgrade the primary to that release, or pin one release tag for both.
A `static_runner` sandbox runner on another protocol is refused too, and every call routed to it fails until it runs the primary's release; MindRoom checks the runner when the primary first routes a call to it, so restart the primary after replacing a running runner with another release.
Restarting the primary picks up that release only when the primary runs the same mutable tag and pulls it on every start, as the runtime chart's default `latest` tag with pull policy `Always` does.
A restarted `uvx mindroom run` keeps its cached release, while `uvx mindroom@latest run` fetches the newest one.
Plain `uvx mindroom run` runs the published PyPI release, so when testing unreleased code, run MindRoom from the checkout you build the image from (`uv run mindroom run` from the repo root, or `uvx --from /path/to/mindroom mindroom run`).
When upgrading from older releases, see [Workspace-only worker mounts](https://docs.mindroom.chat/deployment/upgrades/#workspace-only-worker-mounts), [Config-free runners and workers](https://docs.mindroom.chat/deployment/upgrades/#config-free-runners-and-workers), and [Requester-scoped worker keys](https://docs.mindroom.chat/deployment/upgrades/#requester-scoped-worker-keys).

## Live config snapshots

Runners never receive the primary's config file, the directory around it, its `.env`, or a ConfigMap holding it.
With the `static_runner` and `kubernetes` backends the primary sends the part of its live config that runners need with every request, so config changes reach runners without restarting them.
Docker workers get the same fields as a projected read-only config file, as described under [dedicated Docker workers](#host-machine-dedicated-docker-workers-mindroom_worker_backenddocker).

The snapshot holds only what runners need to resolve the agent, its tools, workspace, `file_access`, `worker_scope`, knowledge paths, and plugin paths.
Everything else stays in the primary, including models, MCP servers, plugin settings, memory provider settings, Git sources, instructions, rooms, teams, access policy, and the inline overrides of every tool other than the one being called.
A worker-routed call carries the called tool's own inline overrides, such as `shell: {extra_env_passthrough: "DAWARICH_*"}` in an agent's `tools` list (see [Per-Agent Tool Configuration](https://docs.mindroom.chat/tools/#per-agent-tool-configuration)), and saved tool settings reach the runner as [credential leases](#credential-leases).

Runners load plugin tools from the snapshot's plugin paths and skip plugins they cannot find, so plugin code must exist in the runner's filesystem.
The charts mount no config directory, so plugin directories beside the primary's config are invisible to the `static_runner` sidecar and Kubernetes workers; install such plugins as Python packages in the runner image.

## Worker Routing

`worker_tools` decides which tools run in workers; the field itself is described in [Agent Configuration](https://docs.mindroom.chat/configuration/agents/).
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



The tools that default to workers when no list is set are `browser_mcp`, `coding`, `docker`, `file`, `python`, and `shell`.
Any other registered tool can be listed in `worker_tools`, except tools that need the primary runtime or room context, which stay in the primary runtime even when listed.
These include the Matrix, scheduling, todo, attachment, and thread tools, `gmail` and the other Google tools, `github`, `atlassian`, `slack`, `spotify`, `homeassistant`, `reasoning`, `daytona`, `e2b`, `mem0`, `claude_agent`, `config_manager`, and `oauth_connections`, plus `browserbase`, `composio`, `duckdb`, `pandas`, `sql`, and `zep`, which keep a session, connection, or other state between calls.
The built-in `memory`, `delegate`, `self_config`, `agent_manager`, and `contacts` tools and configured MCP `mcp_<server_id>` tools also always run in the primary runtime.
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

`worker_scope` controls how worker runtimes are shared between calls; the field is described in [Agent Configuration](https://docs.mindroom.chat/configuration/agents/).
It changes how tool runtimes are isolated, not where agent data lives.

| `worker_scope` | Worker runtime | What a dedicated Docker or Kubernetes worker mounts |
|----------------|----------------|------------------------------------------------------|
| _(unset)_ | Docker and Kubernetes: one persistent worker per agent; `static_runner`: no worker-specific storage | That agent's workspace |
| `shared` | One per agent, shared by all users | That agent's workspace |
| `user` | One per user, shared across that user's agents | The workspaces of every non-private agent on `worker_scope: user`, plus that user's own private workspaces of `private.per: user` agents |
| `user_agent` | One per user and agent pair | That agent's workspace |

Dedicated workers also mount the [shared directories](https://docs.mindroom.chat/tools/execution-and-coding/#shared_directories) of the agents they serve, read-write for directories those agents own and read-only for directories they joined.
Use `user_agent` when you need per-agent filesystem isolation.
Treat `user` as a shared workstation: it mounts every `worker_scope: user` agent's workspace and shared directories, including files other requesters leave there, but never agents on other scopes.
A `user`-scope tool call whose `base_dir` points at an agent on another scope fails with HTTP 400 (`base_dir must stay inside a visible workspace or the worker root`).

### Filesystem isolation and agent data

Each non-private agent's data (context files, workspace, memory, sessions) lives in `agents/<name>/` whatever its `worker_scope`, and every runtime for that agent reads and writes the same directory.
Private agents keep one state root per requester under `private_instances/<scope-key>/<agent>/`.
Worker runtimes also keep their own virtualenvs, caches, and scratch files, which are not agent data.

What a worker can see depends on the backend:

- **Local execution** shares the primary process filesystem.
- **The Kubernetes chart's `static_runner` sidecar** sees every agent's state directory, including the whole `private_instances` directory, and every shared directory; a Compose or host-container runner sees only what you mount into it, so the examples above see only scratch storage.
- **Dedicated Docker and Kubernetes workers** mount only the workspaces listed in the [scope table](#worker-scopes), the shared directories of the agents they serve, their own scratch space, and read-only assigned knowledge, never the sessions or memory beside the workspaces.
  For private agents they mount only the private root (`private.root`), never the state root around it.

`shell`, `file`, `python`, and `coding` use the agent's workspace as their default working directory (`base_dir`); this is a convenience, not a boundary.

A missing or linked workspace is not mounted.
Assigned knowledge that is missing, reached through a link, or inside another agent's workspace, a private instance, a shared directory the worker does not mount, or a worker root is not mounted either, and the worker sees an empty knowledge folder.
Kubernetes knowledge mounts are described in [Knowledge Source Visibility](https://docs.mindroom.chat/deployment/kubernetes/#knowledge-source-visibility).
Any change to a dedicated worker's launch configuration, mounts, environment, or tool and plugin config recreates the worker on its next use, which ends its tmux sessions, background shells, and computer sessions.
Adding, removing, or re-scoping a `worker_scope: user` agent changes every user worker's mounts, so those workers are recreated too.

### Dashboard credentials and worker scopes

The dashboard's generic credential forms work only for unscoped agents and agents with `worker_scope: shared`.
OAuth connections are an exception: the dashboard can connect scoped `user` and `user_agent` credentials for any [OAuth framework](https://docs.mindroom.chat/oauth-framework/) provider, including the Google Drive, Docs, Gmail, Calendar, Sheets, and Tasks providers, whose tools still execute in the primary runtime, except Spotify, which, like the Home Assistant integration, stays shared-only.
GitHub and Atlassian managed OAuth credentials always use the requester's `user` scope, independently of the agent's `worker_scope`.
Saved tool settings are stored by the primary per agent for `shared`, per requester across agents for `user`, and per requester-agent pair for `user_agent`, and reach worker-routed calls through [credential leases](#credential-leases).
Tools without a scoped OAuth provider have no dashboard form for `user` and `user_agent` settings, so configure them in authored config (see [Per-Agent Tool Configuration](https://docs.mindroom.chat/tools/#per-agent-tool-configuration)) or through shared settings granted with `defaults.worker_grantable_credentials`.
Only unscoped and `shared` agents are available through the [OpenAI-compatible API](https://docs.mindroom.chat/openai-api/#model-selection).

### Shared-only integrations

`spotify` and `homeassistant` require `worker_scope` unset or `shared`, and always run in the primary runtime.
Configured MCP `mcp_<server_id>` tools work on every worker scope (see [MCP](https://docs.mindroom.chat/mcp/#oauth-backed-remote-mcp)); [Where Connections Are Stored](https://docs.mindroom.chat/oauth-framework/#where-connections-are-stored) lists which OAuth connection each scope uses.

## Shell environment and PATH

Worker-routed `shell` receives only a small non-secret system environment by default, such as `PATH`, `HOME`, `USER`, `TMPDIR`, locale, proxy, and certificate path variables.
Runtime `.env` values and provider API keys such as `OPENAI_API_KEY` and `ANTHROPIC_API_KEY` are not forwarded.
Worker-routed `python` receives only allowed runtime names.

To pass more variables to `shell`, set the shell tool's `extra_env_passthrough` to exact names or glob patterns of exported process variables; it does not match config-adjacent `.env` entries.
Everything that matches passes through, including service tokens and provider credentials, except MindRoom's own control variables such as credential seeds, Docker and Kubernetes worker backend settings, `MINDROOM_CREDENTIALS_ENCRYPTION_KEY`, and any name starting with `MINDROOM_SANDBOX_`.
Use `shell_path_prepend` to add directories, such as wrapper directories, ahead of the existing `PATH`.
Both options are documented with the [shell tool](https://docs.mindroom.chat/tools/execution-and-coding/#shell).
A plugin's [`tool:environment` hook](https://docs.mindroom.chat/hooks/#shell-command-environment-toolenvironment) can also set, remove, or pass through variables for one call's commands.

### Workspace home contract

For worker-routed `shell` and `python` calls with a resolved workspace, MindRoom sets `HOME`, `MINDROOM_AGENT_WORKSPACE`, `XDG_CONFIG_HOME`, `XDG_DATA_HOME`, and `XDG_STATE_HOME` to the workspace or directories under it.
`XDG_CACHE_HOME`, `PIP_CACHE_DIR`, `UV_CACHE_DIR`, and `PYTHONPYCACHEPREFIX` point at the worker cache directory when a worker root exists, and `VIRTUAL_ENV` stays on the worker's environment.
Neither passthrough nor `.mindroom/worker-env.sh` can redirect these variables.
As a result `pwd`, `~`, `Path.home()`, attachment `mindroom_output_path` saves, and relative paths in the `file` and `coding` tools all refer to the same workspace.
For example, after `get_attachment("att_...", mindroom_output_path="incoming/file.txt")`, worker-routed shell can read both `incoming/file.txt` and `~/incoming/file.txt`.

### Workspace env hook (`.mindroom/worker-env.sh`)

Agents can set custom environment variables for worker-routed `shell` and `python` calls, such as `PATH` entries, package indexes, or npm cache locations, by writing a script to `.mindroom/worker-env.sh` at the root of the agent workspace, without changing config or redeploying.
It works the same with every worker backend.
For an example, see the [shell tool](https://docs.mindroom.chat/tools/execution-and-coding/#shell).

The hook lives at:

- `agents/<agent>/workspace/.mindroom/worker-env.sh` for shared and unscoped agents.
- `private_instances/<scope>/<agent>/<private.root>/.mindroom/worker-env.sh` for private agents, where `private.root` defaults to `<agent>_data`.
- The current request's workspace for `worker_scope: user`, so one shared user runtime picks up each agent's own hook.

How it behaves:

- The runner sources the script with `bash` before each call, and edits take effect on the next call without restarts, config reloads, or Helm changes.
- Only exported variables apply: write `export FOO=bar`, because plain `FOO=bar`, aliases, functions, and `cd` do not carry over.
- The script starts from the prepared request environment, not the runner's full environment.
- Exported values pass through, including tokens you export on purpose, except MindRoom's control variables (the same ones dropped from [shell passthrough](#shell-environment-and-path)), bash bookkeeping variables such as `PWD` and `SHLVL`, the [workspace home](#workspace-home-contract) variables, and values larger than 32 KiB.
- A variable a plugin's [`tool:environment` hook](https://docs.mindroom.chat/hooks/#shell-command-environment-toolenvironment) sets or removes for a `shell` call wins over the script's export.

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
For shell authentication, use [environment passthrough](#shell-environment-and-path), the [workspace env hook](#workspace-env-hook-mindroomworker-envsh), or [Agent Keys](#agent-keys-egress-broker).

## Agent Keys (egress broker)

To let worker-routed `shell` and `python` call external APIs without ever receiving the real credential, route their egress through a proxy that injects the credential in transit.
This works at the network layer, so it covers URLs inside scripts, package CLIs, and subprocesses.
There are two supported shapes:

- **Native egress broker** runs inside the primary runtime and injects secrets from primary-only credential stores that workers never read, for agents with the [`egress_credentials` tool](https://docs.mindroom.chat/deployment/egress-broker/#agent-tool).
  See [Agent Keys](https://docs.mindroom.chat/deployment/egress-broker/) for configuration, secret management, and deployment.
  It works with Docker workers, the static runner, and Kubernetes workers through the runtime chart's `egressBroker` values; for hosted instances, see [Hosted instances](https://docs.mindroom.chat/deployment/egress-broker/#hosted-instances-sandbox-runner-sidecar).
- **A shared credential-injecting proxy** that you run, configured through `MINDROOM_KUBERNETES_WORKER_ENV_JSON`, the chart's `egressProxy` integration, or the worker proxy environment on other backends.
  The proxy holds the real credential and workers receive only its URL.

[Approved egress](https://docs.mindroom.chat/deployment/approved-egress/) and the `mindroom-egress-proxy` image restrict which hosts workers can reach but do not inject credentials; to combine them with the egress broker, see [Egress Broker Chaining](https://docs.mindroom.chat/deployment/approved-egress/#egress-broker-chaining).

Do not put upstream API tokens in `extra_env_passthrough` or `.mindroom/worker-env.sh` unless you intend the worker process to receive them.

## Stopping and background commands

Stopping a response also stops its in-flight worker calls, such as `run_shell_command` and `run_python_code`.
With [background jobs](https://docs.mindroom.chat/tools/agent-orchestration/#background-jobs) enabled, a call that continues as a job stops when its job is cancelled instead.
Tools that run inside the runner process itself, such as `file` under the runner's `inprocess` execution mode, and persistent browser actions finish before the stop takes effect.
A shell command stops at its `timeout`; without one, a background job's command runs until it ends or the job is cancelled, and any other command stops after 120 seconds.
A runner or worker restart stops running commands; see the [shell tool](https://docs.mindroom.chat/tools/execution-and-coding/#shell).

## Browsers in workers

Dedicated Docker and Kubernetes workers with `worker_scope: shared`, `user`, or `user_agent` keep a headless browser session across calls, including open tabs and the browser profile, when Worker Computer is disabled.
Include `browser` in the agent's `worker_tools` to use it.
A change to the browser configuration or the prepared process environment closes the session before the next call.
On the `static_runner` backend every agent routed there shares one set of browser profiles, including signed-in sessions; with the default `inprocess` runner execution mode, calls without a `worker_scope` also share one running browser and its tabs, while other calls start a fresh browser.
Unscoped dedicated workers start a fresh browser for each call.
An explicit `target: host` uses the worker even when `default_target: desktop` is set.

[Worker Computer](https://docs.mindroom.chat/tools/worker-computer/) adds a visible headed browser and Chat viewer to dedicated Docker and Kubernetes workers; the `static_runner` backend does not support it.

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
  Runtimes that block Chromium's namespace sandbox need the profile described in [Worker Computer](https://docs.mindroom.chat/tools/worker-computer/#browser-and-container-sandboxing).
- Dedicated Docker workers can use the hardened `MINDROOM_DOCKER_WORKER_SECURITY_POLICY=computer`, which Worker Computer requires; see [Browser and container sandboxing](https://docs.mindroom.chat/tools/worker-computer/#browser-and-container-sandboxing).
- Dedicated Docker workers share the host kernel, so a kernel bug can let worker code escape its container.
  To keep worker code off the host kernel, install a sandboxed runtime for the Docker daemon and set `MINDROOM_DOCKER_WORKER_RUNTIME` to it, such as `runsc` for gVisor or `io.containerd.kata.v2` for Kata Containers, which runs each worker in its own virtual machine and needs KVM.
  With gVisor 20261005 or earlier, do not register `runsc` with `--oci-seccomp`, because workers then cannot start threads and fail to start.
  MindRoom sets no CPU or memory limit on Docker workers, so Kata sizes each worker's virtual machine from the `default_vcpus` and `default_memory` settings in its configuration, which default to 1 vCPU and 2 GiB.
  On Kubernetes, set [`workers.kubernetes.runtimeClassName`](https://docs.mindroom.chat/deployment/kubernetes/#worker-settings) instead.

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
| `MINDROOM_SANDBOX_PROXY_TIMEOUT_SECONDS` | Time limit for a proxy call that is not a background job, and for every `browser_mcp` call; a `run_shell_command` call waits at least its `timeout` plus 30 seconds, and a `browser` call at least 150 seconds | `120` |
| `MINDROOM_ATTACHMENT_INLINE_SAVE_MAX_BYTES` | Largest attachment saved into a worker workspace with `get_attachment(..., mindroom_output_path=...)`, and largest Atlassian `confluence_download_attachment` download; values above the 64 MiB attachment limit have no effect | `67108864` (64 MiB) |
| `MINDROOM_SANDBOX_CREDENTIAL_LEASE_TTL_SECONDS` | Credential lease lifetime | `60` |
| `MINDROOM_SANDBOX_CREDENTIAL_POLICY_JSON` | JSON mapping tool selectors to credential services | `{}` |
| `MINDROOM_WORKER_COMPUTER_ENABLED` | Enable the [Worker Computer](https://docs.mindroom.chat/tools/worker-computer/) persistent browser and display | `false` |
| `MINDROOM_COMPUTER_ALLOWED_ORIGINS` | JSON list of Chat origins allowed to open worker computers | `[]` |

The Helm chart sets the Kubernetes backend variables.
To deploy that backend without Helm, see [Worker settings](https://docs.mindroom.chat/deployment/kubernetes/#worker-settings).

### Dedicated Docker worker backend

| Variable | Description | Default |
|----------|-------------|---------|
| `MINDROOM_DOCKER_WORKER_IMAGE` | Container image for dedicated Docker workers | _(required for `docker`)_ |
| `MINDROOM_DOCKER_WORKER_SECURITY_POLICY` | `runtime_default` or `computer`; Worker Computer requires `computer` | `runtime_default` |
| `MINDROOM_DOCKER_WORKER_RUNTIME` | Docker runtime for every worker container: a runtime name from the Docker daemon config, or a containerd runtime type such as `io.containerd.kata.v2` whose shim binary is on the daemon's `PATH`; changing it recreates each worker the next time it is used | Docker daemon default |
| `MINDROOM_DOCKER_WORKER_PORT` | Sandbox-runner port inside the container | `8766` |
| `MINDROOM_DOCKER_WORKER_STORAGE_MOUNT_PATH` | Worker root mount path inside the container | `/app/worker` |
| `MINDROOM_DOCKER_WORKER_CONFIG_PATH` | Config path inside the container | `/app/config-host/config.yaml` |
| `MINDROOM_DOCKER_WORKER_HOST_CONFIG_PATH` | Path of `config.yaml` on the Docker host, whose directory the relative paths in each worker's projected config resolve against | Resolved `MINDROOM_CONFIG_PATH` when it exists |
| `MINDROOM_DOCKER_WORKER_IDLE_TIMEOUT_SECONDS` | Idle time before a worker container may be cleaned up | `1800` |
| `MINDROOM_DOCKER_WORKER_READY_TIMEOUT_SECONDS` | Maximum wait for a new worker to become ready | `60` |
| `MINDROOM_DOCKER_WORKER_NAME_PREFIX` | Prefix for worker container names, which use it lowercased, with each run of characters other than letters, digits, and `-` replaced by one `-`, and cut to 38 characters; a longer prefix logs a warning | `mindroom-worker` |
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
| `MINDROOM_SANDBOX_RUNNER_SUBPROCESS_TIMEOUT_SECONDS` | Per-call timeout for `subprocess` and `forkserver` execution, except for a background job's call; it also limits every `browser_mcp` call in a dedicated worker, job or not | `120` |
| `MINDROOM_STORAGE_PATH` | Writable directory for the tool registry and worker-local caches, such as `/app/workspace/.mindroom`; startup fails if it is not writable | `mindroom_data` next to the config |
| `MINDROOM_CONFIG_PATH` | Config used for requests that carry no [live config snapshot](#live-config-snapshots) | _(optional)_ |
