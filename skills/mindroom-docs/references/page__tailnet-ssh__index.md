# Tailnet SSH

Use ordinary SSH (not Tailscale SSH) for a shell, remote commands, or file transfer in a MindRoom worker on one operator-managed Tailscale or Headscale tailnet.
The gateway owns the tailnet nodes and keeps their state outside the workers, which do not join the tailnet.

## Who can reach what

**SSH reach is agent shell reach.**
With the gateway enabled and an agent opted in, a requester who passes the agent's [access check](https://docs.mindroom.chat/authorization/#responder-access) can reach the dedicated Docker or Kubernetes worker that agent's `shell` tool would use for them.
This includes shared agents and workers reused across agents; `static_runner` and local execution have no SSH target.
SSH has no conversation room, so a grant only through `access.current_room_members` does not give SSH access.

The `mindroom` account can read or change anything the worker's shell can, including shared workspaces, joined directories, and mounted knowledge, subject to the worker's existing read-only mounts and permissions.
The session starts in a workspace selected for that worker, which may serve more than one agent, and does not receive credentials injected for a particular tool call or response.
The gateway checks the device's tailnet login, and OpenSSH checks its key against the current authorized keys for the whole worker, not a login/key pair.

## Configure MindRoom

In `config.yaml`, `tailnet_ssh.logins` maps concrete Matrix user IDs to distinct, exact Tailscale or Headscale logins (nonempty, no surrounding whitespace).
`tailnet_ssh.authorized_keys` maps those IDs to lists of OpenSSH `.pub` lines, without key options or private keys.
Both maps default to empty; each requester needs a login and at least one key, and tagged client devices are not admitted.
Set the agent's `tailnet_ssh` boolean to `true` (default `false`) and make sure its `shell` tool runs on the worker.
MindRoom refuses the opt-in if any non-MCP toolkits listed before `shell` declare `run_shell_command` or do not declare their functions.
List `shell` first in the agent's `tools` list, even if tool settings disable the conflicting function.
MCP toolkits do not block this opt-in, regardless of their order.
Merge these settings into your existing config and replace the sample login and public key:

```yaml
tailnet_ssh:
  logins:
    "@alice:example.org": alice@example.org
  authorized_keys:
    "@alice:example.org":
      - ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIP5zSLtckJO5Wu2ZS5l7FHIxSmdRXKUf0SRAzn4Vl5s2 alice-laptop

agents:
  research:
    display_name: Research
    tools: [shell]
    worker_tools: [shell]
    worker_scope: shared
    access:
      users: ["@alice:example.org"]
    tailnet_ssh: true
```

## Deploy the gateway

Use the full MindRoom worker image (it includes OpenSSH, SFTP, rsync, and the `mindroom` user) and the gateway image for the matching release.
Create a reusable, non-ephemeral tailnet auth key that can enroll multiple nodes and apply any tags you configure.
For Headscale, use a reusable pre-auth key and your Headscale URL; leave the login server unset for Tailscale.
Keep the enrollment key in the gateway only and the listener token in the primary and gateway only.

### Docker

Enable the primary's internal listener with a strong random `MINDROOM_TAILNET_SSH_GATEWAY_TOKEN`.
The listener binds the primary's `--api-host` on `MINDROOM_TAILNET_SSH_PORT` (default `8769`), which must differ from the API and script gateway ports.
With the full image, run the primary and worker as UID `1000` so sshd can read the projected host key.

