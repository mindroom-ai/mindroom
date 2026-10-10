# Brokered Worker Egress

The **egress broker** runs inside the primary runtime and intercepts outbound HTTP(S) requests from worker code, injecting secrets the worker never sees.
Use it when workers need to call APIs with credentials (GitHub, Google, Atlassian, OpenAI, cloud providers) while keeping those secrets out of worker code, environment variables, and container memory.
Each service gets its credential from an API key you store or from an account you connect with a few clicks, and built-in presets define the common services in one line.
This page covers how the broker works, configuration and presets, secret sources and connected accounts, deployment, secret management, error codes, migration from Agent Vault, and current limitations.

## How it works

When a worker makes an HTTP(S) request through the broker, the broker matches the destination host against configured services.
If the request matches a service rule, the broker terminates TLS with its own certificate, injects the configured secret into the request as a header or query parameter, forwards the request to the upstream server, and returns the response to the worker.
Unmatched hosts are either tunneled through (passthrough mode) or blocked (deny mode), controlled by `egress_broker.unmatched_hosts` in `config.yaml`.

Workers receive a signed token in their environment (`HTTP_PROXY`, `HTTPS_PROXY`) that authenticates them to the broker; it is valid for `MINDROOM_EGRESS_BROKER_TOKEN_TTL_SECONDS` (default 7 days).
The broker validates the token, resolves the worker's scope (user, user-agent, shared, or global for agents without a worker scope), and loads the service secret from that scope's primary-only credential store, or a fresh access token from the account connected for that scope (see [Secret sources](#secret-sources)).
The secret never reaches the worker process, its environment, or the filesystem it can read.

Each worker call gets a fresh token valid for the configured TTL (default 7 days), so long-running commands keep working while a leaked token expires on its own.
The TTL applies to new requests: an expired token gets 407 on every new request, but websocket splices and raw tunnels that were already open continue until they close.

## Configuration

### Environment variables

Set these on the primary runtime to enable the broker:

| Variable | Required | Default | Description |
|----------|----------|---------|-------------|
| `MINDROOM_EGRESS_BROKER_PORT` | Yes (when using the broker) | Unset (broker disabled) | Port the broker listens on, such as 8768; when unset the broker stays off |
| `MINDROOM_EGRESS_BROKER_HOST` | No | `0.0.0.0` | Bind address for the broker listener |
| `MINDROOM_EGRESS_BROKER_URL` | Yes (when port is set) | None | Worker-facing first hop, such as `http://host.docker.internal:8768` for Docker workers or `http://squid:3128` in an approved-egress chain |
| `MINDROOM_EGRESS_BROKER_TOKEN_TTL_SECONDS` | No | `604800` (7 days) | How long each minted worker token remains valid |

When `MINDROOM_EGRESS_BROKER_PORT` is unset, the broker does not start and workers get no broker environment.
The service and secret management API routes still work while the broker is disabled, so you can enter secrets before cutover.
The request log and CA download routes return 409 while the broker is not running.

Running MindRoom with `--no-api` disables the broker along with the API.

### Service rules

Add `egress_broker` to `config.yaml` with the services workers may reach.
Most services need only a preset; define a custom service when no preset fits.

#### Presets

A preset is a built-in service definition.
Name it with `preset` and the service is complete:

```yaml
egress_broker:
  unmatched_hosts: passthrough  # or deny
  services:
    github:
      preset: github
    gmail:
      preset: google_gmail
    openai:
      preset: openai
```

A preset fills `display_name`, `description`, `rules`, `placeholder_env`, and `oauth_provider`.
The service name is yours to choose; it names the stored `egress_<name>` credential and the dashboard row.

| Preset | Connected account (`oauth_provider`) | Rules | Placeholder env |
|--------|--------------------------------------|-------|-----------------|
| `github` | `github` | `api.github.com` bearer; `uploads.github.com` bearer; `github.com` basic with username `x-access-token` | `GH_TOKEN`, `GITHUB_TOKEN` |
| `google_drive` | `google_drive` | `www.googleapis.com` paths `/drive/` and `/upload/drive/`, bearer | None |
| `google_gmail` | `google_gmail` | `gmail.googleapis.com` bearer; `www.googleapis.com` path `/gmail/`, bearer | None |
| `google_calendar` | `google_calendar` | `www.googleapis.com` path `/calendar/`, bearer | None |
| `google_sheets` | `google_sheets` | `sheets.googleapis.com` bearer | None |
| `google_docs` | `google_docs` | `docs.googleapis.com` bearer | None |
| `google_tasks` | `google_tasks` | `tasks.googleapis.com` bearer; `www.googleapis.com` path `/tasks/`, bearer | None |
| `atlassian` | `atlassian` | `api.atlassian.com` bearer | None |
| `openai` | None | `api.openai.com` bearer | `OPENAI_API_KEY` |
| `anthropic` | None | `api.anthropic.com` header `x-api-key` | `ANTHROPIC_API_KEY` |

Placeholder values are `mindroom-brokered`.
Presets without a connected account take an API key only.

Several Google presets share `www.googleapis.com`.
Longest path prefix matching picks the right service, and a request to another path on that host is forwarded without injection, because the host has rules and so does not count as unmatched.

Any field you set next to `preset` replaces the preset's value for that field, as a whole:

```yaml
egress_broker:
  services:
    github:
      preset: github
      display_name: Work GitHub
      placeholder_env: {}  # replaces the preset's GH_TOKEN and GITHUB_TOKEN
```

Fields you leave out keep coming from the preset, so MindRoom updates to a preset reach your service.
`rules` and `placeholder_env` are not merged with the preset's lists, so an authored `rules` list replaces all of its rules.
Saving the config from the dashboard keeps the preset form you wrote and does not write the expanded values back.
An unknown preset name is rejected when the config loads, and the error lists the known presets.
A service must end up with at least one rule, from `rules` or from its preset.

#### Custom services

Define the rules yourself when no preset fits:

```yaml
egress_broker:
  services:
    github:
      display_name: GitHub
      description: GitHub API, gh CLI, and git over HTTPS
      oauth_provider: github  # optional: lets users connect an account instead of storing a key
      rules:
        - host: api.github.com
          auth: { type: bearer }
        - host: uploads.github.com
          auth: { type: bearer }
        - host: github.com
          auth: { type: basic, username: x-access-token }
      placeholder_env:
        GH_TOKEN: mindroom-brokered
        GITHUB_TOKEN: mindroom-brokered
    openai:
      display_name: OpenAI
      description: OpenAI API
      rules:
        - host: api.openai.com
          auth: { type: header, name: Authorization, template: "Bearer {secret}" }
      placeholder_env:
        OPENAI_API_KEY: mindroom-brokered
```

**Service fields:**

| Field | Required | Description |
|-------|----------|-------------|
| `preset` | No | Built-in preset that supplies the fields below; see [Presets](#presets) |
| `display_name` | No | Name shown in the UI and the agent tool; defaults to the service name |
| `description` | No | Short description shown in the UI |
| `rules` | Yes, unless a preset supplies them | Routing rules for the service |
| `placeholder_env` | No | Placeholder environment variables for workers |
| `oauth_provider` | No | MindRoom OAuth provider id (such as `github`, `google_drive`, `atlassian`) whose connected account supplies the secret when no API key is stored; see [Secret sources](#secret-sources) |
| `oauth_on_shared_workers` | No | Default `false`. Set `true` to use a requester's own GitHub or Atlassian account on `shared` and unscoped agents too, which lets every user of such an agent act with other users' connected accounts; see [Personal accounts on shared agents](#personal-accounts-on-shared-agents). Presets never set it |

**Rule fields:**

| Field | Required | Description |
|-------|----------|-------------|
| `host` | Yes | Host to match; exact, IP address, or one-level wildcard such as `*.example.com` |
| `port` | No | Port to match, 1 to 65535; omit to match any port |
| `path_prefix` | No | Path prefix to match; defaults to `/`; selects a rule but is not an access boundary (see below) |
| `auth` | Yes | Authentication configuration for this rule |

**Auth types:**

| Type | Injects |
|------|---------|
| `bearer` | `Authorization: Bearer <secret>` |
| `basic` | `Authorization: Basic <base64(username:secret)>`; requires `username` field |
| `header` | Custom header; requires `name` and `template` fields; `name` must be a valid HTTP header name other than `Host`, `Content-Length`, `Transfer-Encoding`, `Connection`, `Upgrade`, `TE`, `Trailer`, `Proxy-Authorization`, `Proxy-Connection`, or `Keep-Alive`; template must contain `{secret}` exactly once |
| `query` | Query parameter `<name>=<secret>`; requires a non-empty `name`; replaces any client value |

Matching follows this order: exact host over wildcard, specific port over none, longest path prefix, then declaration order.

Service names use up to 63 lowercase letters, digits, `_`, and `-`, start with a letter or digit, and must not end in `_oauth` or `_oauth_client`, because the stored `egress_<name>` credential would then read as an OAuth service.

**Choosing auth safely:**

- Prefer `bearer`, `basic`, or `header` auth. The broker returns upstream responses to the worker, so any response that echoes the request can reveal the secret to worker code.
- `query` auth puts the secret in the URL. A redirect `Location` header, an error page, or a log line that echoes the URL can hand it back to the worker; use it only for APIs that accept nothing else.
- `header` and `bearer` secrets are exposed the same way by upstream debug endpoints that echo request headers (such as `httpbin.org/headers`), so do not add rules for hosts that offer them.
- `path_prefix` is compared with the raw request path, so `/allowed/../other` matches `/allowed`. It chooses which rule and secret apply but is not an access boundary: the secret still reaches only the rule's host, and any path on that host can carry it.

**Placeholder environment:**

Services with `placeholder_env` set inject those variables into worker environments when a secret is available for that service in the worker's scope, either a stored API key or a connected account.
Use this so CLIs that refuse to run without a token (such as `gh`) work without modification.
The value can be any placeholder string; `mindroom-brokered` is the conventional choice.

## Secret sources

A service can take its secret from two sources in a worker's scope:

- **API key**: a key stored for the scope, set by hand.
- **Connected account**: an OAuth login that the user connected through MindRoom's OAuth providers (the GitHub App user login, the Google providers, Atlassian). A service uses this source when it has an `oauth_provider`, which every preset except `openai` and `anthropic` sets.

For each request the broker picks the secret in this order:

1. The API key stored for the scope, if there is one. An explicit key always wins over a connected account, and removing the key falls back to the account.
2. Otherwise, the connected account's access token. The primary refreshes it only when it is near expiry, through the same serialized refresh the tools that share the connection use, so there is no second refresh path.
3. Otherwise, a 403 `credential_not_configured` that also carries a connect link when the broker may hand one out for the account (see [Error responses](#error-responses)).

The matched rule's auth type then formats the token like any other secret, so a connected GitHub account goes out as `Authorization: Bearer <token>` to `api.github.com` and as basic auth with username `x-access-token` to `github.com`.
Placeholder environment variables are added when either source is available.

OAuth access tokens are fetched and refreshed in the primary and injected by the broker.
They never reach a worker, its environment, or its filesystem; the worker holds only its proxy token and the placeholder values.

**Which account a request uses:**

- The account follows the provider's own credential policy. GitHub and Atlassian are requester-scoped: the broker uses the requester named in the worker's verified token, so each user's requests use that user's own account. By default the broker uses such an account only on `user` and `user_agent` agents (see [Personal accounts on shared agents](#personal-accounts-on-shared-agents)).
- The Google providers follow the agent's worker scope like API keys do: a `shared` agent has one connection that credential managers maintain, and a `user` agent has one per requester.
- A call without a requester has no requester-scoped account (GitHub or Atlassian) to use.

**Service accounts are never injected.**
A deployment that sets `GOOGLE_SERVICE_ACCOUNT_FILE` serves the Google providers through a shared Google service account instead of personal logins.
The broker does not inject a service account's credentials, so the egress pages show such a service as "Uses a shared service account" and personal accounts cannot be connected for that provider.
The account part shows as not connected unless a personal connection was stored earlier; such a connection keeps working, and shows as connected, until it is disconnected.
Give the service an API key if workers need to call it in that deployment.

**Unknown `oauth_provider` ids** are not a config error.
When the id is not in MindRoom's OAuth registry (for example a plugin provider that is not loaded), the broker logs one warning that names the service and uses no account source for it, so the service works with an API key only.

**Connected account problems** do not reach the worker as plain missing credentials:

- A revoked or rejected grant, or a stored credential that cannot be read, returns 403 `oauth_connection_required`. The user reconnects (an unreadable credential must first be reset, with the egress row's **Reset connection** button or from the dashboard Tools tab, and then the response has `reset_required` and no link).
- A provider outage, timeout, or network failure while refreshing returns 503 `oauth_refresh_failed`. The grant is fine, so the worker retries later and the user does not reconnect. After such a failure the broker answers the same credential store with 503 immediately for 30 seconds instead of asking the provider again.
- Logs carry the service, the provider id, and the error type, never tokens or connect links.

### Personal accounts on shared agents

A `shared` agent, and an agent without a worker scope, runs every requester's commands in one sandbox.
A proxy token minted for one user's call stays readable there for its lifetime (for example in the environment of a process that call left running), so a later command from another user could use it to act with the first user's GitHub or Atlassian account until the token expires.

So on those agents the broker does not use requester-scoped accounts at all:

- It injects no GitHub or Atlassian account and adds no placeholder variables for it, and a matched request without an API key gets a 403 `credential_not_configured` with only `manage_url`.
- The status API reports the account part with `unavailable_reason: "shared_worker"`, not connected and not connectable, and the egress pages say "Personal accounts are not used on shared agents; add an API key or ask an administrator" instead of offering **Connect**.
- The [`egress_credentials` tool](#agent-tool) reports such a service as not configured and not connectable.

Give those agents an API key instead, or move users who need their own account to a `user` or `user_agent` agent.
The Google providers are not affected: their connection on a shared agent is the agent's own, maintained by its credential managers.

An operator who accepts the risk can set `oauth_on_shared_workers: true` on the service.
The broker then injects the calling requester's own account on shared and unscoped agents as well, and anyone who can run commands on such an agent can act with any other user's connected account until that user's proxy token expires (`MINDROOM_EGRESS_BROKER_TOKEN_TTL_SECONDS`, default 7 days).
The status API marks those rows with `shared_worker_opt_in: true`, and the egress pages warn "Everyone using this agent can act with the connected account until its access expires".
Use it only where everyone who can use the agent may act with each other's accounts, such as a single-user deployment.

## Secret management

API keys are stored per worker scope in the primary-only credential store (the same encrypted store that holds other credentials), never in worker-writable locations.
Connected accounts live in MindRoom's OAuth credential stores under the same primary-only rule and the scope policy above.

**Scopes:**

| Worker scope | Secret stored in |
|--------------|------------------|
| `user_agent` | Requester and agent scoped store |
| `user` | Requester scoped store |
| `shared` | Agent scoped store |
| None (no worker scope) | Global store, shared by every agent without a worker scope |

Agents without a worker scope do not get a store of their own: they all read the one global key per service, so setting or removing it affects every such agent.
The dashboard edits it as **Global (unscoped agents)**.

Set secrets and connect accounts through the dashboard Credentials tab or the personal egress page at `/connections/egress`.
The dashboard offers agents that can run commands and have no worker scope or `shared` scope; keys for `user` and `user_agent` agents belong to each requester and are set on the personal egress page.
The status API returns, for each service and scope, whether a key is set and when it was last updated, and the state of the connected account; it never returns a secret or token.
`configured` is true when either source is available, and `active_source` is `key`, `oauth`, or `null`:

```json
{
  "name": "github",
  "configured": true,
  "active_source": "oauth",
  "key_configured": false,
  "key_updated_at": null,
  "oauth": {
    "provider": "github",
    "display_name": "GitHub",
    "connected": true,
    "account_label": "alice@example.org",
    "can_connect": true,
    "reset_required": false,
    "service_account": false,
    "unavailable_reason": null,
    "shared_worker_opt_in": false
  }
}
```

The example is abbreviated: each service also has `display_name`, `description`, and `updated_at` (the key's timestamp, kept for older clients), and the personal API adds `is_shared` and `can_manage`.
`oauth` is `null` for a service without an account provider.
`unavailable_reason` is `"shared_worker"` when the scope is a shared or unscoped agent and the provider is GitHub or Atlassian without `oauth_on_shared_workers`; the account part is then not connected and not connectable.
`shared_worker_opt_in` is `true` on such a scope when the service sets `oauth_on_shared_workers` (see [Personal accounts on shared agents](#personal-accounts-on-shared-agents)).
The OAuth part comes from the same helper as the Connections portal's per-provider status, so the egress pages and the portal agree.

### Connecting an account

On the personal egress page, the dashboard panel, and the Connections portal cards, a service with an account provider shows **Connect <Provider>** as its primary action.
It opens the provider's login in a popup; a connected row then reads "Connected as <account>" with a **Disconnect** button.
A row whose saved connection cannot be read reads "Reset required" and offers only **Reset connection**; **Connect** returns once the reset is done.
**Use an API key instead** keeps the Set, Replace, and Remove actions, and a row with a key set says the key is in use.
On a shared or unscoped agent a GitHub or Atlassian row offers no **Connect** and says personal accounts are not used there, unless the service sets `oauth_on_shared_workers`, in which case the row warns that everyone using the agent can act with the connected account.

An agent can also send a user straight to the login: the 403 `credential_not_configured` and `oauth_connection_required` responses and the [`egress_credentials` tool](#agent-tool) point at where to connect, and the broker's responses carry a `connect_url`, a short-lived single-use link.
The link carries only the connect target, not a login: the browser that opens it must be signed in to MindRoom as the requester the link was minted for.
A link for a shared scope would skip that sign-in, and any process in the shared worker could read it from the response, so the broker never hands one out: on a shared agent the response has no `connect_url` and the user connects at `manage_url` instead.
GitHub and Atlassian links are never for a shared scope, because those connections belong to the requester.
The broker reuses one link per caller for 60 seconds, so a worker that retries in a loop does not mint a new one each time.

Who may connect an account follows who may set a key, with one exception:

- For `user` and `user_agent` agents, each user connects their own account on the personal page.
- For `shared` and unscoped agents, administrators and the agent's `credential_managers` connect and disconnect accounts, and set keys.
- GitHub and Atlassian are the exception where the service sets `oauth_on_shared_workers`. Their connections belong to the requester, so any user who may use the agent can then connect their own account on a shared agent too. Without that setting a shared agent does not use these accounts, so its rows offer no connection. API keys on shared agents stay with the managers.

The personal egress page and its API, `/api/connections/egress`, authenticate with `require_connections_user`: they need [trusted upstream auth](https://docs.mindroom.chat/deployment/trusted-upstream-auth/) with JWT (`MINDROOM_TRUSTED_UPSTREAM_REQUIRE_JWT`) and a verified Matrix identity on the request.
Without that signed identity gate, such as on a lab host that has no upstream proxy, only the dashboard can manage secrets.
On the personal page, users manage their own secrets for `user` and `user_agent` agents, while secrets for shared and unscoped agents are managed only by administrators and the agent's `credential_managers`.

**API routes** (all require dashboard authentication):

| Route | Method | Description |
|-------|--------|-------------|
| `/api/egress-broker/services?agent_name=<name>` | GET | List services with configured status; omit `agent_name` for global scope |
| `/api/egress-broker/services/<name>/secret?agent_name=<name>` | PUT | Set a secret; body `{"secret": "<value>"}` |
| `/api/egress-broker/services/<name>/secret?agent_name=<name>` | DELETE | Delete a secret |
| `/api/egress-broker/services/<name>/connect?agent_name=<name>` | POST | Start the OAuth login of the service's provider for that scope; returns the authorization URL. 404 when the service has no known provider, 409 while a shared service account replaces personal accounts |
| `/api/egress-broker/services/<name>/disconnect?agent_name=<name>` | POST | Reset the connected account for that scope |
| `/api/egress-broker/logs?agent_name=&host=&service=&limit=` | GET | Query request audit logs; returns 409 when the broker is not running |
| `/api/egress-broker/ca.pem` | GET | Download the broker's root CA certificate; returns 409 when the broker is not running |

Secrets are rejected when they are empty, whitespace-only, contain ASCII control characters, or exceed 16 KiB.

The personal egress API has matching routes under `/api/connections/egress/agents/<agent>/<service>`: `PUT` and `DELETE` for the key, and `POST .../connect` and `POST .../disconnect` for the account.
The connect and disconnect routes run the same eligibility, same-origin, and permission checks as the key routes, with the requester-scoped exception above, and then delegate to the OAuth connect and disconnect flows of the service's provider.

## Agent tool

Add the `egress_credentials` tool to an agent so it can tell a user which keys or accounts are missing when a brokered request fails with `credential_not_configured`.
It replaces `agent_vault_access` for the native broker.

```yaml
agents:
  code:
    tools:
      - shell
      - egress_credentials
```

Its one function, `list_egress_credentials`, takes no arguments and returns JSON:

```json
{
  "tool": "egress_credentials",
  "services": [
    {"name": "github", "display_name": "GitHub", "configured": true, "active_source": "oauth"},
    {
      "name": "gmail",
      "display_name": "Gmail",
      "configured": false,
      "active_source": null,
      "can_connect_account": true,
      "provider": "google_gmail"
    },
    {
      "name": "openai",
      "display_name": "OpenAI",
      "configured": false,
      "active_source": null,
      "can_connect_account": false,
      "provider": null
    }
  ],
  "manage_url": "https://mindroom.example/connections/egress",
  "note": "..."
}
```

- `configured` is true when the calling agent's scope has either source for the service, and `active_source` says which one the broker uses: `key`, `oauth`, or `null`. An explicit key wins over a connected account. The tool reports the scope the broker uses, so each requester sees only their own keys and, for GitHub and Atlassian, their own account, and an agent with no worker scope reads the global store.
- A service with nothing configured also reports `can_connect_account`. When it is `true`, the user can connect an account instead of adding a key, and `provider` names the OAuth provider id. It is `false` when the service has no account provider, the provider's OAuth client is not set up, a shared service account serves the provider, a stored connection is unreadable and needs a reset first, or the agent is shared or unscoped and the provider is GitHub or Atlassian without `oauth_on_shared_workers`; `provider` is then `null`. A `true` value says the provider can be connected, not that this user may connect it: for providers that are not requester-scoped (the Google providers) on a shared agent, only administrators and credential managers can.
- `display_name` falls back to the service name when the service sets none.
- `manage_url` is the personal egress page when trusted upstream auth is enabled and the dashboard otherwise; it is `null` when `MINDROOM_PUBLIC_URL` is not set, and the note then tells the agent to ask the operator.
- The tool never returns secret values, access tokens, connect links, or update timestamps, and its note tells the agent not to ask users to paste a key into the chat. Users connect accounts and add keys at `manage_url`; the one-time `connect_url` only appears in the broker's HTTP responses to worker code.
- The tool always runs in the primary runtime. When it is built without a worker target, it returns an empty `services` list and a note saying a worker-scoped agent is required.

## Deployment

### Docker workers

Set the environment variables on the primary runtime and open the broker port on `docker0` only so workers can reach it while the host network cannot:

```bash
export MINDROOM_EGRESS_BROKER_PORT=8768
export MINDROOM_EGRESS_BROKER_HOST=0.0.0.0
export MINDROOM_EGRESS_BROKER_URL=http://host.docker.internal:8768
export MINDROOM_EGRESS_BROKER_TOKEN_TTL_SECONDS=604800
```

On the firewall, allow TCP 8768 from `docker0` to the primary:

```bash
# NixOS example (hosts/mindroom/networking.nix)
networking.firewall.interfaces.docker0.allowedTCPPorts = [ 8768 ];
```

Without this firewall rule, workers cannot reach the broker and brokered calls time out.

The broker port must not be published beyond the worker network.
Do not add it to a published ports list in `docker run -p` or Docker Compose `ports:`, because that would let any code on the host network use a leaked token until it expires.

### Static runner

Worker-routed calls on the shared static runner get the same per-call broker environment as dedicated workers.
Set `MINDROOM_EGRESS_BROKER_URL` to an address the runner process can reach; when the runner shares the primary's network, as the hosted sidecar does, that is the loopback address (see [Hosted instances](#hosted-instances-sandbox-runner-sidecar)).
Calls with no worker scope get a token for the unscoped worker key a dedicated backend would give the agent.
The runner is shared across users, so it is not an isolation boundary between the calls it serves; see [Security Posture](https://docs.mindroom.chat/architecture/security-posture/#config-and-state-kept-out-of-workers).

### Kubernetes

The broker needs no worker pod changes: the token and CA certificate travel in each call's environment, and worker pods already have a writable `/tmp` for the trust bundle.
Worker images must still come from a release that includes the broker (see [Worker images](#worker-images)).
The primary's storage volume holds broker state under `egress_broker/`, which workers never mount.
The runtime chart runs one primary replica, so there is one broker.

The charts support these topologies:

| Deployment | Worker to broker | Broker to internet |
|------------|------------------|--------------------|
| Runtime chart, no egress proxy | Worker pod to the `<fullname>-egress-broker` Service to the primary pod | Direct from the primary pod |
| Runtime chart with `egressProxy` (operator proxy) | Same, plus an additive worker egress NetworkPolicy for the broker port | Through the operator proxy only when the primary's env sets `HTTPS_PROXY`, `HTTP_PROXY`, and `NO_PROXY`; otherwise direct (see the warning below) |
| Runtime chart with `approvedEgress` (Squid) | Worker to Squid, which checks the grant by pod IP, then to the broker as Squid's parent | Direct from the primary pod |
| Instance chart (`static_runner` sidecar sharing the primary pod's network) | Sidecar to `127.0.0.1:8768` | Direct |

The first three rows are the runtime chart's two modes.
**Direct mode** (`egressBroker.enabled`, optionally with `egressProxy`) points workers straight at the broker Service.
**Chain mode** (`egressBroker.enabled` together with `approvedEgress.enabled`) keeps Squid as the workers' first hop and renders Squid's parent as the broker.

> [!WARNING]
> **Direct mode with `egressProxy` bypasses the operator proxy unless the primary uses it.**
> Each `shell` and `python` call's broker environment replaces the `HTTP_PROXY` and `HTTPS_PROXY` that `egressProxy.injectWorkerProxyEnv` gives worker pods, so that traffic goes to the broker instead of the operator proxy.
> The broker then dials from the primary pod: matched hosts always, and unmatched hosts too under the default `unmatched_hosts: passthrough`.
> Unless the primary's own environment sets `HTTPS_PROXY`, `HTTP_PROXY`, and `NO_PROXY`, that traffic leaves the cluster directly instead of through the operator proxy.
> Set the operator proxy on the primary through `env.extra`, or set `egress_broker.unmatched_hosts: deny` so only the configured services' hosts are reached directly.
> The broker connects through the operator proxy to the IP address it validated, so the proxy must allow CONNECT to IP addresses on ports 80 and 443.
> The chart does not enforce either setting yet.

The broker's environment also replaces `NO_PROXY` for `shell` and `python` calls with `localhost,127.0.0.1,::1,.svc,.cluster.local` plus the hosts of the primary's callback URLs (the broker, script gateway, and agent CLI URLs).
Entries from `egressProxy.noProxy` or a worker `NO_PROXY` no longer apply to those calls, so requests to other internal hosts go to the broker (through Squid in chain mode), which refuses private addresses with 403 `destination_blocked`.

```yaml
workers:
  backend: kubernetes

egressBroker:
  enabled: true
  port: 8768
  tokenTtlSeconds: ""
```

When `egressBroker.enabled` is true, the chart:

- Exposes an `egress-broker` port on the primary and sets `MINDROOM_EGRESS_BROKER_PORT`, `MINDROOM_EGRESS_BROKER_HOST=0.0.0.0`, and `MINDROOM_EGRESS_BROKER_URL`, plus `MINDROOM_EGRESS_BROKER_TOKEN_TTL_SECONDS` when `egressBroker.tokenTtlSeconds` is set.
- Creates a ClusterIP Service named `<fullname>-egress-broker` that selects the primary.
- Creates, when `networkPolicy.create` is true, an ingress NetworkPolicy on the primary for the broker port: from worker pods in direct mode, from the approved egress pods in chain mode.
- Adds, in direct mode only and only when the chart's worker egress NetworkPolicy exists, a worker egress rule to the broker port. Without that policy workers already have unrestricted egress and need no rule.

In chain mode `MINDROOM_EGRESS_BROKER_URL` is Squid's URL, because workers keep sending their traffic to Squid, which still enforces grants.
Requests that carry the worker's broker token in `Proxy-Authorization` go on to the broker, requests without a token go directly to the internet, and `approvedEgress.parentProxy.bypassDomains` skip the parent.
The chart renders that chain itself, so `approvedEgress.parentProxy.enabled` is not needed.
The broker checks the token and injects the secret, and it re-validates the destination address when it dials, so a DNS change after Squid's check cannot reach private addresses.
See [Approved Egress](https://docs.mindroom.chat/deployment/approved-egress/#egress-broker-chaining).

The chart refuses to render when:

- `workers.backend` is not `kubernetes`.
- `workers.kubernetes.agentVault.enabled` is also true.
- `egressBroker.port` equals `runtime.apiPort` or `scriptGateway.port`.
- `approvedEgress.parentProxy.enabled` names a host other than the default while the broker is enabled, so a custom parent is never dropped silently.

If `networkPolicy.extraEgress` restricts the primary, allow TCP 80 and 443 to public addresses yourself, because the chart cannot infer that rule.
The broker shares the primary's lifecycle: a primary restart interrupts brokered connections in flight, and brokered TLS uses primary CPU, so leave room in the primary's resource requests.

#### Hosted instances (sandbox-runner sidecar)

The instance chart that the SaaS platform deploys runs tool code in a `static_runner` sidecar that shares the primary pod's network.
Set `egressBroker.enabled: true` and the chart sets `MINDROOM_EGRESS_BROKER_PORT=8768`, `MINDROOM_EGRESS_BROKER_HOST=127.0.0.1`, and `MINDROOM_EGRESS_BROKER_URL=http://127.0.0.1:8768` on the primary container and on nothing else.
The broker binds loopback, so the sidecar reaches it without a Service or NetworkPolicy, and nothing outside the pod can.
Tenant secrets are then managed on the dashboard or the personal egress page like any other deployment.
The chart default is off, and the platform provisioner does not forward `egressBroker.enabled` yet.
It runs `helm upgrade --install` with a fixed set of values and no `--reuse-values`, and every release deploy re-provisions all tenants, so a value set by hand (for example `--set egressBroker.enabled=true`) is overwritten by the next provision and brokered secrets then silently stop being injected.
Until the provisioner forwards the value, which is a follow-up, treat a hand-set value as for testing only.
The primary image must also include the broker, so it takes effect with a SaaS release.

### Worker images

Worker and runner images must come from the same release as the primary, or at least one that includes the broker.
Older runners ignore `MINDROOM_EGRESS_BROKER_CA_PEM`, so they never install the broker's CA: requests to matched hosts then fail TLS verification and no secret is sent, while unmatched passthrough hosts keep working.
Upgrade the worker image together with the primary; on the LXC host that means rebuilding `mindroom:dev` after pulling the checkout.

### Background-script workers

Background-script workers do not yet receive the broker environment.
This gap will close after dedicated worker coverage stabilizes.

## Error responses

The broker returns these errors to worker code:

| Condition | Response |
|-----------|----------|
| Missing or invalid proxy token | 407 with `Proxy-Authenticate: Basic` challenge |
| Expired token | 407 with `Proxy-Authenticate: Basic` challenge, including on each request inside an already-open intercepted tunnel; only websocket splices and raw (unmatched-host) tunnels that were already open continue until closed |
| Plain HTTP request matching a rule | 403 JSON `{"error": "tls_required", "service": "<name>"}` |
| `Host` header inside an intercepted tunnel naming a different host than the CONNECT target | 403 JSON `{"error": "host_mismatch"}` |
| Non-origin-form request target (absolute, authority, or asterisk form) inside an intercepted tunnel | 400 JSON `{"error": "bad_request"}` |
| Destination fails dial guard (private/metadata/link-local) | 403 JSON `{"error": "destination_blocked"}` |
| Unmatched host under `deny` policy | 403 JSON `{"error": "host_not_allowed"}` with service list hint |
| Matched service, no key and no connected account in scope | 403 JSON `{"error": "credential_not_configured", "service": "<name>", "manage_url": "<link>"}`, plus `"provider": "<id>"` and `"connect_url": "<link>"` when the service's account provider is connectable and the broker may hand out a link for the scope (never for a shared scope). The link still requires its opener to be allowed to connect that account |
| Connected account revoked, rejected by the provider, or unreadable | 403 JSON `{"error": "oauth_connection_required", "service": "<name>", "provider": "<id>", "connect_url": "<link>"}`; `connect_url` is `null` for a shared scope, and an unreadable stored credential adds `"reset_required": true` and has a `null` `connect_url` until it is reset |
| Provider outage, timeout, or network failure while refreshing the account's token | 503 JSON `{"error": "oauth_refresh_failed", "service": "<name>", "provider": "<id>"}`; retry later, do not reconnect |
| Unresolvable destination | 502 |
| Upstream connect or TLS failure | 502 |
| Broker internal error or secret resolution failure | 502 JSON `{"error": "broker_error"}` |
| Request body over 1 GiB | 413 |
| Upstream timeout | 504 (connect timeout 10s, idle read timeout 30 minutes for long streams) |

A 503 `oauth_refresh_failed` backs off per credential store: for 30 seconds after a failed refresh, further requests for that store get the same 503 without calling the provider again.
The `connect_url` values are short-lived, single-use connect links, so treat the response bodies as sensitive and do not log them.

The broker never logs header values, request bodies, query strings, tokens, connect links, or secrets.
Audit records include timestamps, worker scope, agent name, requester ID, method, host, path (without query string), service name, status code, bytes transferred, and duration.

## Verified behaviors

The following are tested and intentional:

- Only `Upgrade: websocket` is spliced; after a 101 response the broker forwards frames without inspecting them.
- Plain HTTP requests that match a rule get 403 `tls_required` and are never injected, because the secret would travel unencrypted.
- Inside an intercepted HTTPS tunnel, a `Host` header that names a different host than the CONNECT target gets 403 `host_mismatch` to prevent secrets from being sent to the wrong destination. Non-origin-form request targets are rejected with 400 `bad_request`.
- Routing uses the CONNECT target host and port only, never the `Host` header, so a replaced `Host` header cannot redirect the secret.
- Redirects are returned to the client without following them; a redirect to a new host requires a new CONNECT and a new match.
- The client's value for the slot the broker injects into (such as `Authorization`) is always removed before injection.
- An explicit API key wins over a connected account, and removing the key falls back to the account.
- Connected-account tokens are refreshed only through MindRoom's OAuth credential lifecycle, formatted by the matched rule's auth type, and never leave the primary. Shared service accounts are never injected.
- Expired or invalid tokens get 407 on new requests and on requests inside an already-open tunnel, but websocket splices and raw tunnels that started before the token expired keep running until the connection closes.
- Dedicated Docker and Kubernetes workers and the static runner get the broker environment; background-script workers do not yet.

## Migrating from Agent Vault

If you are currently using Agent Vault (the Go-based proxy fork) on the LXC host or in a Kubernetes deployment, follow these steps to migrate to the native broker.

### LXC (Docker workers)

1. Add the egress broker environment variables to the MindRoom service configuration (such as `optional/mindroom-runtime-services.nix`), set `MINDROOM_EGRESS_BROKER_PORT=8768`, `MINDROOM_EGRESS_BROKER_HOST=0.0.0.0`, `MINDROOM_EGRESS_BROKER_URL=http://host.docker.internal:8768`.
2. Open port 8768 on `docker0` in the host firewall configuration (such as `hosts/mindroom/networking.nix`).
3. Rebuild the worker image from the updated checkout so workers install the broker's CA: `cd /srv/mindroom && docker build -t mindroom:dev -f local/instances/deploy/Dockerfile.mindroom .`
4. Add the `github` service (or other services you use) to `~/.mindroom-chat/config.yaml` under `egress_broker.services`, for example `github: {preset: github}`.
5. Set the secrets or connect accounts in the dashboard Credentials tab. The personal egress page needs trusted upstream auth with JWT, which a lab host without an upstream proxy does not have.
6. Verify the broker works by running a worker command that uses a brokered service (such as `gh pr list` or `git clone` of a private repository) and checking that the request succeeds, the audit log records it, and the worker environment and workspace contain no trace of the token.
7. Remove the `agent-vault-bridge` plugin from config.
8. Remove the Agent Vault CA pin from the NixOS configuration.
9. Stop the `agent-vault` compose stack on the `nas` host after a grace period.

### Kubernetes

Services config and secret management work while the broker is disabled, so move secrets over before the cutover:

1. Add the services to the deployment's `config.yaml` under `egress_broker.services`.
2. Have users and credential managers enter their secrets on `/connections/egress` or the dashboard while Agent Vault still serves traffic.
3. Run one `helm upgrade` that sets `egressBroker.enabled=true` and `workers.kubernetes.agentVault.enabled=false`. The chart refuses both being on at once. If approved egress is on, the chart rewires Squid's parent from Agent Vault to the broker in the same upgrade.
4. Verify against real workers: a brokered call succeeds, the audit log records it, and `env` and `/proc/self/environ` hold only the placeholder values and the per-call proxy token, never a secret. Replaying the proxy URL after the token TTL returns 407.
5. Delete the Agent Vault bootstrap Secret and PVC after a grace period.

What replaces each Agent Vault piece:

| Agent Vault piece | Native replacement |
|-------------------|--------------------|
| Agent Vault server Deployment, PVC, bootstrap Job, owner and master password Secret | None; the broker runs in the primary |
| Init container minting a per-worker proxy token | Per-call token from the primary in the call's environment |
| Vault per worker key | Primary-only credential store per worker scope, with the same granularity |
| CA ConfigMap mounted at `/etc/agent-vault/ca.pem` | CA certificate in the call's environment; the runner writes the trust bundle to `/tmp` |
| `agent_vault_access` tool and Agent Vault accounts | Personal egress page (API keys and connected accounts) and the `egress_credentials` tool |
| `accessGrants` Job | `administrators` and `agents.<name>.credential_managers` in config |
| `approvedEgress.parentProxy.host: agent-vault`, port 14322 | Rendered automatically as the broker Service and port |

Hosted instances never had Agent Vault, so they have nothing to migrate and only gain brokered credentials.

## Limitations

The broker has these known limits in the initial release:

- **HTTP/1.1 only:** the broker speaks HTTP/1.1 to clients and upstreams; HTTP/2 and HTTP/3 are not supported. Most agent workloads (curl, git, Python requests, Go http) use HTTP/1.1 by default or fall back to it.
- **Service accounts are not injected:** a Google provider served by `GOOGLE_SERVICE_ACCOUNT_FILE` cannot supply the broker's secret, so such a service needs an API key.
- **No body or websocket frame substitutions:** the broker injects into headers and query parameters only; path, body, and websocket frame substitutions planned for a later release.
- **Background-script workers not yet covered:** the broker environment reaches dedicated Docker and Kubernetes workers and the static runner; background-script workers are not yet wired.
- **Shared static runner is not an isolation boundary:** a scoped call's token lives in a process shared with other users' calls, valid for the token TTL, so keep the TTL short where that matters and use dedicated `user` or `user_agent` workers for isolation. A dedicated `shared` or unscoped worker does not isolate requesters either.
- **Broker approvals stay with Squid:** the broker does not enforce approved-egress grants itself; in the Kubernetes chain Squid does, and the broker takes Agent Vault's place as its parent.

## Troubleshooting

**Workers time out when calling brokered services:**

Check that the broker port is open on the interface workers reach it through (`docker0` for Docker workers, the broker Service for Kubernetes workers).
Verify with `curl -v -x "$HTTPS_PROXY" https://example.com` from an agent's `shell` tool call, whose environment carries the broker URL and token.
A shell opened with `docker exec` does not have that per-call environment.

**403 `credential_not_configured` errors:**

The service is matched but the worker's scope has neither an API key nor a connected account for it.
Connect an account (when the response has `provider` and `connect_url`) or set a key through the dashboard or, where trusted upstream auth is enabled, the personal egress page; the error response includes a `manage_url` field pointing to the right UI.
Agents with the [`egress_credentials` tool](#agent-tool) can look up which services are set, whether an account can be connected, and relay the link to the user.

**403 `oauth_connection_required` errors:**

The account for that scope was revoked at the provider or its credential cannot be read.
Reconnect through `connect_url`, or on the egress page when the response has no link (a shared agent).
When the response has `reset_required`, reset the provider connection first, with the service row's **Reset connection** button on the egress page or the dashboard panel, or from the dashboard Tools tab, because MindRoom cannot read the stored credential.
Alternatively set an API key for the service, which takes priority over the account.

**503 `oauth_refresh_failed` errors:**

The provider's token endpoint failed or timed out while the broker refreshed the account's token, so the account itself is still connected.
Retry the request later; reconnecting does not help.
The broker answers further requests for the same credential store with 503 for 30 seconds before trying the provider again, and logs only the service, the provider id, and the error type.

**A connected account does not take effect:**

Check that the service has an `oauth_provider` (presets set it) and that the id exists in the OAuth registry; an unknown id logs a warning naming the service and leaves the service on API keys only.
An API key stored for the scope wins over the account, so remove the key to use the account.
A provider served by a shared service account shows "Uses a shared service account" and is never injected.
A GitHub or Atlassian account is not used on a shared or unscoped agent unless the service sets `oauth_on_shared_workers`; see [Personal accounts on shared agents](#personal-accounts-on-shared-agents).

**407 authentication errors:**

The worker's token is missing, invalid, or expired.
This should not happen for normal tool calls because the primary mints a fresh token for each call; if it does, check that `MINDROOM_EGRESS_BROKER_TOKEN_TTL_SECONDS` is long enough for the command's expected runtime.

**Plain HTTP requests get 403 `tls_required`:**

The request matched a service rule but used `http://` instead of `https://`.
The broker refuses to inject secrets over plain HTTP; change the request to `https://` or remove the service rule if the destination truly requires plain HTTP and does not need injection.

**Requests to unmatched hosts are blocked when I want them to pass through:**

Set `egress_broker.unmatched_hosts: passthrough` in `config.yaml`.
The default is passthrough; you may have explicitly set it to `deny`.
