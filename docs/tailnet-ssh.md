---
icon: lucide/terminal
---

# Tailnet SSH

Tailnet SSH gives a requester an OpenSSH shell, remote commands, and file transfer in their agent's worker on one operator-managed Tailscale or Headscale tailnet.
The gateway enrolls one persistent node per eligible requester and agent once it discovers their worker, and can wake that same worker on connect.
The operator configures both the tailnet login and the SSH public keys; this is ordinary SSH over the tailnet, not Tailscale SSH.

## Requirements

- A dedicated [`docker` or `kubernetes` worker backend](deployment/sandbox-proxy.md), not `static_runner`, and a worker image with OpenSSH server and the `mindroom` user, as in the full MindRoom image.
- A separate `ghcr.io/mindroom-ai/mindroom-tailnet-gateway` process matching the MindRoom release, with persistent state and private network access to the primary and the worker SSH endpoints.
- A reusable, non-ephemeral auth key for the operator's tailnet and a client device signed in to that tailnet as the requester's configured login.

## Configure MindRoom

In `config.yaml`, `tailnet_ssh.logins` maps concrete Matrix user IDs to exact, nonblank Tailscale or Headscale login strings, and `tailnet_ssh.authorized_keys` maps the same IDs to lists of OpenSSH `.pub` lines without key options or private keys.
Both maps default to empty, and the gateway admits neither tagged client devices nor devices without a known matching login.
Set `agents.<name>.tailnet_ssh: true` to opt in (`false` by default).
Replace the example login and public key with the requester's real values.

```yaml
models:
  default:
    provider: anthropic
    id: claude-sonnet-5-5

tailnet_ssh:
  logins:
    "@alice:example.org": alice@example.org
  authorized_keys:
    "@alice:example.org":
      - ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIP5zSLtckJO5Wu2ZS5l7FHIxSmdRXKUf0SRAzn4Vl5s2 alice-laptop

agents:
  mind_bas:
    display_name: Research
    role: Help Alice with research
    model: default
    tools: [shell]
    worker_tools: [shell]
    worker_scope: user_agent
    access:
      users: ["@alice:example.org"]
      members_of_rooms: []
      current_room_members: false
    tailnet_ssh: true
```

### Eligibility

