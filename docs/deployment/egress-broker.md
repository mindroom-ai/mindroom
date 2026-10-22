---
icon: lucide/shield-check
---

# Agent Keys

**Agent Keys** let your agents use API keys and logins without ever seeing them.
The **egress broker** that does this runs inside the primary runtime and intercepts outbound HTTP(S) requests from worker code, injecting secrets the worker never sees; it is configured under `egress_broker`.
Use it when workers need to call APIs with credentials (GitHub, Google, Atlassian, OpenAI, cloud providers) while keeping those secrets out of worker code, environment variables, and container memory.
Each service gets its credential from an API key you store or from an account you connect with a few clicks, and built-in presets define the common services in one line.
Users can also add their own services and see their own recent requests on a personal page, without dashboard access.
Why the broker is built this way is in [Egress Broker Design](../architecture/egress-broker-design.md).

## How it works

When a worker makes an HTTP(S) request through the broker, the broker matches the destination host against configured services.
If the request matches a service rule, the broker terminates TLS with its own certificate, injects the configured secret into the request as a header or query parameter, forwards the request to the upstream server, and returns the response to the worker.
Unmatched hosts are either tunneled through (passthrough mode) or blocked (deny mode), controlled by `egress_broker.unmatched_hosts` in `config.yaml`.
The broker replaces any value the client sent in the header or query parameter it injects into, and returns redirects to the client without following them, so a redirect to another host gets a credential only where a rule matches that host.

