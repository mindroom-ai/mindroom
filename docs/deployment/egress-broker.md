---
icon: lucide/shield-check
---

# Brokered Worker Egress

The **egress broker** runs inside the primary runtime and intercepts outbound HTTP(S) requests from worker code, injecting secrets the worker never sees.
Use it when workers need to call APIs with credentials (GitHub, OpenAI, cloud providers) while keeping those secrets out of worker code, environment variables, and container memory.
This page covers how the broker works, configuration, deployment, secret management, error codes, migration from Agent Vault, and current limitations.

## How it works

When a worker makes an HTTP(S) request through the broker, the broker matches the destination host against configured services.
If the request matches a service rule, the broker terminates TLS with its own certificate, injects the configured secret into the request as a header or query parameter, forwards the request to the upstream server, and returns the response to the worker.
Unmatched hosts are either tunneled through (passthrough mode) or blocked (deny mode), controlled by `egress_broker.unmatched_hosts` in `config.yaml`.

Workers receive a short-lived signed token in their environment (`HTTP_PROXY`, `HTTPS_PROXY`) that authenticates them to the broker.
The broker validates the token, resolves the worker's scope (user, user-agent, shared, or global), and loads the service secret from that scope's primary-only credential store.
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

Add `egress_broker` to `config.yaml` with the services workers may reach:

```yaml
egress_broker:
  unmatched_hosts: passthrough  # or deny
  services:
    github:
      display_name: GitHub
      description: GitHub API, gh CLI, and git over HTTPS
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

**Rule fields:**

| Field | Required | Description |
|-------|----------|-------------|
| `host` | Yes | Host to match; exact, IP address, or one-level wildcard such as `*.example.com` |
| `port` | No | Port to match; omit to match any port |
| `path_prefix` | No | Path prefix to match; defaults to `/` |
| `auth` | Yes | Authentication configuration for this rule |

**Auth types:**

| Type | Injects |
|------|---------|
| `bearer` | `Authorization: Bearer <secret>` |
| `basic` | `Authorization: Basic <base64(username:secret)>`; requires `username` field |
| `header` | Custom header; requires `name` and `template` fields; template must contain `{secret}` exactly once |
| `query` | Query parameter `<name>=<secret>`; requires `name` field; replaces any client value |

Matching follows this order: exact host over wildcard, specific port over none, longest path prefix, then declaration order.

**Placeholder environment:**

Services with `placeholder_env` set inject those variables into worker environments when a secret is configured for that service in the worker's scope.
Use this so CLIs that refuse to run without a token (such as `gh`) work without modification.
The value can be any placeholder string; `mindroom-brokered` is the conventional choice.

## Secret management

Secrets are stored per worker scope in the primary-only credential store (the same encrypted store that holds other credentials), never in worker-writable locations.

**Scopes:**

| Worker scope | Secret stored in |
|--------------|------------------|
| `user_agent` | Requester and agent scoped store |
| `user` | Requester scoped store |
| `shared` | Agent scoped store |
| Global (no agent) | Global store |

Set secrets through the dashboard Credentials tab or the personal egress page at `/connections/egress`.
The status API returns whether a secret is configured and when it was last updated; it never returns the secret value.

The personal egress page and its API, `/api/connections/egress`, authenticate with `require_connections_user`: they need [trusted upstream auth](trusted-upstream-auth.md) with JWT (`MINDROOM_TRUSTED_UPSTREAM_REQUIRE_JWT`) and a verified Matrix identity on the request.
Without that signed identity gate, such as on a lab host that has no upstream proxy, only the dashboard can manage secrets.
On the personal page, users manage their own secrets for `user` and `user_agent` agents, while secrets for shared and unscoped agents are managed only by administrators and the agent's `credential_managers`.

**API routes** (all require dashboard authentication):

| Route | Method | Description |
|-------|--------|-------------|
| `/api/egress-broker/services?agent_name=<name>` | GET | List services with configured status; omit `agent_name` for global scope |
| `/api/egress-broker/services/<name>/secret?agent_name=<name>` | PUT | Set a secret; body `{"secret": "<value>"}` |
| `/api/egress-broker/services/<name>/secret?agent_name=<name>` | DELETE | Delete a secret |
| `/api/egress-broker/logs?agent_name=&host=&service=&limit=` | GET | Query request audit logs; returns 409 when the broker is not running |
| `/api/egress-broker/ca.pem` | GET | Download the broker's root CA certificate; returns 409 when the broker is not running |

Secrets are rejected when they are empty, whitespace-only, contain ASCII control characters, or exceed 16 KiB.

## Agent tool

Add the `egress_credentials` tool to an agent so it can tell a user which keys are missing when a brokered request fails with `credential_not_configured`.
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
    {"name": "github", "display_name": "GitHub", "configured": true},
    {"name": "openai", "display_name": "openai", "configured": false}
  ],
  "manage_url": "https://mindroom.example/connections/egress",
  "note": "..."
}
```

