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

Set secrets through the dashboard Credentials tab or, when trusted upstream auth is enabled, the personal egress page at `/connections/egress`.
The status API returns whether a secret is configured and when it was last updated; it never returns the secret value.

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

The static runner does not yet receive the broker environment.
Dedicated Docker and Kubernetes workers support it; static runner coverage is planned.

### Kubernetes

The runtime chart renders the broker when `egressBroker.enabled: true` (see the runtime chart documentation).
The broker runs in the primary pod and is reached over a ClusterIP Service.
Workers in direct mode connect to the broker Service; workers in approved-egress mode connect through Squid, which forwards brokered requests (those carrying `Proxy-Authorization`) to the broker as its parent.

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
- Dedicated Docker and Kubernetes workers get the broker environment; the static runner and background-script workers are not yet covered.

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

Kubernetes chart wiring for the native broker is planned; the chart currently supports only Agent Vault.
The broker is ready for the runtime chart; template and validation changes are tracked separately.

## Limitations

The broker has these known limits in the initial release:

- **HTTP/1.1 only:** the broker speaks HTTP/1.1 to clients and upstreams; HTTP/2 and HTTP/3 are not supported. Most agent workloads (curl, git, Python requests, Go http) use HTTP/1.1 by default or fall back to it.
- **No body or websocket frame substitutions:** the broker injects into headers and query parameters only; path, body, and websocket frame substitutions planned for a later release.
- **Background-script workers not yet covered:** the broker environment reaches dedicated Docker and Kubernetes workers and will reach the static runner in a follow-up; background-script workers are not yet wired.
- **Static runner not yet covered:** the shared static runner will gain broker environment support after the dedicated worker backend stabilizes.

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