- The worker must be the requester's normal `user_agent` worker, not one set aside for a script run or subagent, with the agent's `shell` tool routed to that worker.
- Use `private.per: user_agent` for a [private agent](configuration/agents.md#private-instances), without setting `worker_scope` on it, or use effective `worker_scope: user_agent` and grant access to exactly one concrete Matrix user with no room grants, as above.
  No team may list a non-private agent, and if administrators can bypass its access rules, there must be no administrator other than the requester.
- The requester must still be allowed to use the agent under its [access rules](authorization.md#responder-access); if room membership is not yet known, retry after it resolves.
- The worker cannot mount another agent's shared directory as a read-only join; an owned shared directory or an assigned read-only knowledge base does not disqualify it.
- The configured agent name must be unchanged by worker-key normalization and unambiguous.
  For valid agent names (letters, digits, and underscores), this means no leading or trailing `_`; `mine_` is refused, and it would collide with `mine` if both were configured.

## Deploy the gateway

Use a key authorized to enroll multiple persistent nodes on the same tailnet, with permission to apply any server tags you configure.
For Headscale, use a reusable pre-auth key from that server and set its login server URL; leave the login server unset for Tailscale.
Tags apply to the gateway's nodes, not to client admission.
Keep the auth key in the gateway only; replace an expired key and restart the gateway for new enrollments.
Key rotation does not move existing nodes to a new tailnet, and enrollment failures have no interactive fallback.

| Gateway environment | Purpose |
| --- | --- |
| `TAILNET_SSH_AUTH_KEY` | Required enrollment key for the operator's tailnet |
| `TAILNET_SSH_LOGIN_SERVER` | Optional Headscale control URL (`tailnetSsh.loginServer` in Helm) |
| `TAILNET_SSH_TAGS` | Optional advertised node tags, separated by commas or spaces (`tailnetSsh.tags`, a list in Helm) |
| `TAILNET_SSH_GATEWAY_TOKEN` | Required shared bearer token; set the same value as `MINDROOM_TAILNET_SSH_GATEWAY_TOKEN` on the primary |
| `TAILNET_SSH_PRIMARY_URL` | Internal primary listener URL, default `http://127.0.0.1:8769` |
| `TAILNET_SSH_STATE_DIR` | Persistent node state directory, default `/state` |
| `HTTPS_PROXY` | Optional proxy for control and relay traffic; calls to the primary and worker stay direct |

### Docker

With the [Docker worker backend](deployment/sandbox-proxy.md#optional-docker-worker-isolation), give the primary a strong random `MINDROOM_TAILNET_SSH_GATEWAY_TOKEN` to enable its internal listener.
The listener binds the primary's `--api-host` on `MINDROOM_TAILNET_SSH_PORT` (default `8769`), which must differ from the API and script gateway ports.
The gateway must reach both that listener and the SSH endpoints returned by the primary: Docker maps worker port `2222` to a host port on `MINDROOM_DOCKER_WORKER_PUBLISH_HOST` (default `127.0.0.1`) and advertises `MINDROOM_DOCKER_WORKER_ENDPOINT_HOST` (defaults to the publish host).
Use a specific IP for the publish host, not a hostname or wildcard, and keep all internal ports private.
The Docker backend defaults to the primary's UID; with the full image, run both as UID `1000` so the worker runs as `mindroom` and can read its projected host key.

For a primary on the Docker host on Linux, put the gateway settings in a restricted env file and use host networking to reach the loopback listener and worker endpoints:

```bash
docker run -d --name mindroom-tailnet-ssh --network host \
  --env-file /secure/path/tailnet-gateway.env \
  --mount type=volume,source=mindroom-tailnet-ssh,target=/state \
  ghcr.io/mindroom-ai/mindroom-tailnet-gateway:"$MINDROOM_VERSION"
```

### Kubernetes

Create a Secret in the runtime chart's release namespace with the enrollment key under `auth-key` (the file should contain only the key, with no trailing newline), then reference it in the release values:

```bash
kubectl -n mindroom create secret generic mindroom-tailnet-key \
  --from-file=auth-key=/secure/path/tailnet-auth-key
```

```yaml
workers:
  backend: kubernetes
  kubernetes:
    networkPolicy:
      create: true
networkPolicy:
  create: true
tailnetSsh:
  enabled: true
  authKeySecret: mindroom-tailnet-key
  # loginServer: https://headscale.example.org
  # tags: ["tag:mindroom-worker"]
```

The chart sets the gateway environment, shares a separate listener token with the primary, and mounts a persistent volume for node state; keep that volume across restarts.
The chart reuses the listener token via Helm `lookup` when rendering against the live release.
Offline renders (including GitOps tools that use `helm template`) can generate a new token without restarting either consumer.
Keep the generated Secret stable, or roll both the primary and gateway together after its token changes; restarting only one can leave them with different tokens.

`tailnetSsh.port` defaults to `8769` and must differ from `runtime.apiPort`, `scriptGateway.port`, and `egressBroker.port`.
`tailnetSsh.image.tag` defaults to the chart app version, `tailnetSsh.persistence.size` to `1Gi`, and `tailnetSsh.resources` sets the gateway's resource requests and limits.
The gateway policy allows DNS, the internal endpoints, and outbound TCP 443 to public IPv4 by default.
For a private Headscale endpoint or relay, admit it with `tailnetSsh.extraEgress`; if you set `tailnetSsh.httpsProxy`, use `extraEgress` to admit that proxy instead of using the workers' Squid.

## Find the hostname and connect

Run a worker-routed tool once for the requester after opting in so the gateway can discover the eligible worker; login and key mappings alone do not create a node.
Discovery runs at most once a minute.
The gateway logs `started node` with the assigned hostname; check the tailnet control plane to confirm enrollment.

- Lowercase the agent name, replace each run outside `[a-z0-9]` with `-`, trim edge hyphens, cut to 63 characters, and discard a trailing hyphen left by the cut; use `worker` if empty (`mind_bas` → `mind-bas`).
- If reserved, append `-` and the requester's Matrix localpart normalized the same way (capped at 61 characters with no trailing `-`), truncating the base and dropping any trailing `-` to fit 63 characters (`research` → `research-alice`).
- If still reserved, append the first eight hex characters of SHA-256 of the full Matrix ID, a NUL byte, and the raw agent name (`research-alice-968db8c0` for `@alice:other.org` and `research`), truncating the preceding label and dropping any trailing `-` to fit, and extending the hash two hex characters at a time on further collisions, up to 62.
- Existing links reserve their names even when offline, disabled, or retiring; new links claim available names in sorted `(agent name, Matrix ID)` order.
  If the name conflicts with another device outside MindRoom, use the actual MagicDNS name shown by your tailnet's control plane.

On the requester's tailnet client, add the operator-verified public host key to `~/.ssh/known_hosts` for the actual hostname before connecting.
The operator can derive the public key from the link's Ed25519 host key in the primary's `tracking/tailnet_ssh.json`; never send the private host key to the client.

```sshconfig
Host mindroom-research
    HostName mind-bas.tail1234.ts.net
    Port 22
    User mindroom
    IdentityFile ~/.ssh/id_ed25519_mindroom
    IdentitiesOnly yes
    StrictHostKeyChecking yes
    ForwardAgent no
    ForwardX11 no
    ClearAllForwardings yes
```

Run `ssh mindroom-research`.
The gateway checks the tailnet login before waking the worker and may send `MindRoom: waking worker` while it starts; use a client connection timeout long enough for worker startup.
The worker accepts SSH public keys only, not passwords; its internal SSH port is `2222`, not the client's port `22`.
Active sessions normally keep the worker from idling out.

## Shell access and revocation

The shell starts in the agent's workspace, also its home, and can read or change anything the agent's worker shell can.
It gets the agent shell's `PATH`, environment variables (including any credentials supplied to that shell), and its scoped [Agent Keys](deployment/egress-broker.md) proxy when configured, without exposing the broker's stored upstream secrets or granting a response's temporary CLI credentials.
Keep forwarding off in the client even though the worker disables it; do not use clients that trust and execute code from the remote, such as VS Code Remote-SSH.

Remove a public key or login from `config.yaml` to revoke it.
After the config is applied, on the next successful `/links` poll (normally within seconds), a change to the configured login or set of keys closes **all** sessions for that link, even those using a remaining key.
Removing the last key or login refuses new sign-ins but does not retire the node.
If no `/links` poll succeeds for two minutes, the gateway closes all its sessions and refuses new connections, retaining node state until polling recovers.
Wait for the gateway to observe revocation before restoring access; a successful poll must see the change to cut sessions before the lease expires.

## Operating limits

- On a shared tailnet with a permissive policy, every member can reach every enrolled node's `:22`; the gateway's `WhoIs` login check and the worker's public-key check enforce access.
- Reassigning a non-private, sole-user agent hands its existing workspace to the new user, including through SSH; move or wipe that data first if it should not transfer.
- Set the affected agents' `tailnet_ssh: false` and let revocation finish **before** changing the worker backend, tenant, or namespace; new connections only use the current backend, and old workers' keys are not cleared after the move.
- A hostname can be reused after deletion, even by a different requester, but the new link has a fresh host key.
  A permitted client with the old key pinned gets a warning and must verify the replacement; the old requester is refused if the name now belongs to someone else.
- Hosted installations need the primary and worker ingress NetworkPolicies shown above, or equivalent enforced isolation, in addition to the gateway policy; otherwise in-cluster pods can reach worker sshd directly, where access is still key-only.
  Keep internal ports off public ingress and do not use `2222` for the worker runner port (`workers.kubernetes.port` or `MINDROOM_DOCKER_WORKER_PORT`).