The gateway must reach that listener and the worker endpoints: [Docker](https://docs.mindroom.chat/deployment/sandbox-proxy/#optional-docker-worker-isolation) maps worker port `2222` to a host port on `MINDROOM_DOCKER_WORKER_PUBLISH_HOST` (default `127.0.0.1`) and advertises `MINDROOM_DOCKER_WORKER_ENDPOINT_HOST` (defaults to the publish host).
Use a specific IP for the publish host, not a hostname or wildcard, and keep these ports private.

Put the gateway settings in a restricted env file:

| Variable | Value |
| --- | --- |
| `TAILNET_SSH_GATEWAY_TOKEN` | Same token as on the primary |
| `TAILNET_SSH_AUTH_KEY` | Tailnet enrollment key |
| `TAILNET_SSH_PRIMARY_URL` | Internal listener URL (default `http://127.0.0.1:8769`) |
| `TAILNET_SSH_LOGIN_SERVER` | Optional Headscale URL |
| `TAILNET_SSH_TAGS` | Optional tags for enrolled nodes, separated by commas or spaces |
| `TAILNET_SSH_STATE_DIR` | Persistent node state directory (default `/state`) |
| `HTTPS_PROXY` | Optional proxy for tailnet control and relay traffic; internal calls stay direct |

For a primary on the Docker host on Linux, use host networking to reach the default loopback endpoints:

```bash
docker run -d --name mindroom-tailnet-ssh --network host \
  --env-file /secure/path/tailnet-gateway.env \
  --mount type=volume,source=mindroom-tailnet-ssh,target=/state \
  ghcr.io/mindroom-ai/mindroom-tailnet-gateway:"$MINDROOM_VERSION"
```

**Docker bridge limit:** Workers on the same bridge can reach one another's sshd on `2222` directly, even if the published host port binds to loopback.
That path checks only the projected SSH keys, not the tailnet login, and may still accept a removed key until the next projection or until the worker stops.

### Kubernetes

Create the enrollment Secret in the runtime release namespace with the key under `auth-key` (no trailing newline in the file):

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

Use a cluster that enforces NetworkPolicies and keep both ingress policies shown above, or equivalent isolation, so unrelated pods cannot reach the private listener or worker sshd.
Each release's SSH policies admit only its own gateway and workers, even when releases share a worker namespace.
On upgrade to release-scoped policies, recycle existing SSH-enabled workers if they have not picked up the new pod label and SSH is unavailable.
The chart creates the shared listener token and a PVC for node state; keep both across restarts.
Offline Helm renders can generate a new token; keep the Secret stable or restart both the primary and gateway after it changes.
`tailnetSsh.port` defaults to `8769` and must differ from `runtime.apiPort`, `scriptGateway.port`, and `egressBroker.port`.
The gateway policy allows DNS, internal endpoints, and outbound TCP 443 to public IPv4 by default; use `tailnetSsh.extraEgress` to admit private Headscale or relay endpoints, or the proxy if you set `tailnetSsh.httpsProxy`.

## Find the hostname and connect

The gateway enrolls one node per eligible worker, even before it has started, so there is no need to run a tool first.
Find the worker's node in the tailnet control plane and use its actual MagicDNS hostname.
For `user` and `user_agent` workers, the hostname is derived from a key containing the Matrix user ID and can expose it to the operator's tailnet.
The operator can derive its public host key from the Ed25519 key for that worker in the primary's `tracking/tailnet_ssh.json`; share only the public key and pin it to the hostname in `~/.ssh/known_hosts`.

```bash
ssh -o StrictHostKeyChecking=yes -o IdentitiesOnly=yes -i ~/.ssh/id_ed25519_mindroom mindroom@v1-default-shared-research.tail1234.ts.net
```

Connect from a device signed in as the configured tailnet login, using port `22`, not the internal `2222`.
Use a client timeout long enough for startup; the gateway may report `MindRoom: waking worker` while it waits.
Active sessions normally keep the worker from idling out, even when a bad config reload temporarily blocks new connections and link listings.
With idle cleanup enabled, set the worker idle timeout to at least 150 seconds (the default is 1,800 seconds).
The gateway waits 75 seconds before counting a connection and reports every 30 seconds; leave extra time for slow startup or delayed reports.
Password authentication and forwarding are disabled; do not use clients that forward credentials or trust and execute code from the worker.

## Revoke access

Remove a user's login, their keys, or their access through every opted-in agent that routes them to the worker.
Set an agent's `tailnet_ssh: false` to stop granting access through that agent.
Once the change takes effect, new gateway connections using the revoked login or access are refused.
When no authorized login with a key remains, the primary stops listing the link and the gateway takes the node offline after open connections drain, retaining its state.
A removed key cannot authenticate on a new gateway connection unless it is still authorized for that worker through another entry.
Existing sessions last until closed or the worker stops, unless the transport itself fails.
Removal does not clear the worker's `authorized_keys`; the next admitted gateway connection rewrites it from current config.

## Troubleshooting

- **No node:** Check the matching gateway token, agent opt-in, dedicated shell routing, requester access, login and key, and enrollment key validity.
  If access depends on room membership, retry after membership has resolved; check gateway logs and the tailnet control plane for enrollment or policy problems.
- **Node visible but SSH closes or stalls:** Check that the tailnet policy permits TCP `22` from the client and the gateway can reach the primary and worker endpoints.
  On Docker, a wildcard or non-IP publish host can leave the link listed without a worker SSH endpoint.
  After repeated attempts, wait a minute before retrying; the per-worker connection limit is shared by all tailnet logins for that worker.
- **`Permission denied (publickey)`:** Use the private key matching a configured `.pub` line for the worker; check the SSH username is `mindroom`.