- `configured` reflects the calling agent's own secret scope, so each requester sees only their own keys when the agent uses `user` or `user_agent` scope. An agent with no worker scope reads the global store.
- `display_name` falls back to the service name when the service sets none.
- `manage_url` is the personal egress page when trusted upstream auth is enabled and the dashboard otherwise; it is `null` when `MINDROOM_PUBLIC_URL` is not set, and the note then tells the agent to ask the operator.
- The tool never returns secret values or update timestamps, and its note tells the agent not to ask users to paste a key into the chat.
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
Do not add it to a published ports list in `docker run -p` or Docker Compose `ports:`, because that would let any code on the host network use the broker with a leaked or expired token.

### Static runner

Worker-routed calls on the shared static runner get the same per-call broker environment as dedicated workers.
Set `MINDROOM_EGRESS_BROKER_URL` to an address the runner process can reach; when the runner shares the primary's network, as the hosted sidecar does, that is the loopback address (see [Hosted instances](#hosted-instances-sandbox-runner-sidecar)).
Calls with no worker scope get a token for the unscoped worker key a dedicated backend would give the agent.
The runner is shared across users, so it is not an isolation boundary between the calls it serves; see [Security Posture](../architecture/security-posture.md#config-and-state-kept-out-of-workers).

### Kubernetes

The broker needs no worker pod changes: the token and CA certificate travel in each call's environment, and worker pods already have a writable `/tmp` for the trust bundle.
The primary's storage volume holds broker state under `egress_broker/`, which workers never mount.
The runtime chart runs one primary replica, so there is one broker.

The charts support these topologies:

| Deployment | Worker to broker | Broker to internet |
|------------|------------------|--------------------|
| Runtime chart, no egress proxy | Worker pod to the `<fullname>-egress-broker` Service to the primary pod | Direct from the primary pod |
| Runtime chart with `egressProxy` (operator proxy) | Same, plus an additive worker egress NetworkPolicy for the broker port | Through the operator proxy when the primary's env sets `HTTPS_PROXY` and `NO_PROXY` |
| Runtime chart with `approvedEgress` (Squid) | Worker to Squid, which checks the grant by pod IP, then to the broker as Squid's parent | Direct from the primary pod |
| Instance chart (`static_runner` sidecar sharing the primary pod's network) | Sidecar to `127.0.0.1:8768` | Direct |

The first three rows are the runtime chart's two modes.
**Direct mode** (`egressBroker.enabled`, optionally with `egressProxy`) points workers straight at the broker Service.
**Chain mode** (`egressBroker.enabled` together with `approvedEgress.enabled`) keeps Squid as the workers' first hop and renders Squid's parent as the broker.

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
See [Approved Egress](approved-egress.md#egress-broker-chaining).

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
The chart default is off and the platform provisioner does not forward this value yet, so set it in the instance chart's Helm values, for example `--set egressBroker.enabled=true`.
It takes effect with a SaaS release, because the primary image must include the broker.

### Background-script workers

Background-script workers do not yet receive the broker environment.
This gap will close after dedicated worker coverage stabilizes.

## Error responses

The broker returns these errors to worker code:

| Condition | Response |
|-----------|----------|
| Missing or invalid proxy token | 407 with `Proxy-Authenticate: Basic` challenge |
| Expired token | 407 with `Proxy-Authenticate: Basic` challenge; already-open tunnels and websockets continue until closed |
| Plain HTTP request matching a rule | 403 JSON `{"error": "tls_required", "service": "<name>"}` |
| Host mismatch inside an intercepted tunnel | 403 JSON `{"error": "host_mismatch"}` (non-origin-form target or `Host` header naming a different host) |
| Destination fails dial guard (private/metadata/link-local) | 403 JSON `{"error": "destination_blocked"}` |
| Unmatched host under `deny` policy | 403 JSON `{"error": "host_not_allowed"}` with service list hint |
| Matched service, no secret in scope | 403 JSON `{"error": "credential_not_configured", "service": "<name>", "manage_url": "<link>"}` |
| Unresolvable destination | 502 |
| Upstream connect or TLS failure | 502 |
| Broker internal error or secret resolution failure | 502 JSON `{"error": "broker_error"}` |
| Request body over 1 GiB | 413 |
| Upstream timeout | 504 (connect timeout 10s, idle read timeout 30 minutes for long streams) |

The broker never logs header values, request bodies, query strings, tokens, or secrets.
Audit records include timestamps, worker scope, agent name, requester ID, method, host, path (without query string), service name, status code, bytes transferred, and duration.

## Verified behaviors

The following are tested and intentional:

- Only `Upgrade: websocket` is spliced; after a 101 response the broker forwards frames without inspecting them.
- Plain HTTP requests that match a rule get 403 `tls_required` and are never injected, because the secret would travel unencrypted.
- Inside an intercepted HTTPS tunnel, a `Host` header or target that names a different host than the CONNECT target gets 403 `host_mismatch` to prevent secrets from being sent to the wrong destination. Non-origin-form request targets are rejected with 400.
- Routing uses the CONNECT target host and port only, never the `Host` header, so a replaced `Host` header cannot redirect the secret.
- Redirects are returned to the client without following them; a redirect to a new host requires a new CONNECT and a new match.
- The client's value for the slot the broker injects into (such as `Authorization`) is always removed before injection.
- Expired or invalid tokens get 407 on new requests and on requests inside an already-open tunnel, but websocket splices and raw tunnels that started before the token expired keep running until the connection closes.
- Dedicated Docker and Kubernetes workers and the static runner get the broker environment; background-script workers do not yet.

## Migrating from Agent Vault

If you are currently using Agent Vault (the Go-based proxy fork) on the LXC host or in a Kubernetes deployment, follow these steps to migrate to the native broker.

### LXC (Docker workers)

1. Add the egress broker environment variables to the MindRoom service configuration (such as `optional/mindroom-runtime-services.nix`), set `MINDROOM_EGRESS_BROKER_PORT=8768`, `MINDROOM_EGRESS_BROKER_HOST=0.0.0.0`, `MINDROOM_EGRESS_BROKER_URL=http://host.docker.internal:8768`.
2. Open port 8768 on `docker0` in the host firewall configuration (such as `hosts/mindroom/networking.nix`).
3. Add the `github` service (or other services you use) to `~/.mindroom-chat/config.yaml` under `egress_broker.services`.
4. Set the secrets in the dashboard Credentials tab or the Connections portal.
5. Verify the broker works by running a worker command that uses a brokered service (such as `gh pr list` or `git clone` of a private repository) and checking that the request succeeds, the audit log records it, and the worker environment and workspace contain no trace of the token.
6. Remove the `agent-vault-bridge` plugin from config.
7. Remove the Agent Vault CA pin from the NixOS configuration.
8. Stop the `agent-vault` compose stack on the `nas` host after a grace period.

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
| `agent_vault_access` tool and Agent Vault accounts | Personal egress page and the `egress_credentials` tool |
| `accessGrants` Job | `administrators` and `agents.<name>.credential_managers` in config |
| `approvedEgress.parentProxy.host: agent-vault`, port 14322 | Rendered automatically as the broker Service and port |

Hosted instances never had Agent Vault, so they have nothing to migrate and only gain brokered credentials.

## Limitations

The broker has these known limits in the initial release:

- **HTTP/1.1 only:** the broker speaks HTTP/1.1 to clients and upstreams; HTTP/2 and HTTP/3 are not supported. Most agent workloads (curl, git, Python requests, Go http) use HTTP/1.1 by default or fall back to it.
- **No body or websocket frame substitutions:** the broker injects into headers and query parameters only; path, body, and websocket frame substitutions planned for a later release.
- **Background-script workers not yet covered:** the broker environment reaches dedicated Docker and Kubernetes workers and the static runner; background-script workers are not yet wired.
- **Shared static runner is not an isolation boundary:** a scoped call's token lives in a process shared with other users' calls, valid for the token TTL, so keep the TTL short where that matters and use dedicated workers for isolation.
- **Broker approvals stay with Squid:** the broker does not enforce approved-egress grants itself; in the Kubernetes chain Squid does, and the broker takes Agent Vault's place as its parent.

## Troubleshooting

**Workers time out when calling brokered services:**

Check that the broker port is open on the interface workers reach it through (`docker0` for Docker workers, the broker Service for Kubernetes workers).
Verify with `curl -v -x $MINDROOM_EGRESS_BROKER_URL http://example.com` from inside a worker (for example `docker exec <worker> curl ...`).

**403 `credential_not_configured` errors:**

The service is matched but no secret is set for that worker's scope.
Set the secret through the dashboard or Connections portal; the error response includes a `manage_url` field pointing to the right UI.
Agents with the [`egress_credentials` tool](#agent-tool) can look up which services are set and relay the link to the user.

**407 authentication errors:**

The worker's token is missing, invalid, or expired.
This should not happen for normal tool calls because the primary mints a fresh token for each call; if it does, check that `MINDROOM_EGRESS_BROKER_TOKEN_TTL_SECONDS` is long enough for the command's expected runtime.

**Plain HTTP requests get 403 `tls_required`:**

The request matched a service rule but used `http://` instead of `https://`.
The broker refuses to inject secrets over plain HTTP; change the request to `https://` or remove the service rule if the destination truly requires plain HTTP and does not need injection.

**Requests to unmatched hosts are blocked when I want them to pass through:**

Set `egress_broker.unmatched_hosts: passthrough` in `config.yaml`.
The default is passthrough; you may have explicitly set it to `deny`.