Workers authenticate to the broker with a signed token in `HTTP_PROXY` and `HTTPS_PROXY`: the user name in `http://<token>:mindroom@<broker host>:<port>`, whose fixed password `mindroom` is not a secret, so curl, Python `urllib`, and Node `fetch` (with `NODE_USE_ENV_PROXY`) work as is.
The broker checks the token, resolves the worker's scope, and loads that scope's API key or connected-account token (see [Secret sources](#secret-sources)), which never reaches the worker process, its environment, or the filesystem it can read.

Each `shell` and `python` call routed to a worker gets a fresh token valid for `MINDROOM_EGRESS_BROKER_TOKEN_TTL_SECONDS` (default 7 days), so long-running commands keep working while a leaked token expires on its own.
The TTL applies to new requests: an expired token gets 407 on every new request, but websocket splices and raw tunnels that were already open continue until they close.

Those calls' `NO_PROXY` is replaced with `localhost,127.0.0.1,::1,.svc,.cluster.local` plus the hosts of the primary's own callback URLs, so a call to any other internal host goes to the broker, which refuses private addresses with 403 `destination_blocked`.
That includes LAN and Tailscale addresses and names that resolve to them, unless [`private_networks`](#private-networks) lists them.
Without that, a command that needs one can bypass the broker, for example with `curl --noproxy '*'`, where the worker's network allows direct connections.

## Configuration

### Environment variables

Set these on the primary runtime to enable the broker:

| Variable | Required | Default | Description |
|----------|----------|---------|-------------|
| `MINDROOM_EGRESS_BROKER_PORT` | Yes (when using the broker) | Unset (broker disabled) | Port the broker listens on, such as 8768; when unset the broker stays off |
| `MINDROOM_EGRESS_BROKER_HOST` | No | `0.0.0.0` | Bind address for the broker listener |
| `MINDROOM_EGRESS_BROKER_URL` | Yes (when port is set) | None | Worker-facing first hop, such as `http://host.docker.internal:8768` for Docker workers or `http://squid:3128` in an approved-egress chain |
| `MINDROOM_EGRESS_BROKER_TOKEN_TTL_SECONDS` | No | `604800` (7 days) | How long each minted worker token remains valid |

When `MINDROOM_EGRESS_BROKER_PORT` is unset, the broker does not start, workers get no broker environment, and Agent Keys is hidden: its pages, API routes, dashboard panel, and the `egress_credentials` tool are unavailable.
Keys and accounts can only be entered while the port is set.
With the port set, the request log and CA download routes return 409 while the broker is not running.

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

Any field you set next to `preset` replaces the preset's value for that field as a whole, such as `display_name: Work GitHub`, or `placeholder_env: {}` to drop the `github` preset's `GH_TOKEN` and `GITHUB_TOKEN`.
Fields you leave out keep coming from the preset, so MindRoom updates to a preset reach your service, and an authored `rules` list replaces all of the preset's rules.
A service must end up with at least one rule, from `rules` or from its preset.

#### Custom services

Define the rules yourself when no preset fits:

```yaml
egress_broker:
  services:
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
| `rules` | Yes, unless a preset supplies them | Routing rules for the service. Every rule needs `auth`, unless the service sets `restrict_only` |
| `placeholder_env` | No | Variables set in workers whose scope has a key or account for the service, so CLIs that refuse to run without a token, such as `gh`, work; any placeholder value, conventionally `mindroom-brokered` |
| `oauth_provider` | No | MindRoom OAuth provider id (such as `github`, `google_drive`, `atlassian`) whose connected account supplies the secret when no API key is stored; see [Secret sources](#secret-sources) |
| `oauth_on_shared_workers` | No | Default `false`. Set `true` to use a requester's own GitHub or Atlassian account in shared sandboxes too; see [Personal accounts and shared sandboxes](#personal-accounts-and-shared-sandboxes). Only `config.yaml` may set it, and presets never do |
| `restrict_to_rules` | No | Default `false`. Set `true` to refuse, with 403 `path_not_allowed`, a request to this service's hosts whose path matches none of its party's rules there, instead of forwarding it without credentials; see [Limiting paths and repositories](#limiting-paths-and-repositories). Presets never set it |
| `restrict_only` | No | Default `false`. Only for user services; see [Limiting in place](#limiting-in-place) |

**Rule fields:**

| Field | Required | Description |
|-------|----------|-------------|
| `host` | Yes | Host to match; exact, IP address, or one-level wildcard such as `*.example.com` |
| `port` | No | Port to match, 1 to 65535; omit to match any port |
| `path_prefix` | No | Path prefix to match by whole segments; defaults to `/`; selects a rule but is not an access boundary (see [Limiting paths and repositories](#limiting-paths-and-repositories)) |
| `auth` | Yes, unless the service sets `restrict_only` | Authentication configuration for this rule |

**Auth types:**

| Type | Injects |
|------|---------|
| `bearer` | `Authorization: Bearer <secret>` |
| `basic` | `Authorization: Basic <base64(username:secret)>`; requires `username` field |
| `header` | Custom header; requires `name` and `template` fields; `name` cannot be `Host` or a standard hop-by-hop or framing header such as `Content-Length` or `Connection`; template must contain `{secret}` exactly once |
| `query` | Query parameter `<name>=<secret>`; requires a non-empty `name`; replaces any client value |

Prefer `bearer`, `basic`, or `header` over `query`, which puts the key in the URL; see [What the broker cannot protect against](#what-the-broker-cannot-protect-against).

Matching follows this order: exact host over wildcard, specific port over none, longest path prefix, then the service whose name sorts first, so the order services are written in never matters.

Service names use up to 63 lowercase letters, digits, `_`, and `-`, start with a letter or digit, and must not end in `_oauth` or `_oauth_client`.

#### Limiting paths and repositories

**Segment matching.** A `path_prefix` ending in `/` matches every path under it, so `/` matches everything.
A prefix without a trailing slash matches the exact path or that path followed by `/`: `/repos/o/r` matches `/repos/o/r` and `/repos/o/r/pulls` but never `/repos/o/r-old`.
Matching is case-sensitive, so `/repos/Alice/Repo` does not match a rule for `/repos/alice/repo`.
That fails safe, because the request then gets no credential (or a 403 under `restrict_to_rules`), so write names as they appear in URLs.
On a host with rules, a path that could be read more than one way, such as one with `//`, a backslash, or a dot segment (also percent-encoded), is refused with 400 `bad_request` instead of being matched, while encoded slashes such as `group%2Fproject` and non-ASCII paths such as `/wiki/Café` stay allowed.

**Refusing unlisted paths.** By default a path that no rule matches is forwarded without credentials, and the upstream's own permissions decide what the secret can reach.
With `restrict_to_rules: true` on a service (or `restrict_only: true`, which implies it), a request to a host and port that one of its rules names must pass that restriction, or it is refused with 403 `path_not_allowed`.
Two parties can restrict a host, and a path must pass every party's restriction:

- **The administrator.** When a service in `config.yaml` that has a rule for the host and port sets `restrict_to_rules`, the path must match a rule of a config service on that host and port.
- **The requester.** When one of the requester's own [user services](#user-services) that has a rule for the host and port sets `restrict_to_rules`, the path must match a rule of one of their user services on that host and port.

A rule bound to another port never restricts this one.
A path that passes both checks gets the credential of the most specific matching rule over the config and user services (config services win a tie before the service name does), and a path that matches no rule is forwarded without credentials.
Rules of `restrict_only` services take part in the checks but never choose the credential.

What follows from this:

- **A user's restriction also limits the administrator's services on that host.** With the administrator's `github` service open on all of `api.github.com`, a user service that lists one repository and sets `restrict_to_rules` makes every other `api.github.com` path refuse for the scope that holds that service.
  Other requesters, and hosts the user service has no rule for, follow the administrator's services as before; see [User services](#user-services) for which agents share a scope.
- **A user's restriction cannot open a path the administrator refuses.** The administrator's check runs on its own, so a user service with a `/` rule on a host where a config service restricts still gets `path_not_allowed` for every path the config rules do not list.

An administrator's service restricted to one repository, in `config.yaml`:

```yaml
egress_broker:
  services:
    agent_cli:
      display_name: GitHub (agent-cli only)
      restrict_to_rules: true
      rules:
        - { host: api.github.com, path_prefix: /repos/octo-org/octo-repo, auth: { type: bearer } }
        - { host: github.com, path_prefix: /octo-org/octo-repo, auth: { type: basic, username: x-access-token } }
        - { host: github.com, path_prefix: /octo-org/octo-repo.git, auth: { type: basic, username: x-access-token } }
```

GitHub's GraphQL endpoint (`api.github.com` `/graphql`) is refused under `restrict_to_rules` unless a rule of the restricting party lists it.
Listing it opens every repository the credential can reach, because a GraphQL query names its repositories in the request body, which the broker never reads.

**GitHub "Limit to repositories" helper.** The [services editor](#services-editor) writes these rules for you.
Enter one `owner/repo` or `owner/*` per line and **Use these repositories** replaces the service's rules and turns on `restrict_to_rules`; repositories you typed are also applied when you save.
The helper writes:

- `owner/repo` gives `api.github.com` `/repos/owner/repo` (bearer), plus `github.com` `/owner/repo` and `/owner/repo.git` (basic auth with username `x-access-token`, which git over HTTPS uses).
- `owner/*` gives the owner-wide prefixes `/repos/owner/` and `/owner/`.
- `uploads.github.com` (release assets) and `/graphql` stay off unless you tick them.
  Leaving `uploads.github.com` out leaves any other service's rule on that host, such as the administrator's `github` preset, reaching every repository.
- Other API paths, such as `/user` or search, stop working while the restriction is on.
- On a **Custom** service that has no placeholder variables, it also adds `GH_TOKEN` and `GITHUB_TOKEN` with the value `mindroom-brokered`, so `gh` and other GitHub tooling in the worker find a token to send.

**What it cannot do:**

- It limits only the hosts the service has rules for.
- Under `unmatched_hosts: passthrough` it is not a network boundary.
  A worker can open a tunnel to the server's IP address, which the broker does not intercept.
  The tunnel carries no credential, but paths you did not list stay reachable through it.
  `unmatched_hosts: deny` refuses that tunnel.
- The real boundary is the credential.
  Use a fine-grained personal access token limited to the repositories you want, or a GitHub App installed on selected repositories, so GitHub itself refuses everything else.
  The rules only decide what the broker injects and forwards.

#### Limiting in place

A service with `restrict_only: true` only limits paths, and only users' own services may set it.
It is how a user narrows an administrator's service, such as GitHub, to some repositories: its rules count in the requester's check like those of a user service with `restrict_to_rules`, but they never choose a credential, so the administrator's service keeps sending the user's own connected account or API key for the paths the limit allows.
For example, with the administrator's `github: {preset: github}` and a limit to `octo-org/octo-repo`, `api.github.com/repos/octo-org/octo-repo/pulls` gets the administrator's credential and `api.github.com/user` gets 403 `path_not_allowed` naming the limit.
A limit has no key, account login, or placeholder variables (it may not set `oauth_provider`, `placeholder_env`, or `oauth_on_shared_workers`, and its rules need no `auth`), the broker refuses to store a key for it, and the [`egress_credentials` tool](#agent-tool) leaves it out.
On the personal page, users who can manage the agent get **Limit to repositories** on an administrator's service with a rule on `github.com`, `api.github.com`, or `uploads.github.com`, which opens the [limit editor](#the-limit-editor) and saves one.

### Private networks

The broker refuses private addresses, so by default worker calls through it cannot reach your LAN or your Tailscale network.
List the ranges agents may reach in `private_networks`:

```yaml
egress_broker:
  private_networks:
    - 192.168.1.0/24  # LAN
    - 100.64.0.0/10   # Tailscale
```

Each entry is a network in CIDR form with the host bits zero: `192.168.1.0/24`, not `192.168.1.5/24`.
A name that resolves to several addresses is dialed only when every private one is inside a listed network.
Loopback addresses (`127.0.0.0/8`, `::1`) stay refused even inside a listed range, because the broker runs in the primary process and its loopback is the primary's.
Cloud metadata, link-local, and multicast addresses and the names `localhost`, `*.localhost`, and `*.local` stay refused too.
Service rules work on hosts in these ranges, but the broker adds a key only over HTTPS with a certificate the primary trusts.
Many LAN services serve plain HTTP, which gets 403 `tls_required`, or a self-signed certificate, which fails verification and gets 502 `upstream_unreachable`.
The broker reads the list for each request, so a change to `config.yaml` applies without a restart.

> [!WARNING]
> Under `unmatched_hosts: passthrough`, a listed range opens every port on every address in it to every agent that uses the broker, including the primary's own address in that range.
> With Docker workers, a range that covers the `docker0` gateway (typically `172.17.0.1`) then reaches every port of the primary host, which the `docker0` firewall rule otherwise keeps closed.
> On Kubernetes, a range that covers the pod or Service network lets workers reach whatever the primary pod can, which their NetworkPolicy otherwise blocks.
> Under `unmatched_hosts: deny`, a listed range opens only the hosts and ports the services' rules name.
> List only the networks agents need, as narrowly as you can.

## Secret sources

A service can take its secret from two sources in a worker's scope:

- **API key**: a key stored for the scope, set by hand.
- **Connected account**: an OAuth login that the user connected through MindRoom's OAuth providers (the GitHub App user login, the Google providers, Atlassian).
  A service uses this source when it has an `oauth_provider`, which every preset except `openai` and `anthropic` sets.

For each request the broker picks the secret in this order:

1. The API key stored for the scope, if there is one.
   An explicit key always wins over a connected account, and removing the key falls back to the account.
2. Otherwise, the connected account's access token, which the primary refreshes when it is near expiry.
3. Otherwise, a 403 `credential_not_configured`, with a connect link when the user can connect an account (see [Error responses](#error-responses)).

The matched rule's auth type then formats the token like any other secret, so a connected GitHub account goes out as `Authorization: Bearer <token>` to `api.github.com` and as basic auth with username `x-access-token` to `github.com`.

**Which account a request uses:**

- The account follows the provider's own credential policy.
  GitHub and Atlassian are requester-scoped: the broker uses the requester named in the worker's verified token, so each user's requests use that user's own account.
  In shared sandboxes the broker does not use them by default; see [Personal accounts and shared sandboxes](#personal-accounts-and-shared-sandboxes).
- The Google providers follow the agent's worker scope like API keys do: a `shared` agent has one connection that credential managers maintain, and a `user` agent has one per requester.
- A call without a requester has no requester-scoped account (GitHub or Atlassian) to use.

**Service accounts are never injected.** When `GOOGLE_SERVICE_ACCOUNT_FILE` serves the Google providers through a shared service account, the broker does not inject it and personal accounts cannot be connected for those providers, so give such a service an API key.

**Unknown `oauth_provider` ids** are not a config error: when the id is not in MindRoom's OAuth registry (for example a plugin provider that is not loaded), the broker logs one warning that names the service, which then works with an API key only.

### Personal accounts and shared sandboxes

A requester's own GitHub or Atlassian account is safe to use only in a sandbox that belongs to that requester.
That is an agent with worker scope `user` or `user_agent` on a dedicated worker backend (`MINDROOM_WORKER_BACKEND` set to `docker` or `kubernetes`), where each requester's commands run in their own worker.

Every other sandbox is shared:

- A `shared` agent, and an agent without a worker scope, runs every requester's commands in one sandbox, whatever the backend.
- The static runner (`MINDROOM_WORKER_BACKEND` unset or `static_runner`, including the hosted sidecar) runs every call in one process, so even `user` and `user_agent` agents share a sandbox there.

A proxy token minted for one user's call stays readable in a shared sandbox for its lifetime (for example in the environment of a process that call left running), so a later command from another user could use it to act with the first user's GitHub or Atlassian account until the token expires; keep `MINDROOM_EGRESS_BROKER_TOKEN_TTL_SECONDS` short where that matters.

So in a shared sandbox the broker does not use requester-scoped accounts at all.
It injects no GitHub or Atlassian account and adds no placeholder variables for it, a matched request without an API key gets 403 `credential_not_configured` with no connect link, and the egress pages, the connect routes, and the [`egress_credentials` tool](#agent-tool) offer no connection for it.

Give those agents an API key instead, or run the agents whose users need their own account with worker scope `user` or `user_agent` on the Docker or Kubernetes worker backend, and make them `private` if other requesters can use them (see [Shared workspaces on non-private agents](#shared-workspaces-on-non-private-agents)).
The Google providers are not affected: their connection on a shared agent is the agent's own, maintained by its credential managers.

An operator who accepts the risk can set `oauth_on_shared_workers: true` on the service.
The broker then injects the calling requester's own account in shared sandboxes as well, and anyone who can run commands in such a sandbox can act with any other user's connected account until that user's proxy token expires (`MINDROOM_EGRESS_BROKER_TOKEN_TTL_SECONDS`, default 7 days).
Any user who may use the agent can then connect their own account on it, sees only their own connection, and gets a warning that everyone using the agent can act with it; refusals carry a connect link for the requester's own account.
On the dashboard panel, whose scopes are shared and unscoped agents, such an account is the signed-in administrator's own, so the panel offers it only with this setting.
Use it only where everyone who can use the agent may act with each other's accounts, such as a single-user deployment.

#### Shared workspaces on non-private agents

A worker per requester does not keep requesters apart.
On a `user` or `user_agent` agent that is not `private`, every requester works in the same agent workspace.
A file one requester leaves there, such as `.mindroom/worker-env.sh`, can run in another requester's commands, and it then runs with that requester's broker access.
So another requester's code can use that requester's connected accounts and personal keys, but not read them, as long as none of the hosts those services reach echo requests (see [Hosts that echo requests](#what-the-broker-cannot-protect-against)).
When requesters need to be kept apart, make the agent `private`, so that each requester gets a workspace root of their own.
Set `private.per` to `user` or `user_agent` instead of `worker_scope`, because the two cannot both be set (see [Private Instances](../configuration/agents.md#private-instances)).
Connected accounts and personal keys belong to the requester, not to one agent: GitHub and Atlassian accounts are stored at scope `user` whichever agent uses them, and `user` keys are shared by all of that requester's `user` agents.
So every personal (`user` or `user_agent`) agent that other requesters can use must be `private` for those accounts to stay out of other requesters' code, not only the agent that seems most sensitive.

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

Set secrets and connect accounts in the **Agent Keys** panel on the dashboard's Credentials page, or on the personal egress page at `/connections/egress`, linked from the Connections portal.
The panel's **Keys for** selector lists the `shared` agents that can run commands and, when such an agent exists, **Agents without a scope**.
Keys for `user` and `user_agent` agents, private agents included, belong to each requester, so the panel does not list them and each user sets them on the personal egress page.
The pages show, for each service and scope, whether a key is set and when it was last updated, and the state of the connected account, never a secret or token.
Secrets are rejected when they are empty, whitespace-only, contain ASCII control characters, or exceed 16 KiB.
The dashboard's routes are under `/api/egress-broker/`, the personal page's under `/api/connections/egress`, and the broker's root CA certificate is at `/api/egress-broker/ca.pem`.
A script can set a shared or unscoped agent's key with `PUT /api/egress-broker/services/<name>/secret?agent_name=<agent>` and the body `{"secret": "..."}`, and remove it with `DELETE` on the same URL; without `agent_name` it sets the key that agents without a scope share.
On a standalone deployment the script authenticates with `Authorization: Bearer $MINDROOM_API_KEY`.

### Connecting an account

On the personal egress page, the dashboard panel, and the Connections portal, a service with an account provider offers **Connect**, which opens the provider's login in a popup.
A connected row shows "Connected" with the account's verified email, or otherwise its login, such as `octocat`, and offers **Disconnect**, and a row whose saved connection cannot be read offers only **Reset connection** until it is reset.
In a shared sandbox, GitHub and Atlassian rows offer no **Connect**; see [Personal accounts and shared sandboxes](#personal-accounts-and-shared-sandboxes).

**Shared accounts.** Where connecting an account makes everyone who uses the agent act as it, which is an agent-scoped account (the Google providers) on a shared or unscoped agent, **Connect** first asks for confirmation and suggests a team account instead of a personal one.
Every user of the agent, not only its managers, sees which account is connected, because they all act as it.
Only administrators and credential managers can connect or disconnect it.

**Disconnecting** removes the saved connection for that agent's scope.
Every service of the agent that uses the provider, and on the Connections portal also the provider's tools, stops working until an account is connected again, and the confirmation lists them.

**Connect links.** Refusals and the [`egress_credentials` tool](#agent-tool) point at where to connect, and a refusal can carry a `connect_url`, a short-lived single-use login link that works only in a browser signed in to MindRoom as the requester it was made for.
A connection that everyone using an agent shares never gets a link; connect it at `manage_url` instead.

**Who may connect and set keys.** On `user` and `user_agent` agents each user manages their own keys, accounts, and [services](#user-services) on the personal page.
On `shared` and unscoped agents only administrators and the agent's `credential_managers` do (see [Platform and credential authority](../authorization.md#platform-and-credential-authority)); GitHub and Atlassian under `oauth_on_shared_workers` are the exception, where each user connects their own account.

The personal egress page and its API need a verified Matrix identity on the request, from [opening Connections in MindRoom Chat](../connections-portal.md#open-it-from-mindroom-chat) or from [trusted upstream auth](trusted-upstream-auth.md) with JWT (`MINDROOM_TRUSTED_UPSTREAM_REQUIRE_JWT`).
With sign-in from Chat turned off (`MINDROOM_CONNECTIONS_ALLOWED_ORIGINS='[]'`) and no JWT, nobody can sign in to the personal page and the dashboard sets only the keys and accounts of `shared` and unscoped agents.
A `user` or `user_agent` agent, private agents included, then has no page where its keys can be set: until an administrator enables one of those sign-ins or gives the agent a shared worker scope, its brokered calls to a service without a key are refused, and the refusal names no page.
When more than one user can sign in through a trusted upstream gateway, set `MINDROOM_CONNECTIONS_AGENT` as well, or every user the gateway admits gets full dashboard access, including configuration edits, shared keys, and every requester's log; with it set, non-administrators who open the dashboard are sent to the Connections portal (see [Security Boundary](trusted-upstream-auth.md#security-boundary)).

## User services

A user service is a service the user defines for their own scope instead of an administrator defining it in `config.yaml`.
Users manage them on the personal egress page at `/connections/egress`, which needs one of the [sign-ins above](#connecting-an-account).
They have the same fields and validators as a config service (a preset or `rules`, `placeholder_env`, `oauth_provider`, `restrict_to_rules`), plus [`restrict_only`](#limiting-in-place), but not `oauth_on_shared_workers`.

On `user` and `user_agent` agents each requester adds their own, which apply only to that requester's workers.
On `shared` and unscoped agents administrators and the agent's `credential_managers` add them, the same rule as for keys, and other users of the agent can read but not change them.

**Where they live.** A service is stored per scope beside that scope's keys, following the [scope table](#secret-management): per requester (and agent for `user_agent`), per agent for `shared`, and in the global store for unscoped agents, where every agent without a worker scope shares it.
So a service saved on one of a user's `user` agents applies to all of that user's `user` agents, together with its key, while each `user_agent` agent has its own.
A change applies to the next request without a restart.
A user service routes only that scope's keys and accounts, so it cannot reach anyone else's credentials.

**Names and overlap.** Config names win: saving a name that a config service already uses returns 409.
On a host that both a config service and a user service name, the usual matching order applies, with config services before user services on a tie, so a longer user prefix takes those paths for that user only.

**Inactive entries.** An entry that a config service later shadows, or that no longer validates (for example after an upgrade tightened a rule), is ignored but kept, and the personal page lists it as **Inactive** with the reason and a **Delete** button.
A shadowed entry becomes active again if the config service is removed.

**Caps.** A scope can hold at most 50 user services, counting inactive ones, and a service at most 50 rules and 16 KiB.

**User services narrow operator policy and never widen it:**

- Under `unmatched_hosts: deny`, a user rule applies only on a host and port that an operator rule names, and saving a rule for any other host is refused with a message that names the hosts.
- On a host where an operator service sets `restrict_to_rules`, a path must match an operator rule.
  A user rule only picks the credential for a path that is already allowed, so a user rule for `/` cannot open paths the operator refuses.
- A user service that sets `restrict_to_rules` also limits the operator's services on its hosts, for its scope only; see [Limiting paths and repositories](#limiting-paths-and-repositories).
- A user service can sign in with a connected account (`oauth_provider`, or a preset that has one) only on personal agents (`user` and `user_agent`).
  On a shared or unscoped agent only the administrator's services use connected accounts, so saving one with a provider returns 422; set `oauth_provider: null` and use a key.
  A provider the registry does not know is refused too.
- A user service sends a connected account only to its provider's own hosts, the exact rule hosts of the [presets](#presets) bound to that provider (such as `api.github.com`, `uploads.github.com`, and `github.com` for `github`), because the token works nowhere else.
  Saving one with another host or a wildcard is refused with a message that names the allowed hosts, and a provider no preset is bound to, such as `google_cloud`, is refused on every host.
  Services in `config.yaml` and user services that use an API key are not limited this way.

**Keys.** A new service never inherits an old key stored under its name, editing a service keeps its key, a limit never keeps one, and deleting a service deletes its key unless a config service now shadows the entry.

**Placeholder variables.** A user service's placeholder names must look like credential variable names, such as `MY_API_KEY`, and values are short plain strings such as `mindroom-brokered`; the editor says why a name or value is refused.
When a config service sets the same variable, the config value wins, and the editor warns about it.

## Services editor

One editor adds, edits, and deletes services in two places:

- **Personal egress page** (`/connections/egress`): each agent has an **Add service** button where the caller may manage services (their own personal agents, and shared or unscoped agents they manage as an administrator or credential manager).
  A user's own services have **Edit service** and **Delete service**.
  Services from `config.yaml` appear read-only under "From your administrator" and keep their key and account controls; a service that covers GitHub also offers **Limit to repositories** (see [The limit editor](#the-limit-editor)).
- **Dashboard Credentials page**: the **Agent Keys** panel adds, edits, and deletes the services of `config.yaml`, and shows the selected scope's user services read-only.
  Deleting one removes it from the configuration but keeps its saved keys, which apply again if a service with the same name is added back.
  A save writes `config.yaml` like Settings does and keeps the preset form you wrote (`{preset: github}` is not expanded); when the Settings draft holds other unsaved changes, the panel asks you to confirm first, because a save writes the whole draft.

On the personal page the editor saves the service and its key in one step.
Pick a **Preset**, or **Custom** and fill in the website (the host of the first rule), how the key is sent (bearer token, header, basic auth, query parameter, or a custom header template), and the header, username, or parameter name.
The **Name** is fixed once saved.
**More options** holds the display name, description, path prefix, port, extra rules, **Replace the preset's rules with my own**, **Refuse other paths on these hosts** (`restrict_to_rules`), the account login (for a **Custom** service), and the placeholder variables.

### The limit editor

The same editor opens in limit mode for a [`restrict_only` service](#limiting-in-place), from **Limit to repositories** (new) or **Change limit** (an existing limit).
It takes the repositories with a **GraphQL** choice and always limits `uploads.github.com` to the same repositories.
The usual caps on a service's rules (50) and size (16 KiB) apply, so a limit holds at most 12 `owner/repo` lines (4 rules each) or 16 `owner/*` lines (3 rules each).
**Remove limit** asks first, because every agent in the limit's scope can then reach every repository the key or connected account allows.

Once saved, the administrator's GitHub row shows "Limited to {repos}" with **Change limit** and **Remove limit**, and the limit is also a row of its own with a "Limit only" badge.
Another service of the user's own (or, on a shared agent, its credential managers') with rules on one of the limit's hosts lets requests past the limit, because a path passes when any of the agent's own services allows it; the row says so.

## Connections pages

Users see two pages: the Connections portal at `/connections` (their agents, tools, and account cards) and the personal egress page at `/connections/egress` (titled "Agent Keys").
Both need trusted upstream auth with JWT or Matrix sign-in from MindRoom Chat, and the portal links each agent to its section on the personal page, where services are added, edited, and deleted.
The personal page lists each agent with how many of its keys and accounts are set; expand an agent to see its services.
Each service row shows the action that makes it work, such as **Connect** or **Set key**, as a button, and keeps the rest, such as **Replace key**, **Disconnect**, **Edit service**, and **Delete service**, in its **⋯** menu.

Each row shows the hosts the service sends to, how it sends the key, and the placeholder variables the agent sees, such as `GH_TOKEN=mindroom-brokered`.
For a service with no placeholder variables and no account login, the agent is not told the key exists, so mention its host in your request.

## Personal request log

On the personal egress page each agent has a **Recent requests** table that shows the latest 10 requests, with **Show all** for up to 50, and the broker's error code under the status when the broker refused the request.
Users see only their own requests, on personal agents too; on a shared or unscoped agent they manage (as an administrator or one of its `credential_managers`), the table holds every requester's requests, with a **User** column for each requester's Matrix ID.
A `CONNECT` row that reads "Passed through (no key added)" is a tunnel to a host that no service matched.
Administrators see every requester's requests in the **Request log** under the dashboard's **Agent Keys** panel, with filters for user, agent, host, and service.
The same rows come from `GET /api/connections/egress/logs?agent_name=&limit=` (at most 200) and, for administrators, `GET /api/egress-broker/logs` with `requester`, `agent_name`, `host`, `service`, and `limit` (at most 1000).

Each row records the time, kind (`request`, `tunnel`, or `denied`), scope, agent, requester, method, host, path without its query string, service, status, bytes up and down, duration, and the broker's error `code`, which is `null` when the upstream's response was relayed.
The broker never logs header values, request bodies, query strings, tokens, connect links, or secrets.
Rows are kept for up to 30 days, at most the newest 100,000.

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
    {"name": "github", "display_name": "GitHub", "source": "config", "configured": true, "active_source": "oauth"},
    {"name": "gmail", "display_name": "Gmail", "source": "config", "configured": false, "active_source": null,
     "can_connect_account": true, "provider": "google_gmail"},
    {"name": "openai", "display_name": "OpenAI", "source": "user", "configured": false, "active_source": null,
     "can_connect_account": false, "provider": null}
  ],
  "manage_url": "https://mindroom.example/connections/egress",
  "note": "..."
}
```

- The tool lists the services the broker matches for the calling scope that take a credential: the services in `config.yaml`, then the user's own [user services](#user-services).
  `source` says which, `config` or `user`, so the agent can tell the user whether an administrator defined a service or they added it themselves.
  A [`restrict_only` service](#limiting-in-place) is left out.
- `configured` is true when the calling agent's scope has either source for the service, and `active_source` says which one the broker uses: `key`, `oauth`, or `null`.
  The tool reports the scope the broker uses, so each requester sees only their own keys and, for GitHub and Atlassian, their own account, and an agent with no worker scope reads the global store.
- A service with nothing configured also reports `can_connect_account`.
  When it is `true`, the user can connect an account instead of adding a key, and `provider` names the OAuth provider id.
  It is `false` when the service has no account provider, the provider's OAuth client is not set up, a shared service account serves the provider, a stored connection needs a reset first, or the provider is GitHub or Atlassian in a [shared sandbox](#personal-accounts-and-shared-sandboxes); `provider` is then `null`.
  A `true` value says the provider can be connected, not that this user may connect it: for the Google providers on a shared agent, only administrators and credential managers can.
- `manage_url` is the personal egress page when it is served and the dashboard otherwise; it is `null` when `MINDROOM_PUBLIC_URL` is not set, and the note then tells the agent to ask the operator.
- For a `user` or `user_agent` agent when the personal page is not served, `manage_url` is `null`, and the note says that nobody can sign in to the page for the agent's keys here, that an administrator can enable Connections sign-in or make the agent shared, and that an account can still be connected at the `connect_url` in the broker's refusal.
- The tool never returns secret values, access tokens, or connect links, and its note tells the agent not to ask users to paste a key into the chat.
- Without a worker target, the tool returns an empty `services` list and a note saying a worker-scoped agent is required.

## Deployment

### Docker workers

Set the environment variables on the primary runtime and open the broker port on `docker0` only so workers can reach it while the host network cannot:

```bash
export MINDROOM_EGRESS_BROKER_PORT=8768
export MINDROOM_EGRESS_BROKER_HOST=0.0.0.0
export MINDROOM_EGRESS_BROKER_URL=http://host.docker.internal:8768
export MINDROOM_EGRESS_BROKER_TOKEN_TTL_SECONDS=604800
```

On the firewall, allow TCP 8768 from `docker0` to the primary, or workers cannot reach the broker and brokered calls time out:

```bash
# NixOS example
networking.firewall.interfaces.docker0.allowedTCPPorts = [ 8768 ];
```

The broker port must not be published beyond the worker network.
Do not add it to a published ports list in `docker run -p` or Docker Compose `ports:`, because that would let any code on the host network use a leaked token until it expires.

### Static runner

Worker-routed calls on the shared static runner get the same per-call broker environment as dedicated workers.
Set `MINDROOM_EGRESS_BROKER_URL` to an address the runner process can reach; when the runner shares the primary's network, as the hosted sidecar does, that is the loopback address (see [Hosted instances](#hosted-instances-sandbox-runner-sidecar)).
The runner is shared across users, so it is a [shared sandbox](#personal-accounts-and-shared-sandboxes) and not an isolation boundary between the calls it serves; see [Security Posture](../architecture/security-posture.md#config-and-state-kept-out-of-workers).

### Kubernetes

Turn the broker on in the runtime chart:

```yaml
workers:
  backend: kubernetes

egressBroker:
  enabled: true
  port: 8768
```

Workers need no changes beyond an image from a release that includes the broker (see [Worker images](#worker-images)): the token and CA certificate travel in each call's environment.
The chart has two modes:

- **Direct mode** (`egressBroker.enabled`) points workers straight at a broker Service in front of the primary.
- **Chain mode** (`egressBroker.enabled` together with `approvedEgress.enabled`) keeps Squid as the workers' first hop, so it still enforces grants, and makes the broker Squid's parent; see [Approved Egress](approved-egress.md#egress-broker-chaining).

> [!WARNING]
> In direct mode with `egressProxy`, brokered traffic leaves from the primary pod instead of through the operator proxy, unless the primary's own environment sets `HTTPS_PROXY`, `HTTP_PROXY`, and `NO_PROXY` (through `env.extra`).
> Set them, or set `egress_broker.unmatched_hosts: deny` so only the configured services' hosts are reached directly.

The chart's values, what it renders, the network policies, and the settings it refuses are in the chart README's [Egress Broker](https://github.com/mindroom-ai/mindroom/blob/main/cluster/k8s/runtime/README.md#egress-broker) section.

#### Hosted instances (sandbox-runner sidecar)

The instance chart that the SaaS platform deploys runs tool code in a `static_runner` sidecar that shares the primary pod's network.
With `egressBroker.enabled: true`, the primary binds the broker on `127.0.0.1:8768`, which the sidecar reaches and nothing outside the pod can.
The sidecar is a static runner, so it is a [shared sandbox](#personal-accounts-and-shared-sandboxes).
The chart default is off, and the platform provisioner does not forward `egressBroker.enabled`: every release deploy re-provisions all tenants with a fixed set of values, so a value set by hand is overwritten and brokered secrets then stop being injected.

### Worker images

Worker and runner images must come from the same release as the primary, or at least one that includes the broker.
Older runners ignore `MINDROOM_EGRESS_BROKER_CA_PEM`, so they never install the broker's CA: requests to matched hosts then fail TLS verification and no secret is sent, while unmatched passthrough hosts keep working.
Upgrade the worker image together with the primary, and rebuild a locally built image after updating the checkout.

### Resetting broker state

The broker keeps its root CA, the key that signs worker tokens, and the request log in `egress_broker/` in the primary's storage directory (see [Storage](storage.md)).
To reset it, stop the primary, delete `egress_broker/`, and start the primary again.
The broker then creates a new CA, which new calls trust on their own, and a new token key, so every worker token issued before gets 407, and the request log is gone.
Stored keys, connected accounts, and user services are kept, because they live in the credential store.
`ca.key` is encrypted with `MINDROOM_CREDENTIALS_ENCRYPTION_KEY` when one is set, so after setting, changing, or removing that key the broker fails to start until you remove `egress_broker/`.

## Error responses

Every JSON refusal the broker writes has a `message`: a plain sentence that an agent can relay to its user, next to the machine-readable `error` code.
A message about a missing key or account or a refused path names who can fix it (the user, the agent's credential managers by Matrix ID, or an administrator) and where, at `connect_url` or `manage_url`, and no message contains a secret, token, query string, or connect link.
`manage_url` is `null` when `MINDROOM_PUBLIC_URL` is not set, and it is left out for a `user` or `user_agent` agent where the personal page is not served, whose messages then name no page.
Many clients print only the status line, so every refusal also carries `X-MindRoom-Error: <code>` and uses its message as the HTTP reason phrase, and git prints `remote error: <message>` with the `manage_url` link for a refused clone, fetch, or push.

The broker returns these errors to worker code:

| Condition | Response |
|-----------|----------|
| Missing, invalid, or expired proxy token, also on each request inside an already-open intercepted tunnel | 407 with a `Proxy-Authenticate: Basic` challenge and JSON `{"error": "proxy_authentication_required", "message": "..."}` |
| Plain HTTP request matching a rule | 403 JSON `{"error": "tls_required", "service": "<name>", "message": "..."}` |
| `Host` header inside an intercepted tunnel naming a different host than the CONNECT target | 403 JSON `{"error": "host_mismatch", "message": "..."}` |
| Client TLS server name (SNI) that differs from the CONNECT host | No response: the TLS handshake is aborted with an `unrecognized_name` alert, and nothing is forwarded |
| Request the broker cannot read unambiguously, such as an invalid CONNECT target or an HTTP protocol error | 400 JSON `{"error": "bad_request", "message": "..."}`; 431 with the same body when the request head is over 64 KiB, usually a huge `Cookie` or custom header |
| Ambiguous path on a host with rules (see [Limiting paths and repositories](#limiting-paths-and-repositories)) | 400 JSON `{"error": "bad_request", "message": "..."}` with the ambiguous-path text |
| `TRACE` or `TRACK` request to a host with rules | 405 JSON `{"error": "method_not_allowed", "message": "..."}`; the upstream would echo the injected credential back |
| Path that fails a restriction (see [Limiting paths and repositories](#limiting-paths-and-repositories)) | 403 JSON `{"error": "path_not_allowed", "service": "<restricting service>", "manage_url": "<link>", "message": "..."}` |
| Destination fails dial guard (private/metadata/link-local) | 403 JSON `{"error": "destination_blocked", "message": "..."}` |
| Unmatched host under `deny` policy | 403 JSON `{"error": "host_not_allowed", "services": [...], "message": "..."}` with the operator's service names as a hint |
| Matched service, no key and no connected account in scope | 403 JSON `{"error": "credential_not_configured", "service": "<name>", "manage_url": "<link>", "message": "..."}`, plus `"provider": "<id>"` and `"connect_url": "<link>"` when the user can connect an account that is not shared by everyone using the agent |
| Connected account revoked, rejected by the provider, or unreadable | 403 JSON `{"error": "oauth_connection_required", "service": "<name>", "provider": "<id>", "connect_url": "<link>", "manage_url": "<link>", "message": "..."}`; `connect_url` is `null` for a shared connection, and an unreadable stored credential adds `"reset_required": true` and has a `null` `connect_url` until it is reset |
| Provider outage, timeout, or network failure while refreshing the account's token | 503 JSON `{"error": "oauth_refresh_failed", "service": "<name>", "provider": "<id>", "manage_url": "<link>", "message": "..."}`; retry later, do not reconnect |
| Unresolvable destination, or upstream connect or TLS failure | 502 JSON `{"error": "upstream_unreachable", "message": "..."}` |
| Broker internal error or secret resolution failure | 502 JSON `{"error": "broker_error", "message": "..."}` |
| Request body over 1 GiB | 413 JSON `{"error": "request_body_too_large", "message": "..."}` |
| Upstream timeout | 504 JSON `{"error": "upstream_timeout", "message": "..."}` (connect timeout 10s, idle read timeout 30 minutes for long streams) |

The `connect_url` values are short-lived, single-use connect links, so treat the response bodies as sensitive and do not log them.

## Migrating from Agent Vault

If you are currently using Agent Vault (the Go-based proxy fork) with Docker workers or in a Kubernetes deployment, follow these steps to migrate to the native broker.
A deployment uses either Agent Vault or Agent Keys, never both, so the switch is one change that turns the broker on and Agent Vault off.
Keys can only be entered once the broker is on, and the secrets stored in Agent Vault do not carry over, so users enter their keys after the switch.
Until they do, a brokered call answers 403 `credential_not_configured`, and its message says where to add the key.

### Docker workers

Apply steps 1 to 5 as one change, so Agent Vault stops serving when the broker starts:

1. Set `MINDROOM_EGRESS_BROKER_PORT=8768`, `MINDROOM_EGRESS_BROKER_HOST=0.0.0.0`, and `MINDROOM_EGRESS_BROKER_URL=http://host.docker.internal:8768` on the MindRoom service.
2. Allow TCP 8768 from the Docker bridge (`docker0`) in the host firewall (see [Docker workers](#docker-workers)).
3. Rebuild or pull the worker image from the same release as the primary, so workers install the broker's CA (see [Worker images](#worker-images)).
4. Add the services you use to `config.yaml` under `egress_broker.services`, for example `github: {preset: github}`.
5. Remove the `agent-vault-bridge` plugin from the config.
6. Set the keys or connect the accounts of `shared` and unscoped agents in the dashboard's **Agent Keys** panel on the Credentials page.
   Each user sets the keys of `user` and `user_agent` agents, private agents included, on the personal egress page, which users open from MindRoom Chat or through trusted upstream auth with JWT; with neither, give those agents a shared worker scope, or their brokered calls that need a key are refused.
7. Verify the broker works by running a worker command that uses a brokered service (such as `gh pr list` or `git clone` of a private repository) and checking that the request succeeds, the request log records it, and the worker environment and workspace contain no trace of the token.
8. After a grace period, remove the Agent Vault CA from wherever you pinned it for workers, and stop Agent Vault.

### Kubernetes

1. Add the services to the deployment's `config.yaml` under `egress_broker.services`.
2. Run one `helm upgrade` that sets `egressBroker.enabled=true` and `workers.kubernetes.agentVault.enabled=false`.
   The chart refuses both being on at once.
   If approved egress is on, the chart rewires Squid's parent from Agent Vault to the broker in the same upgrade.
3. Have users and credential managers enter their keys or connect their accounts on `/connections/egress` or the dashboard.
   Users open the personal page from MindRoom Chat or through trusted upstream auth with JWT; with neither, `user` and `user_agent` agents get no keys until one is enabled or they get a shared worker scope.
4. Verify against real workers: a brokered call succeeds, the request log records it, and `env` and `/proc/self/environ` hold only the placeholder values and the per-call proxy token, never a secret.
   Replaying the proxy URL after the token TTL returns 407.
5. Delete the Agent Vault bootstrap Secret and PVC after a grace period.

The broker runs in the primary, so Agent Vault's server, its init container, per-worker vaults, and CA ConfigMap have no replacement to deploy: each call carries its own token and CA, and keys live per worker scope in the primary's credential store.
The `agent_vault_access` tool becomes `egress_credentials`, and the `accessGrants` Job becomes `administrators` and `agents.<name>.credential_managers` in config.

Hosted instances never had Agent Vault, so they have nothing to migrate and only gain brokered credentials.

## What the broker cannot protect against

The broker keeps keys out of worker code.
It cannot help where the code runs outside a worker, where the configuration or the remote host gives a key away, or where the key itself is too powerful.
Each of these is a choice you make, so make it on purpose.

- **Code in the primary process.** An agent whose code runs in the primary process can read every stored key and token.
  That is the case when no sandbox runner or dedicated worker is configured, so tools run in the primary process (the default when `MINDROOM_SANDBOX_PROXY_URL` is unset), with `MINDROOM_SANDBOX_EXECUTION_MODE` set to `off`, `local`, or `disabled`, with a `MINDROOM_SANDBOX_PROXY_TOOLS` list that leaves a tool out (unless the mode is `all`), with `worker_tools: []`, and with primary-only tools such as `duckdb`, `pandas`, `sql`, and `script`.
  Path tools that run in the primary process, such as `attachments` or `gmail`, can also open any file the primary can reach under `file_access: unrestricted`.
  Use a dedicated worker backend or a sandbox runner, keep `file_access` at `workspace`, and keep such tools off agents that should not see secrets.
- **Write access to the configuration.** `config_manager`, and any other way to write the `egress_broker` config, can point a service at another host, and the broker then sends that service's key there.
  It can also add [`private_networks`](#private-networks) that open your local network to every agent.
  Give config write access only to people and agents you would trust with every key.
- **Hosts that echo requests.** A host that sends the request back to the caller, such as a debug endpoint that echoes headers (`httpbin.org/headers`) or an error page with the full URL, hands the injected key to the agent.
  `query` auth is the most exposed, because a redirect `Location`, an error page, or a log line that echoes the URL can hand the key back; use it only for APIs that accept nothing else.
  The broker refuses `TRACE` and `TRACK`, which exist to echo requests, but it cannot see what other pages do, so add keys only for hosts you trust not to echo them.
- **Wildcard rules on shared hosting domains.** A rule such as `*.github.io`, `*.vercel.app`, or `*.workers.dev` sends the key to every tenant on that domain, including a page that someone else publishes there.
  Name exact hosts instead.
- **Keys that can create other keys.** With an admin token or a deploy-key API, the agent can ask the service for a new working key and keep it, and that key no longer needs the broker.
  Store the least powerful key that works.
- **Shared workspaces.** On a `user` or `user_agent` agent that is not `private`, one requester's files can run in another requester's commands and use that requester's accounts and keys; see [Shared workspaces on non-private agents](#shared-workspaces-on-non-private-agents).

## Limitations

- **HTTP/1.1 only:** the broker speaks HTTP/1.1 to clients and upstreams; HTTP/2 and HTTP/3 are not supported.
  Most agent workloads (curl, git, Python requests, Go http) use HTTP/1.1 by default or fall back to it.
- **No body or websocket frame substitutions:** the broker injects into headers and query parameters only; websocket frames and raw tunnels to hosts without rules are relayed as they come.
- **Background-script workers are not covered:** the broker environment reaches dedicated Docker and Kubernetes workers and the static runner, not background-script workers.
- **No independent review:** the transport code was tested with a smuggling corpus and property-based fuzzing, but no human or automated adversarial review of it has been done; see [Testing and review](../architecture/security-posture.md#testing-and-review-of-the-egress-broker).
- **Broker approvals stay with Squid:** the broker does not enforce approved-egress grants itself; in the Kubernetes chain Squid does, and the broker takes Agent Vault's place as its parent.

## Troubleshooting

**Workers time out when calling brokered services.** Check that the broker port is open on the interface workers reach it through (`docker0` for Docker workers, the broker Service for Kubernetes workers).
Verify with `curl -v -x "$HTTPS_PROXY" https://example.com` from an agent's `shell` tool call, whose environment carries the broker URL and token.
A shell opened with `docker exec` does not have that per-call environment.

**403 `destination_blocked` for a LAN or Tailscale service.** Add its range to [`private_networks`](#private-networks).

**403 `credential_not_configured` errors.** The service is matched but the worker's scope has neither an API key nor a connected account for it.
Connect an account (when the response has `provider` and `connect_url`) or set a key: for shared and unscoped agents through the dashboard (or by their credential managers on the Connections page), for personal agents on the personal egress page when it is served.
The response's `message` says who can fix it and where; for a personal agent without the personal page it names no page (see [Connecting an account](#connecting-an-account)).
Agents with the [`egress_credentials` tool](#agent-tool) can look up which services are set, whether an account can be connected, and relay the link to the user.

**403 `oauth_connection_required` errors.** The account for that scope was revoked at the provider or its credential cannot be read.
Reconnect through `connect_url`, or on the egress page when the response has no link (a shared agent).
When the response has `reset_required`, reset the provider connection first, with the service row's **Reset connection** button on the egress page or the dashboard panel, or from the dashboard Tools tab, because MindRoom cannot read the stored credential.
Alternatively set an API key for the service, which takes priority over the account.
A 503 `oauth_refresh_failed` instead means the provider failed while the broker refreshed the token; the account is still connected, so retry in a minute instead of reconnecting.

**A connected account does not take effect.** Check that the service has an `oauth_provider` (presets set it) and that the id exists in the OAuth registry; an unknown id logs a warning naming the service and leaves the service on API keys only.
An API key stored for the scope wins over the account, so remove the key to use the account.
A provider served by a shared service account is never injected; give the service an API key.
A GitHub or Atlassian account is not used in a [shared sandbox](#personal-accounts-and-shared-sandboxes) unless the service sets `oauth_on_shared_workers`.

**403 `path_not_allowed` errors.** A service on that host sets `restrict_to_rules` and no rule of that party lists the path.
The body's `service` names the restricting service, and it can be the user's own limit (such as `github-limited`, a [`restrict_only` service](#limiting-in-place)) as well as an administrator's service: a user's restriction also limits the administrator's services on its hosts, for its own scope.
Add a rule to that service whose `path_prefix` covers the path (a prefix such as `/repos/owner/repo` covers that path and everything below it), or turn its restriction off or delete it.
For a limit, use **Change limit** or **Remove limit** on the personal page.
GraphQL (`/graphql`) needs a rule of its own, and a user service cannot list a path that an operator's `restrict_to_rules` refuses.

**400 `bad_request` on a path.** The host has rules and the path is ambiguous: an empty segment (`//`), a backslash, a dot segment (also percent-encoded), a NUL, or invalid UTF-8.
Send the normalized path; the broker refuses ambiguous paths instead of guessing how the upstream reads them.

**A TLS handshake to the broker fails for one host.** The client sent an SNI that differs from the CONNECT host, which the broker aborts with an `unrecognized_name` alert.
Use the same name in the CONNECT target and the TLS server name.
Clients that send no SNI (for an IP address) are served.

**407 authentication errors.** The worker's token is missing, invalid, or expired, and the response's `message` says to start a new tool call.
This should not happen for normal tool calls because the primary mints a fresh token for each call; if it does, check that `MINDROOM_EGRESS_BROKER_TOKEN_TTL_SECONDS` is long enough for the command's expected runtime.
A client that builds its own proxy URL from the token alone, with an empty password, can also get 407: Python `urllib` and Node `fetch` send no credentials then.
Use the `HTTPS_PROXY` from the call's environment, whose password is `mindroom`.

**Plain HTTP requests get 403 `tls_required`.** The request matched a service rule but used `http://` instead of `https://`.
The broker refuses to inject secrets over plain HTTP; change the request to `https://` or remove the service rule if the destination truly requires plain HTTP and does not need injection.

**Requests to unmatched hosts are blocked when I want them to pass through.** Set `egress_broker.unmatched_hosts: passthrough` in `config.yaml`.
The default is passthrough; you may have explicitly set it to `deny`.
