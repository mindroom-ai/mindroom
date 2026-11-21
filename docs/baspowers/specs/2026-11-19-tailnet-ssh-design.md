# SSH into your own worker over Tailscale (tailnet SSH)

Date: 2026-11-19. Status: decided by Bas ("let's build it"), ready to plan.
Code facts are from `mindroom` `origin/main` (branch `feat/tailnet-ssh`), `mindroom-chat` c65efdffe, and `mindroom-infra` `origin/main`.
Tailscale and sshd behaviour is from the feasibility spike (`tailscale-brainstorm/spike-report.md` in the DevAgent workspace, prototype in `spike-gateway/`), cited as "spike Q1" to "spike Q6": tsnet 1.104.7, Headscale 0.29.7, OpenSSH 10.0, the hosted worker seccomp profile under Docker.

> The multi-tailnet and portal parts are Later; [Single-tailnet v1 (2026-11-21)](#single-tailnet-v1-2026-11-21) supersedes them for v1.

## Intent

A person's worker is their computer in the cloud, yet they reach it only through the agent, the Chat terminal, and the Computer panel.
Tailnet SSH makes it a normal host on that person's own tailnet: `ssh mind-bas`, `scp`, `rsync`, `sshfs`, and local agents that run `ssh mind-bas '<cmd>'`.
Success means: after a one-minute setup `ssh <hostname>` opens a shell in the workspace their agent uses, also when the worker is asleep, and that shell can never be in a container that holds another person's files.

## Understanding

Said by Bas:

- The worker joins the person's tailnet by itself once it exists, the goal is SSH and file sharing with local agents, and the session lands only in that person's worker container with no other user's files visible.
- The six decisions in the brief: inbound only, a gateway outside the worker, login-link enrolment for hosted with an operator key for self-hosters, pasted public keys, deny-by-default eligibility, stable links with wake on connect.

Assumed here:

- "Belongs to that one person" allows read-only knowledge bases the operator assigned to the agent, and forbids every directory another person's agent can write (open question 1).
- One link per person and agent, one SSH key list per person, and one account name, `mindroom` (uid 1000 in `local/instances/deploy/Dockerfile.mindroom`).
- The SSH session is equal to the agent's shell in reach: it reads what uid 1000 reads in that container, which the person can already ask the agent to print.
- The setup card is part of the Connections portal, which the backend serves and Chat opens signed in (see Deviations).

## Architecture

```text
laptop ──WireGuard/DERP──▶ tailnet node "mind-bas" :22        (one tsnet node per link, in the gateway)
                             │ gateway: POST /links/{id}/connect ──▶ primary: check, ensure worker, start sshd
                             └─ raw TCP ──▶ worker :2222 sshd (uid 1000, public key only)
primary ◀── GET /links, POST /status ── gateway        portal card ──▶ primary /api/connections/tailnet-ssh
```

| Component | Where | New or reused |
| --- | --- | --- |
| Gateway | `tailnet-gateway/` (Go, `tailscale.com/tsnet`), one Deployment or container | New, about 400 lines; holds node keys, listens on tailnet :22 only, dials only what the primary names. |
| Link service | `src/mindroom/tailnet_ssh/` (`eligibility.py`, `store.py`, `projection.py`, `links.py`) | New; all decisions live here. |
| Internal listener | `src/mindroom/api/tailnet_ssh_gateway.py` on its own port | New, started as `serve_script_gateway_listener` is (`api/script_gateway.py`, `orchestrator.py`). |
| User API | `src/mindroom/api/tailnet_ssh.py` | New, shaped as `api/egress_credentials.py`. |
| Worker sshd | `src/mindroom/api/worker_ssh.py` in the runner, `openssh-server` in the image | New, shaped as `api/worker_computer.py` `/start`. |
| Worker lifecycle | `ensure_worker`, `touch_worker`, idle cleanup (`workers/backend.py`, `api/main.py`) | Reused unchanged. |
| Portal card | `frontend/src/connections/TailnetSsh.tsx` beside `ConnectedClients` | New, on the page that hosts Agent Keys. |
| Chart | `cluster/k8s/runtime/templates/tailnet-ssh.yaml` | New, shaped as `approved-egress.yaml` (own Deployment, selector labels, policies). |

The gateway lives in the mindroom repo as `tailnet-gateway/` with its own `go.mod`, Dockerfile, and a CI job that tests it and publishes `ghcr.io/mindroom-ai/mindroom-tailnet-gateway` under the release tag.
Its API contract, the chart, and the worker image change together, so one repo and one version keep them from drifting, and a second repo would add a release to coordinate for 400 lines.

The gateway is a pipe.
It never decides who may connect where: every accepted connection asks the primary, and it deletes node state only on the primary's explicit word.
It has no code path that dials a tailnet address, no shell, no Kubernetes or Docker access, and no worker storage.

### Gateway and primary

The primary serves three routes on its own port (`MINDROOM_TAILNET_SSH_PORT`, default 8769), never on the public API port.
The gateway authenticates with `Authorization: Bearer <MINDROOM_TAILNET_SSH_GATEWAY_TOKEN>`, compared in constant time; the feature is on when the primary has that token.
In Kubernetes only gateway pods may reach the port.

| Route | Purpose |
| --- | --- |
| `GET /links` | Desired state: `[{id, hostname, desired: "on" \| "suspended" \| "removing", allowed_login, closed_before, login_after}]`. |
| `POST /status` | Observed state of every node the gateway holds, plus `enrollment: "login" \| "auth_key"` for the gateway as a whole. |
| `POST /links/{id}/connect` | Called once per accepted and authorized TCP connection, with the peer's login and node name; answers `{host, port}` or refuses. |

Reconciliation, every 5 seconds and at start:

- `on`: the gateway runs a `tsnet.Server` with `Dir` `<state>/<id>` and the hostname, enrolling with the operator key when it has one and otherwise reporting the login URL.
  `Dir` is keyed by the link id and never by hostname: two servers on one `Dir` start without error and overwrite each other's state (spike Q1).
- It calls `Start()`, never `Up()`, and reads `BrowseToURL` and `State` from `WatchIPNBus`; `Running` means enrolled, after which `Status()` gives the DNS name, MagicDNS suffix, and login name (spike Q2).
- A node that falls back to `NeedsLogin` gets no new URL by itself, so the gateway calls `StartLoginInteractive` then, and again whenever the link's `login_after` moves.
- `suspended`: the node stays enrolled, new connections are refused, open ones are closed.
- `removing`: the gateway closes connections, logs the node out, deletes `<state>/<id>`, and reports `removed`, after which the primary drops or parks the record.
- A state directory the primary does not list is stopped and kept, never deleted, so an empty or failed answer cannot destroy enrolments.
- `closed_before` is a timestamp; the gateway closes every connection of the link opened before it, which is how a removed key cuts open sessions.

`POST /status` carries per link: `state` (`needs_login`, `needs_approval`, `running`, `removed`), `login_url`, `tailnet`, `dns_name`, `login_name`, `key_expiry`, `open_connections`, and `last_error`.
The gateway posts on every change and at least every 30 seconds.

`POST /links/{id}/connect` runs the connect sequence under Eligibility and may take as long as the worker's ready timeout (600 s hosted).
**Peer check.** On every accept the gateway calls `WhoIs` on the remote address and closes the connection unless the peer's login name equals the link's `allowed_login`, before any wake.
The spike showed why: a second user in the same Headscale tailnet reached the node under its default policy, and Tailscale's default ACL allows everything too (spike Q1, Q3).
Tagged devices have no login and are refused in v1 (open question 3).
**Wake.** The gateway then holds the connection and writes an RFC 4253 pre-banner line (`MindRoom: waking worker`) every 3 seconds until sshd answers.
Stock `ssh`, `scp`, `sftp`, and `rsync` waited 300 s with no client setting, and the lines, visible only with `ssh -v`, reset `ConnectTimeout` so configs with a timeout of 5 s or more also survive (spike Q5).
A refusal or timeout closes the connection.
The gateway allows six connect calls a minute per link.

### Portal and primary

Routes under `/api/connections/tailnet-ssh`, with `require_connections_user`, `CONNECTIONS_HEADERS`, and agent resolution through `resolve_connection_user` and `resolve_connection_agent` (`api/connection_agents.py`), so the requester and the worker key come from the session and never from the request body.

| Route | Purpose |
| --- | --- |
| `GET ""` | The requester's keys, links with their state, and each agent they may use with `eligible` or the reason it is not. |
| `PUT /keys` | Replace the requester's public keys. |
| `POST /keys/github` | Add the keys of the requester's connected GitHub account (`https://github.com/<login>.keys`, the login from `oauth/github.py`). |
| `PUT /agents/{agent}` | Create or re-enable the link with a hostname; refused with the reason when not eligible. |
| `POST /agents/{agent}/login` | Ask for a fresh login URL when the node needs one. |
| `DELETE /agents/{agent}` | Disconnect. |

`login_url` is returned only by `GET ""` to the link's owner.
No tool, Matrix event, notification, or log line carries it, and no agent-facing function returns link state in v1; an agent asked how to SSH in points to the Connections page.

## Data model

Links live in `tracking_dir(runtime_paths) / "tailnet_ssh.json"`, primary-only, written under `advisory_file_lock` with `write_json_file_durable` as `contacts/store.py` does.

| Field | Meaning |
| --- | --- |
| `id` | 16 random hex characters; the gateway's state directory name and the API path. |
| `requester_id`, `agent_name` | The person (canonical Matrix ID) and agent; unique together. |
| `worker_key` | The `user_agent` key resolved at creation; a link whose key no longer resolves the same is removed. |
| `hostname` | Chosen by the person, `[a-z0-9-]{1,63}`, default the agent name with `_` as `-`. |
| `desired` | `on`, `off`, or `removing`; `suspended` is computed, not stored, and `off` links are not sent to the gateway. |
| `host_key` | An Ed25519 host key pair made at creation with `cryptography`, so `known_hosts` survives worker restarts. |
| `enrolled_login` | The Tailscale login that first enrolled the node; it is `allowed_login`, and a later sign-in by another account is refused until Disconnect. |
| `closed_before`, `login_after` | Set when keys or eligibility change, and when the person asks for a fresh login URL. |
| `created_at`, `enrolled_at` | For the card and for dropping links never enrolled within 24 hours. |

The same file holds `keys: {requester_id: [public key lines]}`, at most 20 per person, each parsed with `cryptography`'s `load_ssh_public_key`.
It also holds `enrollment` (`login` or `auth_key`, what the gateway's last `POST /status` said, absent before its first), so after a primary restart an existing operator-key link admits its configured login and is parked on Disconnect before the gateway reports again; operator links for further workers are added only after a report in the new primary session, since the gateway may have switched to `login` meanwhile.
It also holds `seen_keys: {requester_id: [key identities]}`, every key each person with a link could sign in with at the last reconcile, so a key the operator removes from the config closes that person's sessions, also when a primary restart came with the change.
What the gateway reports (`state`, `login_url`, `dns_name`, `open_connections`) is kept in memory only and is empty until the gateway's first post after a primary restart.
Node keys exist only in the gateway's volume.

## Eligibility

`worker_scope` is a reuse rule, not a data partition.
`visible_workspace_roots` (`tool_system/worker_routing.py`) mounts `agents/<agent>/workspace` for every requester of a non-private agent, whatever its scope, and a `user` worker mounts every `user`-scoped agent's workspace.
So the rule names the workspace's owner, not only the scope.

A link of person P to agent A is eligible only when all of these hold:

1. The worker backend is dedicated, `docker` or `kubernetes` (`worker_backend_kind(...).dedicated`, the gate Worker Computer uses in `api/computers.py`).
2. A sets `tailnet_ssh: true`.
3. A's `effective_execution_scope` (`ResolvedAgentPolicy`) is `user_agent`.
4. A's workspace is P's alone: A is private (`private.per: user_agent`, a root per requester under `private_instances/`), or `resolve_responder_access(config, A)` admits exactly the one concrete user P, with no room grants and `current_room_members` false.
   The second form is what a generated personal agent has (`config/personal_agents.py` writes `members_of_rooms: []` and the owner into `users`).
5. P may use A, and the key is the one `resolve_connection_agent` resolves for P and A: scope `user_agent`, no subagent pin (`subagent_worker_state_scope_key` is `None`).
6. The worker's launch mounts no shared directory read-only, which is how a directory another agent owns appears (`plan_shared_directory_mounts`).
   A directory A owns passes, since only A writes it; assigned knowledge bases pass, read-only (open question 1).

Everything else is denied: `shared`, `user`, unscoped, subagent and script workers, `static_runner` and its local workers, agents reached through rooms, and created agents with co-owners.

Config surface:

```yaml
agents:
  research:
    private: {per: user_agent}
    tailnet_ssh: true          # default false; also valid in a personal_agents template's `agent:`
tailnet_ssh:
  logins:                      # required per person with an operator key: the Tailscale login the peer check admits
    "@bas:example.org": bas@github
  authorized_keys:             # optional, for installs where the operator knows everyone's key
    "@bas:example.org": ["ssh-ed25519 AAAA... bas@laptop"]
```

The rule is enforced in four places, all calling `tailnet_ssh/eligibility.py`:

- **Config load.** `tailnet_ssh: true` without scope `user_agent`, or on an agent that is neither private nor limited to `access.users` of concrete IDs, fails validation with a sentence naming the agent and the missing condition.
  The number of users is not checked at load, because a created agent's owner may add co-owners (`update_generated_agent`), which must suspend SSH, not make the config unloadable.
- **Launch.** Inside the worker's lock, both backends ask `ssh_eligible_launch(worker_key, launch, private_agent_names, shared_mounts)` with the mount plan they are about to apply (conditions 2 to 6, with P read from the key and accepted only when the key resolved for P and A is the same key).
  When true they mark the container: Kubernetes adds the pod label `mindroom.ai/tailnet-ssh: "true"` and port 2222 on the pod and its Service, Docker adds the label and publishes 2222 on `publish_host`.
  `WorkerHandle.ssh_endpoint` is set only from that mark.
  The mark and the mounts come from one plan, and a plan change replaces the container as any template drift does, so a running container can never have SSH and a foreign mount.
- **Connect.** Conditions 1 to 5 against the current config, then `ensure_worker(WorkerSpec(worker_key, private_agent_names))` under `lease_configured_primary_worker_manager` as `_resolve_worker` does, then `handle.ssh_endpoint` must be set, then the projection is written, then the runner starts sshd, then the endpoint is returned.
  The same conditions, with 6 planned for the key, give `suspended` on each `GET /links` and the reason on the card.
- **Network.** The Kubernetes policy admits the gateway only to pods carrying the label, on 2222.

The portal shows the failing condition as one of: `backend`, `not_enabled`, `scope`, `shared_workspace`, `joined_directory` (with its name), `not_authorized`.

## Worker changes

- **Image.** `openssh-server` and `rsync` join the apt line of `local/instances/deploy/Dockerfile.mindroom`; sshd then ran as uid 1000 with all capabilities dropped, a read-only root, and the hosted seccomp profile, with no denial (spike Q4).
- **Projection.** The primary writes `<worker root>/.runtime/ssh/` with `sshd_config`, `authorized_keys`, and the link's host key, the key 0600 and owned by uid 1000, which sshd requires and the primary's own uid gives.
  `.runtime` is already mounted read-only into the worker by both backends (`_scoped_storage_mounts` in `kubernetes_resources.py`, the worker-root binds in `docker.py`), so no mount changes and worker code cannot rewrite the files.
  It is written on connect and whenever that person's keys change, so a removed key stops working for new logins at once.
- **`authorized_keys`.** The person's keys from the portal and from `tailnet_ssh.authorized_keys`, each prefixed `restrict,pty`.
- **`sshd_config`**, the spike's working one: port 2222, the projected host key and key file, `PidFile none`, `UsePAM no`, `StrictModes yes`, public key only, `AllowUsers mindroom`, `PermitUserEnvironment no`, `PermitUserRC no`, `LoginGraceTime 60`, `Subsystem sftp internal-sftp`, and no TCP, stream-local, agent, X11, or tunnel forwarding.
- **Session.** `/ssh/start` carries a shell call's request for P and A, built outside any conversation (`build_requester_execution_identity`) as a terminal's is built inside one.
  The runner turns it into a `TerminalLaunch` with `prepare_terminal_launch` (`api/sandbox_runner.py`) and keeps that environment and directory in a 0600 file under `/tmp`.
  `ForceCommand` runs `python -m mindroom.tailnet_ssh.session`, which loads the file and runs the requested command, the SFTP server, or plain `bash`.
  Not `bash -l`: a login shell reads `/etc/profile`, which on the Debian image resets `PATH` and drops the agent CLI and the worker's virtualenv, so the session would lose the agent's shell environment; plain `bash` keeps the launch `PATH` and still reads the workspace's `.bashrc`.
  It is needed, not cosmetic: sshd refuses a uid without a passwd entry, the image's entry (`mindroom:x:1000:1000::/home/mindroom:/bin/sh`) gives `/bin/sh` and a home on the read-only root, so the module sets `HOME` and the start directory to the workspace (spike Q4).
  A custom worker image must keep a passwd entry for uid 1000.
  So `~` is the agent workspace, `scp notes.md mind-bas:` lands where the agent looks, and the shell's egress goes through the same proxy and Agent Keys as the agent's (open question 5).
- **Runner.** `POST <runner api>/ssh/start` with `validate_runner_token` stores the launch, starts `sshd -D -e -f <projection>` when none is running, and returns once the port accepts.
  sshd never starts at boot, so a worker has no SSH server until the primary has passed a connect for it.
- **Activity.** `active_tailnet_ssh_worker_keys(app)` returns the keys of links whose latest status, at most 90 seconds old, has open connections, and is passed beside `active_computer_worker_keys` into the two maintenance paths (`api/workers.py`, `api/main.py`), which call `touch_worker`.
  The gateway counts a connection once it is 75 seconds old, which is past `LoginGraceTime`, so only authenticated sessions keep a worker up; the connect itself already counts through `ensure_worker`.

## Link lifecycle

| State | Meaning | Leaves by |
| --- | --- | --- |
| `pending_login` | The node runs and waits for sign-in; the card has the link. | Sign-in, or removal after 24 hours. |
| `needs_approval` | The tailnet requires device approval or a tailnet-lock signature. | The tailnet's admin. |
| `enrolled` | In the tailnet; the card shows tailnet, login, and DNS name. Shown as `ready`, `asleep`, or `waking` from the worker handle. | Any row below. |
| `needs_login` | The node key expired or the machine was deleted in the Tailscale console. | "Reconnect": a new sign-in, same hostname and host key. |
| `suspended` | Not eligible now; connections are refused and closed, the node stays. | Eligibility returning, or removal. |
| `removing` | Logged out and state deleted by the gateway. | Record dropped, or parked as `off` in operator mode. |

- **Stable.** Nodes are not ephemeral; the hostname and host key belong to the link, so they survive worker restarts, scale to zero, and gateway restarts.
- **Operator key.** When the gateway reports `auth_key`, the primary adds a link, at most once a minute, for every listed worker whose handle has `ssh_endpoint` and no link, and nodes enrol without a sign-in; `allowed_login` is the person's entry in `tailnet_ssh.logins`, and without one every connection is refused.
  A person's Disconnect parks the link as `off` so it is not added again; "Connect" turns it back on.
- **Removal.** Disconnect; A leaving the config or roster; P no longer the owner or no longer authorized; the stored `worker_key` no longer the resolved one.
  All go through one function, `remove_links(...)`, which sets `removing` and `closed_before`.
- **Key removal.** Rewrites `authorized_keys` and sets `closed_before` on that person's links, which cuts open sessions within one poll.
- **Worker retirement.** Normal `user_agent` workers are never retired today; only script workers are (`retire_worker` in `script_runs/manager.py`), and they cannot have a link.
  The projection lives in the worker's state root and goes with it, and any later retirement path for normal workers must call `remove_links`.

## User experience

The card "SSH access over Tailscale" sits on the Connections portal, visible when the feature is on and the person has at least one agent with `tailnet_ssh: true`.

| Card state | Shows |
| --- | --- |
| No key | A field for a public key and "Import from GitHub" when a GitHub account is connected. |
| Not linked | Agent picker when there are several, hostname field, "Connect to my tailnet", and what a shell there can read. |
| Sign in | "Sign in to Tailscale" opening the login URL in a new tab; the card polls until enrolled. |
| Waiting for approval | "Ask your tailnet admin to approve `mind-bas`." |
| Connected | "Connected to `tail1234.ts.net` as `bas@github`", worker `ready` or `asleep`, the copy blocks below, Disconnect. |
| Needs sign-in | "Reconnect", with the reason. |
| Not available | The failing condition in a sentence, for example "Leave the shared directory `team/specs` to use SSH with this agent." |

```text
Host mind-bas
  HostName mind-bas.tail1234.ts.net
  User mindroom
  ForwardAgent no
  ForwardX11 no
  ServerAliveInterval 30
```

The card also offers the `known_hosts` line for the link's host key and the one-liner `ssh mindroom@mind-bas.tail1234.ts.net`.
A note says the first connection after the worker slept can take a minute or more, and that a `ConnectTimeout` below 5 seconds must go (spike Q5).
Showing the tailnet and login after sign-in is the check against a link opened in the wrong account; Disconnect undoes it.
With an operator key the card skips the sign-in state and shows the link as connected once the node has enrolled.

## Deployment

### Kubernetes

`cluster/k8s/runtime/values.yaml` gains:

```yaml
tailnetSsh:
  enabled: false
  port: 8769                   # the primary's internal listener
  image: {repository: ghcr.io/mindroom-ai/mindroom-tailnet-gateway, tag: ""}
  persistence: {size: 1Gi, storageClassName: ""}
  authKeySecret: ""            # Secret with key `auth-key`; empty means login links
  loginServer: ""              # Headscale
  tags: []                     # with an operator key
  httpsProxy: ""               # an HTTP CONNECT proxy for the gateway's only internet egress
  resources: {}                # about 25 MB plus 7 MB per link
```

`templates/tailnet-ssh.yaml` renders, when enabled:

- A Deployment with one replica and `strategy: Recreate`, because two processes must never hold one node key, with a ReadWriteOnce claim at `/state`.
  It runs non-root with a read-only root, all capabilities dropped, no service account token, and the chart's node placement for the control plane.
  It sets `TS_NO_LOGS_NO_SUPPORT=true`, without which tsnet uploads node logs to `log.tailscale.com`, and calls `netns.SetEnabled(false)`, without which every dial hangs on a network with no default route (spike Q6).
- A token Secret mounted into the primary and the gateway, created as `api-key-secret.yaml` creates its key.
- Three NetworkPolicies: worker pods carrying `mindroom.ai/tailnet-ssh: "true"` admit gateway pods on 2222; the primary admits gateway pods on `tailnetSsh.port`; the gateway admits nothing and may reach DNS, that port, those worker pods, and TCP 443 only: the proxy when `httpsProxy` is set (`HTTPS_PROXY`, honoured for control and DERP), public addresses otherwise.
  No UDP is allowed, so traffic is relayed by DERP, about 25 ms in the spike, which needs `controlplane.tailscale.com` and the `derp*.tailscale.com` hosts, or the Headscale URL and its DERP (spike Q6).
  The proxy must not be the workers' Squid, whose allowlist would then open those hosts to workers.
- `validate.yaml` requires `workers.backend: kubernetes` and a port distinct from `runtime.apiPort`, `scriptGateway.port`, and `egressBroker.port`.

The worker egress policy, Squid, Agent Keys, and `approvedEgress` are not touched: workers gain one ingress rule and no egress.

### Docker

The gateway is one more container that shares the primary's network namespace (Compose `network_mode: "service:<primary>"`, or `--network host` beside a primary on the host) with a named volume for `/state`.
So the primary's listener is `127.0.0.1:8769`, and a worker's published 2222 is reachable at the `endpoint_host` the primary already uses.
`TAILNET_SSH_AUTH_KEY`, `TAILNET_SSH_LOGIN_SERVER`, `TAILNET_SSH_TAGS`, and `HTTPS_PROXY` on the gateway select the operator key, Headscale, tags, and a proxy.

### Not supported

`static_runner` and the local workers inside it have no per-person container; the portal says `backend` and no link can exist.

## Security

- **Tenant isolation** rests on Eligibility: the container an SSH session enters was launched from a mount plan with one owner, the network admits the gateway only to such containers, and the primary names the target for each connection from the session-derived worker key.
- **Key custody.** Node keys are in the gateway's volume only; no worker has a mount of it, a Tailscale socket, or a route to the gateway, whose pod admits no ingress.
- **Direction.** The gateway only listens; nothing in a worker can open a connection to a tailnet, so the person's devices are not reachable by a prompt-injected agent.
- **The worker is not trusted by the client.** sshd runs as the agent's uid, so worker code can replace it and can plant shell startup files; the copy block disables agent and X11 forwarding, and the docs warn against `ssh -A` and VS Code Remote-SSH, whose model trusts the remote.
- **The operator is trusted.** The gateway holds a machine in each linked person's tailnet, which the card states; a person limits it with their tailnet's ACLs, and operator-key installs should use a tagged key.
- **Three layers admit a session:** the tailnet's ACL, the gateway's `WhoIs` check against the enrolling login, and the SSH key in the worker; a node also answers ping and a PeerAPI port that returned 403 to every request, and nothing else (spike Q1).

| Threat | Answer |
| --- | --- |
| A session shows another person's files | Eligibility at launch, connect, and network; `shared`, `user`, unscoped, and room-reached agents can never be marked. |
| Link created for someone else's agent or worker | Requester and key come from the portal session through `resolve_connection_agent`; no route takes a worker key or Matrix ID. |
| Agent reads or uses a node key | Not in the worker pod; no network path to the gateway. |
| Agent reaches the person's tailnet | No dial path in the gateway; worker egress unchanged. |
| Agent edits `authorized_keys`, `sshd_config`, or the host key | Read-only `.runtime` mount, written only by the primary. |
| Agent replaces sshd with one that admits any key | Reaches only that agent's own, already compromised worker, only from devices of the enrolling login; the client sees a host-key change. Accepted. |
| Gateway sends A's connection to B's worker | The target comes from `connect` for the listener's own link id; three nodes in two tailnets with overlapping IPs showed no cross-talk (spike Q1). |
| Worker or outsider calls the internal API | Own port, bearer token held by primary and gateway, policy admits gateway pods only, never on the public ingress. |
| Login URL leaks and another account enrols the node | Only on the owner's signed-in portal page; the card names tailnet and login; an SSH key is still required. |
| Another user or device in the tailnet reaches, wakes, or holds the worker | Refused by the `WhoIs` check before any wake; for the owner's own devices, six connects a minute, and unauthenticated connections end at 60 s and never count as activity. |
| Access outlives a revoke | Disconnect, key removal, and lost eligibility close open connections within one poll; `removing` logs the node out. |
| Data leaves past Squid over SSH | Only when the person's own client pulls it; forwarding is off, so the worker gains no outbound path. |
| Gateway compromised | Small Go program, no shell, non-root, read-only root, no ingress, egress policy; reach is each tailnet's ACL for that one device. |

## Failure modes

| Failure | Behaviour |
| --- | --- |
| Gateway down or restarting | SSH is unavailable and open sessions drop; agents and tools are unaffected; nodes return from `/state` without a new sign-in. |
| Primary down | New connections are refused; open sessions continue; no link state is deleted. |
| Worker fails to start in time | The held connection closes; the card shows `last_error`; the next attempt retries. |
| Worker replaced mid-session | The session drops; reconnecting lands in the new container with the same host key. |
| Tailscale control plane unreachable | Enrolled nodes keep serving known peers; sign-ins wait. |
| Node key expired or machine deleted | `needs_login`; "Reconnect" on the card. |
| Device approval or tailnet lock | `needs_approval` until the tailnet's admin acts. |
| Gateway volume lost | Every link shows `needs_login`; a re-enrolled node may get a `-1` name until the person deletes the old machine. |
| DERP or the proxy unreachable | Nodes stay enrolled but unreachable; there is no direct path to fall back to, by design. |
| Worker image without sshd | `/ssh/start` answers 503 and the card says the worker image lacks SSH. |
| Many links | Measured idle: 42 MB for one node, 159 MB for 20, so about 7 MB and 9 file descriptors per link; a restart restores nodes in about a second each. Hundreds fit one replica; thousands need lazy start or shards (out of scope). |

## Testing

- **Unit (pytest).** The eligibility matrix: every scope, private or not, each access shape, a joined and an owned shared directory, a subagent-pinned key, each backend kind.
  Config validation messages; store transitions and every removal trigger; operator-key links and the `off` parking.
  User API: another person's link is invisible, `login_url` goes only to the owner, keys are parsed, GitHub import is mocked.
  Internal listener: token check, desired state, and the connect order against a fake backend, including that no runner call happens when `ssh_endpoint` is unset.
  Projection file contents; activity touches in both maintenance paths.
- **Backends.** The launch fixtures in `tests/fixtures/dedicated_worker_launch/`: an opted-in `user_agent` launch gains the label and port, and the `user`, `shared`, and unscoped fixtures must not change; the same for Docker launches.
- **Chart.** Render tests beside `tests/test_runtime_chart_approved_egress_*.py` for the Deployment, the three policies, and `validate.yaml`.
- **Gateway (`go test`).** Reconcile against a fake primary: unlisted state is kept, `removing` deletes, `closed_before` closes, the peer check, the pre-banner lines, the connect rate limit, the pipe.
- **Portal (vitest).** Each card state, and that the login URL is rendered only as a link target.
- **Integration with Headscale** (Docker, marked `e2e`, its own CI job): Headscale, the gateway image, the worker image run with the Docker backend's security options and the worker seccomp profile, a stock `tailscale/tailscale` userspace client, and a fake of the three primary routes, so no model or Matrix is needed.
  Cases: enrol by login URL (`headscale auth register`) and by pre-auth key; a second user in the same tailnet is refused; exec, PTY, `scp`, `sftp`, `rsync`; forwarding refused; wrong key refused; key removal cuts an open session; two links under two Headscale users where neither client reaches the other's node; a connection held through a 120 s delayed start.
- **Kubernetes** is covered by the render tests and, before hosted enablement, by the live run below repeated on the staging stack with two real Tailscale accounts.

### Live run

`live.py` starts `scripts/agent-stack/stack.py` on `static_runner`, Docker workers exist there only behind `--computers`, and it has no Kubernetes path.
PR 3 therefore adds `stack.py --tailnet-ssh` and a pass-through flag in `live.py`:

```sh
~/repos/dev/scripts/live.py up --backend ~/repos/worktrees/mindroom-tailnet-ssh --tailnet-ssh
```

- The option implies `--client-sessions` and Docker workers, reusing `build_worker_image` and `option_env`.
- The helper gets `worker_scope: user_agent`, `tailnet_ssh: true`, and `access.users` of the slot's one test user.
- It starts Headscale on a slot port with a user `live`, the gateway with `--network host` and that login server, and a `tailscale/tailscale` userspace client joined as the same user by pre-auth key.
- It prints `docker exec <headscale> headscale auth register --auth-id <id from the sign-in page> --user live` for the card's sign-in step, and `ssh -o ProxyCommand='docker exec -i <client> tailscale nc %h %p' mindroom@<hostname>`.

The run: add a key and connect on the portal, `scp` a file in, ask the helper to read it, stop the worker container and `ssh` again to see the wake, then Disconnect and watch the session drop.

## Rollout and pull requests

The feature is off until the primary has the gateway token, and then still off for every agent without `tailnet_ssh: true`.

1. **mindroom: backend, worker, gateway, chart.** The config field and validation, `src/mindroom/tailnet_ssh/`, both APIs, the runner route, the backend marks, the image packages, `tailnet-gateway/` with its CI image, the chart template, values, validation and README, the unit, chart, Go, and Headscale tests, and the row in `docs/architecture/security-posture.md`.
   Usable through the API with no UI.
2. **mindroom: portal card and docs.** `frontend/src/connections/TailnetSsh.tsx` with its tests, a user page `docs/tailnet-ssh.md` (setup, the config field, operator key and Headscale, what a shell can read, the forwarding warning), and a line in `docs/connections-portal.md`.
3. **mindroom-chat: live and e2e harness.** `stack.py --tailnet-ssh`, one e2e spec that drives the card, and the `live.py` flag in the dev repo.
   Chat's UI does not change: it already opens the portal signed in.
4. **mindroom-infra: hosted enablement**, after a release holding 1 and 2.
   The chart version, `tailnetSsh` in `helm_release.mindroom` (`stack/mindroom.tf`), the gateway image in `scripts/images` and `stack/images.tf`, `tailnet_ssh: true` under `mind.agent` in `stack/mindroom-config/personal_agents.yaml`, and a stack test for the policies; staging first, with two accounts in two tailnets.

## Out of scope

- Any path from a worker into a tailnet, and any Tailscale domain on the worker allowlist.
- Tailscale SSH, Taildrop, Funnel, exit nodes, subnet routes, and operator-tailnet device sharing.
- Keyless login by Tailscale identity, port forwarding, and dev-server preview over the tailnet.
- `shared` and `user` workers, subagent and script workers, `static_runner`.
- An agent tool that reports or changes links, a Chat-native card, and an audit view; v1 logs one line per connect with the link id and the peer's node and login, never commands or the login URL.

## Open questions

1. **Do assigned knowledge bases break "only my files"?** Recommended: no; they are operator-assigned and read-only, the card lists them, and the hosted Mind (`knowledge_bases: [allpurpose]`) would otherwise not qualify. 75%.
2. **May an agent that owns a shared directory qualify?** Recommended: yes; only the owning agent writes it, so it holds nothing from another person. 80%.
3. **Tagged devices.** The peer check admits only the enrolling login, so a person's tagged servers are refused. Recommended: keep that in v1 and add a per-link list of allowed tags when someone needs it. 75%.
4. **Operator OAuth client.** Recommended: v1 documents a reusable, tagged auth key and accepts an OAuth client secret only if `tsnet` takes one in `AuthKey` unchanged, which the spike does not cover. 60%.
5. **Session environment.** Recommended: build the shell request outside a conversation so SSH shells get the agent's proxy and Agent Keys; if `prepare_terminal_launch` cannot take such a request cheaply, v1 ships the workspace home contract alone. 70%.
6. **Default hostname.** Recommended: the agent name with dashes (`mind-bas`); Tailscale appends `-1` on a clash and the card shows the real name. 85%.
7. **Hosted egress for the gateway.** Recommended: direct TCP 443 by NetworkPolicy in v1; a dedicated allowlisting proxy (`httpsProxy`) when the hosts must be pinned. 70%.
8. **Unverified by the spike:** tailnet name, login format, AuthURL lifetime, and key expiry on real Tailscale; non-OpenSSH clients with pre-banner lines; the `RuntimeDefault` seccomp profile and `fsGroup` on the host-key mode in Kubernetes; memory with large tailnets. Recommended: check each on staging in PR 4, and the Kubernetes two in PR 1's kind smoke. 80%.

## Deviations from the brief

- The card ships in the mindroom repo, because the Connections page that hosts Agent Keys is the backend's portal (`frontend/src/connections/`), which Chat only opens signed in (`src/app/connections/ConnectionsPage.tsx`); the mindroom-chat pull request is the test harness.
- sshd starts on a link's first connect, not at runner boot, so a worker never runs it before the primary has passed a connect.
- Removing a key cuts access and open sessions but keeps the link, so adding a key again needs no new sign-in.
- "Worker retirement" has no hook today for workers that can have a link; the rule is stated under Link lifecycle.
- `suspended` is added between enrolled and removed, so a temporary loss of eligibility does not cost the person a new sign-in.

## Checklist

- [x] Spike report folded in.
- [ ] Spec committed.
- [ ] PR 1: eligibility tests red, then `tailnet_ssh/`, APIs, runner route, backend marks, image, gateway, chart.
- [ ] PR 1: Headscale integration job green, including the two-tailnet case.
- [ ] PR 2: card and user docs.
- [ ] PR 3: `stack.py --tailnet-ssh`, e2e spec, `live.py` flag; live run recorded.
- [ ] PR 4: staging with two real tailnets, then production.

## Single-tailnet v1 (2026-11-21)

Bas decided that v1 uses one operator-managed Tailscale or Headscale tailnet for the whole installation.
The existing operator-key mode becomes the only mode: the gateway automatically enrolls a node for every eligible worker with one reusable auth key, and the operator supplies each person's login and SSH public keys in config.
There is no user enrollment or management UI in v1.

### Link identity and data flow

- **Discover, then persist.** On the first `GET /links` and at most once a minute for discovery, scan `list_workers()` for marked handles with `ssh_endpoint`.
  Use `user_agent_worker_key_requester`, `worker_key_agent_name` and `normalize_worker_key_part` to identify the pair; mint a link only if the exact normal, unpinned key resolves to that pair and it is eligible.
  This discovers normal worker launches without starting containers or treating login/key mappings as a roster; missing credentials deny SSH, not enrollment.
  Keep a primary-only `tracking/tailnet_ssh.json` ledger with `{id, requester_id, agent_name, hostname, host_key}` per pair, with 16-hex IDs, Ed25519 host keys and locked durable writes on change.
  This is smaller than enumerating requesters from config plus separate persistence for node IDs, names and host keys; derive worker keys at runtime, and keep links through idle cleanup and failed lists.
- **Name once.** Normalize the agent name to a lowercase DNS label (`_` and other non-alphanumeric runs to `-`, trim edge hyphens, at most 63 chars, fallback `worker`), e.g. `mind_bas` → `mind-bas`.
  If reserved, append the requester's normalized Matrix localpart (`research-alice`); if still reserved (e.g. `@alice:other.org`), append a short stable hash of the full Matrix ID and raw agent name, extending it on collision and truncating the prefix to fit.
  Allocate new claims in sorted `(agent_name, requester_id)` order under the store lock, reserving existing names even when unlisted or retiring.
  Log the link ID and hostname in the gateway; use `ssh mindroom@<hostname>`, checking the actual MagicDNS name in the control plane on an external clash and pinning the derived public host key in `known_hosts`.
- **Enroll automatically.** Pass the required reusable non-ephemeral `TAILNET_SSH_AUTH_KEY` to each `tsnet.Server.AuthKey` with `Ephemeral: false`; optional `TAILNET_SSH_LOGIN_SERVER` sets `ControlURL` for Headscale and `TAILNET_SSH_TAGS` sets `AdvertiseTags` for server nodes, not client admission.
  Start each node and listen on TCP `:22`, reusing its own `<TAILNET_SSH_STATE_DIR or /state>/<id>` on restart.
  Fail startup on missing key; invalid/expired keys, approval or tailnet-lock trouble are operator problems with no interactive fallback, and rotating a key does not move enrolled nodes to another tailnet.
- **List, omit, or retire.** `GET /links` returns `{id, hostname, desired, allowed_login, access_revision}` with `on` for eligible links.
  Omit an ineligible link; the gateway cancels its wakes and pipes on the next successful 5 s reconcile and stops the node without deleting its state.
  When eligibility returns, reuse its ID, hostname, host key and state directory, even if its worker is asleep.
  An actual roster deletion is final for that link ID: record its ID durably in `retiring_ids`, omit it until keys are cleared or there is no live SSH endpoint, then send `removing` until the gateway acknowledges `removed` after best-effort logout and successful state-directory deletion.
  A restored agent never reactivates a retiring ID; once that retirement completes, discover it as a new link with a fresh ID and host key, so an old acknowledgement cannot delete it.
  Reject a failed or unparseable response as a whole, leaving state unchanged; in a decoded list from the trusted primary, skip a bad row (invalid ID or unknown `desired`) and reconcile the valid rows.
  A valid empty list stops all nodes but keeps state.
- **Revoke worker access and close pipes.** When a live current-backend worker loses eligibility, is deleted, or loses its last login/key, clear `authorized_keys` before requesting the token-gated `/ssh/stop`; derive its unpinned key from the ledger even if scope changed, and retry failed lists or clears while the row is present.
  No runner call is needed for a stopped worker; new connects always project current keys before starting sshd.
  Serialize projection writes and revocation clears for the same worker; `/ssh/start` refuses an empty key file and a late `/ssh/stop` cannot kill a newer admission with keys restored.
  Stops are bounded and best effort; stopping the listener does not close established sessions, so the gateway closes their transports.
  Replace `closed_before` and `seen_keys` with `access_revision`, a digest of the configured login and sorted set of normalized key identities (type and blob, not comments); a changed digest, omission or `removing` closes all existing pipes for the link.
  New connects fail during a primary outage, but existing sessions can outlive it; if revoke and restore yield the same digest before any successful poll observes the change, those pipes stay open in v1.
- **Report activity, not enrollment.** `POST /status` reports only `{open_connections: {link_id: count}, removed: [link_id, ...]}`; drop `pending_login`, `needs_login`, `needs_approval`, `enrolled`/`running`, `suspended`, `off` and card-only worker-readiness states.
  One non-overlapping status publisher reports on change and at least every 30 s, independently of a slow reconcile; both maintenance paths touch workers from known IDs and reports no older than 90 s.
  Count only pipes at least 75 s past reaching sshd (`LoginGraceTime 60`); this is an activity heuristic, not an authentication signal.
- **Connect and wake.** `carry` calls `WhoIs` on the remote address and `admit` compares a nonempty login exactly with `tailnet_ssh.logins[requester_id]` before a wake or pre-banner.
  Unknown, tagged, unmapped or mismatched clients get no primary connect call; keep six calls per minute per link.
  On the bearer-only `POST /links/{id}/connect` (`peer_login`, `peer_node`), check current login and `link_ineligibility`, resolve the same worker as the agent's tool call, wake it with `ensure_worker` and require a ready marked `ssh_endpoint`.
  After wake, under the projection lock reread the current committed config with `read_app_committed_runtime_config` and recheck login, eligibility, target and keys; refuse a stale target/backend or revoked admission before writing a projection or calling `/ssh/start`.
  Send the normal agent-shell launch to `/ssh/start` and then return `{host, port}`; the gateway writes `MindRoom: waking worker` every 3 s until sshd's first bytes and pipes only to the returned endpoint.

### Isolation

Keep all four gates: `config_error` at load, `ssh_eligible_launch` on the same mount plan as the label and ports, `link_ineligibility` and exact marked target at connect, and gateway-to-marked-worker Kubernetes policy.
At both config load and launch, refuse opt-in if `normalize_worker_key_part(agent_name) != agent_name`: otherwise a lone private `mine_` can resolve as `mine` and `visible_workspace_roots` can select a non-private root.
Rename such an agent before opting in.
Reject ambiguous worker keys independently of hostname allocation.
Require an unpinned `user_agent` Docker or Kubernetes worker and `tailnet_ssh: true`, with a per-requester private workspace or exactly one concrete user and no room grants for a non-private agent.
Require requester authorization and no joined shared-directory mounts; assigned read-only knowledge bases and owned shared directories still qualify.
Under permissive ACLs, every tailnet member can reach every link node's `:22`; isolation depends on `WhoIs` before wake, eligibility and binding by link ID to the exact worker, and only that requester's config keys in `authorized_keys` (`restrict,pty`).
SSH reaches what the agent's shell reaches, with the same environment/workspace and no forwarding; the operator and control plane are trusted, and neither the gateway token nor its node volume goes to workers.

### Documented limits

The operator setup page must state:

- Reassigning a non-private sole-user agent hands its existing workspace to the new user, through SSH as well as through the agent; wipe or move the data first if that is not intended.
- Turn off `tailnet_ssh` and let revocation finish before switching worker backend, tenant or namespace; new connects dial only current-backend endpoints and v1 does not clear stale keys from old workers.
- A hostname can be reused after deletion, including by another requester; the new link gets a fresh ID and host key.
  SSH warns if a permitted client has the old key pinned, and `WhoIs` refuses the old requester if the name now belongs to someone else.
- Hosted enablement needs both the primary and worker ingress NetworkPolicies, or equivalent enforced isolation, in addition to the gateway policy; otherwise in-cluster pods can reach worker sshd (still key-only).
  The runner port must not be `2222`, and none of the internal ports belong on public ingress.

**Later.** Per-person tailnets, login-link enrollment and the Connections portal card can return together; there is no user API, GitHub import or Task 13 in v1.
Cut over any existing branch deployment by revoking old worker access and retiring old nodes, then starting clean v1 state; verify real Tailscale login strings, tag policy and auth-key expiry/recovery before hosted enablement.

### Deletion checklist by file

Implementation follow-ups only.
Measure acceptance with `shrink/count.sh` from the worktree (`origin/main...HEAD`, added minus deleted): baseline **4,910 production net lines**, including the kind harness, both chart/kind READMEs, workflows and `tach.toml`.
The filter excludes `docs/`, `skills/`, tests, fixtures, Go testdata, `go.sum` and `uv.lock`.
The targets below require consolidating the kind harness, removing sshd stderr capture, and simplifying gateway/reconcile, chart and CI wiring without dropping their checks; moving code across the counting boundary is not a cut.

| Production file | Action | Target net lines |
| --- | --- | ---: |
| `src/mindroom/api/tailnet_ssh.py` | **Delete** the entire public router at `/api/connections/tailnet-ssh`: `GET` (`tailnet_ssh_overview`), `PUT /keys` (`put_keys`), `POST /keys/github` (`import_github_keys`), `PUT /agents/{agent_name}` (`connect_agent`), `POST /agents/{agent_name}/login` (`request_login`), and `DELETE /agents/{agent_name}` (`disconnect_agent`); delete `_link_row`, `_overview`, `_change_keys`, `_own_link`, `_github_login`, `_fetch_github_keys`, and their portal auth, hostname input, and login URL handling with it; leave the shared Connections and GitHub OAuth infrastructure alone. | 0 |
| `src/mindroom/api/tailnet_ssh_gateway.py` | **Shrink** to the three routes, constant-time token gate, committed-config loader and listener lifecycle. Read app state directly in `active_tailnet_ssh_worker_keys` and remove the now-unused `tailnet_ssh_status` accessor/export, not the status used by maintenance. Use one generic non-echoing body validator; preserve auth before parsing/routing and config `503` before parsing. | 127 |
| `src/mindroom/api/main.py` | **Shrink**: remove `tailnet_ssh_router` import and `include_router`; keep SSH activity in `_worker_cleanup_loop` / `_cleanup_workers_once`. | 1 |
| `src/mindroom/api/app_state.py` | **Keep** the minimal `tailnet_ssh_status` app state for the fresh activity report. | 3 |
| `src/mindroom/api/workers.py` | **Keep** `maintain_leased_workers` / `cleanup_idle_workers` touching SSH and Computer keys. | 2 |
| `src/mindroom/api/sandbox_runner_app.py` | **Keep** worker SSH router and lifespan cleanup. | 4 |
| `src/mindroom/api/worker_ssh.py` | **Shrink** `_StderrTail`, `_Sshd` and custom pipe/drain code: inherit runner stderr and return a generic startup `503`, rather than returning log lines. Keep `start`, `stop`, shutdown, runner token, serialized readiness and process reaping, `prepare_terminal_launch`, nonempty-key start and late-stop guard. | 175 |
| `src/mindroom/config/agent.py` | **Keep** `AgentConfig.tailnet_ssh` (default `false`), reword its description for one tailnet. | 7 |
| `src/mindroom/config/main.py` | **Keep** `Config.tailnet_ssh`, `_OPTIONAL_SECTION_NAMES`, and `validate_tailnet_ssh_agents`. | 20 |
| `src/mindroom/config/tailnet_ssh.py` | **Shrink** wording and portal-paste diagnostics; keep `TailnetSshConfig.logins`, `.authorized_keys`, concrete Matrix ID and nonblank-login checks, and `normalize_public_key` rejecting private keys, options and malformed lines. | 85 |
| `src/mindroom/orchestration/config_updates.py` | **Keep** the live opt-in field. | 1 |
| `src/mindroom/orchestrator.py` | **Keep** internal listener lifecycle in `_run_api_server`. | 8 |
| `src/mindroom/runtime_env_policy.py` | **Keep** `MINDROOM_TAILNET_SSH_GATEWAY_TOKEN` exclusions. | 2 |
| `src/mindroom/tailnet_ssh/__init__.py` | **Keep** package marker. | 1 |
| `src/mindroom/tailnet_ssh/eligibility.py` | **Shrink**: delete `_Ineligible.detail` and joined-directory name formatting with the portal; retain `.pending` only for connect’s retryable `503` (both known and pending denies are omitted). Keep `config_error`, `ssh_eligible_launch`, `link_ineligibility` and the current isolation tests; add exact-name round-trip refusal at load and launch. | 190 |
| `src/mindroom/tailnet_ssh/links.py` | **Shrink** `desired_links` and `record_status`: use one ledger pass for discovery, projection refresh, revocation and retirement; replace `LinkReport`/`LinkStatus` and mode fields with counts and removed IDs. Delete `_UNENROLLED_SECONDS`, `link_state`, `listed_workers`, portal `refresh_projections`, enrollment branching and separate revoked-worker queue. Reuse one admission check before and after wake with a fresh committed-config read under the projection lock (login, eligibility, exact target and keys); keep the ready endpoint, `_shell_call` with `unbound_agent_cli_shell_env`, shared bounded start/stop POST, batched best-effort stops and activity freshness. | 320 |
| `src/mindroom/tailnet_ssh/store.py` | **Shrink** `Link` to the five identity fields: drop `worker_key`, `desired`, `host_public_key`, `enrolled_login`, `closed_before`, `login_after`, `created_at`, `enrolled_at`, `park_when_removed`. Delete `LinkBook.keys`, `.enrollment`, `.seen_keys`, `.revoked_workers`, `set_keys`, `_MAX_KEYS` and `close_sessions_of_gone_keys`. Replace `remove_links`/`finish_removal` parking with a durable `retiring_ids` set and acknowledgement by exact ID; keep `default_hostname`/`new_link` allocation, lock, durable writes and host-key-safe parse errors. | 125 |
| `src/mindroom/tailnet_ssh/projection.py` | **Shrink** `signin_keys` / `authorized_key_lines` to config-only deduplication by key identity; drop the `LinkBook` argument; keep atomic `write_projection`, `clear_authorized_keys`, hardened `sshd_config`, `restrict,pty` and mode `0600` host key. | 72 |
| `src/mindroom/tailnet_ssh/session.py` | **Keep** the stdlib `ForceCommand`, agent shell env/PATH/proxy/Agent Keys, workspace `HOME` and cwd, non-login Bash, exec and SFTP. | 60 |
| `src/mindroom/tool_system/sandbox_proxy.py` | **Keep** `worker_call` and `worker_runner_address` for authenticated start/stop. | 9 |
| `src/mindroom/tool_system/worker_routing.py` | **Keep** canonical requester decoding, round-trip and script/subagent pin refusal. | 31 |
| `src/mindroom/workers/models.py` | **Keep** `LABEL_TAILNET_SSH`, `WORKER_SSH_PORT`, `WorkerSshEndpoint` and `WorkerHandle.ssh_endpoint`. | 17 |
| `src/mindroom/workers/backends/_lifecycle.py` | **Keep** endpoint propagation through `worker_handle`. | 2 |
| `src/mindroom/workers/backends/docker.py` | **Keep** `_ssh_marked`, shared mount plan, launch hash and `_container_host_port`/handle logic; no wildcard publish and no endpoint without the mark. | 77 |
| `src/mindroom/workers/backends/docker_config.py` | **Keep** reserved SSH label. | 3 |
| `src/mindroom/workers/backends/kubernetes.py` | **Keep** rendered mark → Service and handle, ready-cache mark recheck and drift handling. | 39 |
| `src/mindroom/workers/backends/kubernetes_config.py` | **Keep** reserved SSH label validation. | 4 |
| `src/mindroom/workers/backends/kubernetes_resources.py` | **Keep** `ssh_marked`, one mount plan, `_AppliedDeployment`, label and port rendering, and the client-37 `application/merge-patch+json` Service **and** Secret patch calls. | 36 |
| `tailnet-gateway/main.go` | **Shrink**: delete `enrollment` selection and log field; require `TAILNET_SSH_AUTH_KEY`, keep tag parsing, server option, persistent state defaults and constrained network/log setup. | 55 |
| `tailnet-gateway/node.go` | **Shrink** `NewTsnetNode` to noninteractive startup; delete `tsnetNode.watch`, `observe`, `filled`, `Observed`, `Relogin` / `StartLoginInteractive`, `BrowseToURL` and approval/tailnet-lock polling. Keep `Listener`, `WhoIs`, `peer`, `Logout` and `Close`; no sign-in messages or auth secrets in logs. | 76 |
| `tailnet-gateway/primary.go` | **Shrink** `DesiredLink` (drop `LoginAfter`, replace `ClosedBefore`), remove `LinkStatus`/enrollment fields and have `PostStatus` send only counts and acknowledgements. Use one bounded JSON request/response helper for all calls; keep bearer, no proxy, timeouts, non-null list and connect target checks. | 100 |
| `tailnet-gateway/reconcile.go` | **Shrink** `Node`/`NewGateway`/`keep`/`Status`/`Reconcile`: drop `NodeObserved`, `enrollment`, `relogin`, `suspended`, card-only `lastError` and the reconcile/report rendezvous in `Run`; use a status tick with a coalesced notification from the serialized reconcile loop. Keep early cancellation, one status writer, bad-row omission, unlisted state retention, `remove`/`drop`, per-ID dirs and hostname logs. | 203 |
| `tailnet-gateway/serve.go` | **Shrink** `settle` and `lastError` to logging; use connection deadlines and context cancellation instead of extra timeout goroutines for banner writes and the worker’s first bytes, then clear those deadlines for the pipe. Keep `carry`, `admit`, `wake`, direct worker dial, rate limit, pre-banner, age accounting and revoke-during-wake/dial behavior. | 190 |
| `tailnet-gateway/Dockerfile` | **Keep** gateway image and non-root runtime. | 23 |
| `tailnet-gateway/go.mod` | **Keep** tsnet module; tidy dependencies after imports shrink. | 51 |
| `cluster/k8s/runtime/templates/tailnet-ssh.yaml` | **Shrink** auth-key env to a required Secret ref (gateway only), consolidate repeated env/label rendering, and use shared chart helpers for repeated policy peers/ports where they fit. Keep the internal Service, token Secret, PVC, Recreate Deployment security and all three policies with unchanged selectors and egress. | 220 |
| `cluster/k8s/runtime/templates/deployment.yaml` | **Keep** primary listener port and token wiring only. | 15 |
| `cluster/k8s/runtime/templates/validate.yaml` | **Keep** backend/port/egress checks and require nonblank `tailnetSsh.authKeySecret` when enabled. | 14 |
| `cluster/k8s/runtime/values.yaml` | **Keep** `tailnetSsh.enabled` default `false` and `authKeySecret: ""` as the disabled default only, not login-link mode; keep `port`, `image`, `persistence`, `loginServer`, `tags`, `resources`, `httpsProxy`, `extraEgress`. | 16 |
| `.github/workflows/build-mindroom.yml` | **Keep** gateway image build and publishing. | 4 |
| `.github/workflows/tailnet-gateway.yml` | **Shrink** the duplicated Headscale build steps into one Buildx loop (both branch images, existing read caches); let `uv run` install dependencies. Keep pinned actions, LFS, Go format/vet/race tests, Headscale job and its permissions/timeouts. | 110 |
| `.github/workflows/smoke-stacks.yml` | **Keep** kind SSH worker smoke step. | 7 |
| `local/instances/deploy/Dockerfile.mindroom` | **Keep** `openssh-server` and `rsync`. | 1 |
| `pyproject.toml` | **Keep** `kubernetes>=37` (one line replaced). | 0 |
| `tach.toml` | **Shrink**: delete `mindroom.api.tailnet_ssh` module, main dependency, gateway visibility and portal-only dependency/visibility edges; remove the store's config-key import edge, and reconcile remaining edges without widening visibility. | 68 |
| `vulture_whitelist.py` | **Shrink**: remove the six portal route entries and `_StderrTail` callbacks; keep validators, internal route entries and Kubernetes client-37 cleanup. | 0 |
| `cluster/k8s/kind/smoke_tailnet_ssh_worker.py` | **Shrink** by sharing a pod builder and consolidating apply/wait/diagnostics; keep named kind checks, only delete the created namespace, and derive both pod identities from chart templates. Render with a dummy `authKeySecret`; filter `TAILNET_SSH_AUTH_KEY` out in `_pod_manifests` for the stand-in and never start the real gateway. | 154 |
| `cluster/k8s/kind/tailnet_ssh_gateway_stand_in.py` | **Shrink** the retry/dial and read code; let connection failures reach pod logs instead of relaying custom errors. Keep the chart-labelled pod’s probe through the worker Service. | 38 |
| `cluster/k8s/kind/tailnet_ssh_worker_probe.py` | **Shrink** by replacing `_Cluster` and duplicate mark/unmark dictionaries with one read/assert helper and a three-phase loop; reuse agent-shell launch helpers and drop the non-asserting primary reachability check. Keep the real Service UID/port checks, worker mark, pod uid/fsGroup/seccomp, `/ssh/start` and banner via stand-in. | 144 |
| `cluster/k8s/runtime/README.md` | **Shrink** to one key setup, config-only credentials, hostname, documented limits and recovery; link to operator docs for examples rather than duplicating proxy and policy prose. | 39 |
| `cluster/k8s/kind/README.md` | **Shrink** to the smoke command and its context/namespace guard. | 4 |
| **Expected production total** | **Budget: ≤ 3,000 net lines** | **2,953** |

#### Tests and fixtures (excluded from line total)

| File | Action |
| --- | --- |
| `tests/api/test_tailnet_ssh_api.py` | **Delete** the portal suite (including login URL visibility, key paste/import, Connect/Reconnect/Disconnect, `off`, card states); carry its security outcomes for own-worker routing, key revocation and clearing/stopping into the internal and link tests, not the deleted routes. |
| `tests/test_tailnet_ssh_store.py` | **Shrink**: drop `test_set_keys_splits_pasted_lines_dedupes_and_caps_at_twenty` and `test_removing_a_key_sets_closed_before_adding_does_not`; replace `test_remove_links_then_finish_drops_or_parks` with final retirement and acknowledgement by ID. Keep host key, safe durable storage, pair uniqueness, and add cross-domain/normalization/truncation hostname collisions. |
| `tests/test_tailnet_ssh_links.py` | **Shrink**: delete `test_first_running_report_pins_enrolled_login`, `test_operator_key_enrolment_pins_no_login`, `test_operator_mode_survives_a_primary_restart`, `test_a_node_waiting_for_approval_is_not_dropped_at_a_day`, `test_link_state_names_what_the_card_shows`, `test_a_stored_operator_mode_adds_no_link_before_the_gateway_reports`, `test_login_mode_adds_no_links` and `test_a_key_still_in_the_portal_or_already_closed_for_closes_nothing_more`; drop URL-specific log assertions. Replace `off`/24 h/`suspended` tests with omission/reuse, discovery without status, and delete→restore→delete proving old acknowledgements cannot retire new IDs. Add a test replacing committed config during wake and verifying the resumed connect rechecks admission, writes no stale keys and does not call `/ssh/start` on refusal. **Must keep** primary→own-worker/endpoint binding (`test_resolve_target_is_the_persons_own_worker`, `test_connect_order`, `test_connect_without_ssh_endpoint_calls_no_runner`, shell key match), ambiguous-key and wrong/missing/empty-login denial, pending-membership `503`, key-change revocation, clear/retry/late-stop and fresh activity. |
| `tests/api/test_tailnet_ssh_gateway_api.py` | **Shrink**: replace `test_get_links_suspends_a_room_granted_link_while_membership_is_pending` with omission, strip old status/enrollment and login-URL-specific validation fixtures; keep non-echoing errors, token-before-body, precisely three routes, configured/unconfigured listener, connect endpoint/refusal, key cleanup, wire fixtures, no token in worker env, and both maintenance paths. |
| `tests/test_tailnet_ssh_eligibility.py` | **Keep** load/launch matrix, another person’s key, ambiguity, private/concrete access, directory rules, non-dedicated/pinned-worker denials and key validation; remove portal detail assertions. Add a lone private `mine_` regression: load refuses opt-in and launch refuses the mark even if load validation is bypassed. |
| `tests/test_tailnet_ssh_projection.py` | **Shrink** `test_authorized_keys_are_restricted_and_merged` to config-only keys, deduped and isolated per requester; keep content/modes, host-key match, rewrite and clear. |
| `tests/api/test_worker_ssh.py` | **Shrink** stderr-tail assertions into generic startup `503` and prompt cleanup/restart despite live sessions. Keep runner token, environment/workspace, concurrency, cancellation, late-stop and fail-closed empty keys; give start fixtures a key projection. |
| `tests/test_docker_worker_backend.py` | **Keep** SSH mark, endpoint requires label/running container, restricted publish address, mount drift and pinned-worker denials. |
| `tests/test_kubernetes_worker_backend.py` | **Keep** marked/unmarked pod and Service ports in both directions, endpoint/label and ready-cache checks, foreign-mount/ambiguous/pinned denials, and `test_installed_client_sends_secret_and_service_patches_as_merge_patches`. |
| `tests/test_dedicated_worker_launch_goldens.py` | **Keep** positive `user_agent_tailnet_ssh` and negative unmarked, shared/user/unscoped and script cases. |
| `tests/fixtures/dedicated_worker_launch/docker_user_agent_tailnet_ssh.json` | **Keep** marked launch golden. |
| `tests/fixtures/dedicated_worker_launch/kubernetes_user_agent_tailnet_ssh.json` | **Keep** marked launch golden. |
| `tests/test_runtime_chart_tailnet_ssh.py` | **Shrink** `test_auth_key_login_server_tags_and_proxy_are_optional_env`: key secret now required when enabled; Headscale/tags/proxy stay optional. Supply a dummy secret ref in render fixtures and test missing ref explicitly; **keep** port conflicts, mark/namespace selection, isolated listener, no gateway ingress, bounded egress and unchanged worker egress/Squid. |
| `tests/test_tailnet_ssh_headscale_e2e.py` | **Shrink**: drop `test_enrol_by_login_url_then_exec`, `_enrol`, `_login_stack`, URL assertions and `other_tailnet`/`stack_other_tailnet`; use one pre-auth stack with Alice’s and Bob’s nodes. Replace waits on `running`, `login_name`, `login_url` or `enrollment` with Headscale `nodes list`/state-dir checks and successful owner SSH (`_await_admitted`); on restart require SSH with the pinned host key, and for removal use the new acknowledgement plus node/dir absence. Test each owner’s exec and prove TCP reaches the other node by observing its `WhoIs` refusal and no connect call, even with the right SSH key. Keep wrong key/password, read-only projection/uid/0600, PTY/file transfers, forwarding refusal, live key revocation and 120 s wake. This uses a fake primary: real primary→worker binding is covered by `tests/test_tailnet_ssh_links.py`, not a new end-to-end test. |
| `tailnet-gateway/node_test.go` | **Shrink**: delete `TestObserveMapsTsnetStatesToTheContracts`, `TestObserveSeesANodeItsTailnetsLockKeepsOutAsWaitingForApproval` and `TestObserveReadsTheTailnetFromTheStatus`; keep `TestPeerOfATaggedDeviceHasNoLogin`. Drop unauthenticated-start assumptions from `TestTsnetNodeStartsWithoutALoginServer`; test missing-key rejection without network and default control URL separately from Headscale enrollment. |
| `tailnet-gateway/primary_test.go` | **Shrink** old wire fields and mode assertions; **keep** bearer, failed/null lists, status `204`, connect/refusal/target validation, timeouts and no-proxy coverage. |
| `tailnet-gateway/reconcile_test.go` | **Shrink**: delete `TestSuspendedKeepsItsNode`, `TestLoginAfterChangeCallsRelogin`, `TestNeedsLoginAfterRunningCallsReloginOnce`, `TestFailedReloginIsTriedAgain`, `TestStatusReportsWhatTheNodeSees`, `TestGatewayWithoutLinksStillSaysHowItEnrols` and `TestStatusCarriesALinksLastErrorWhileItIsListed`; drop enrollment assertions and test omission/revision instead. Keep dirs, startup retry, partial success with invalid rows, failed/null vs empty lists, early cancellation, deletion ack, age counts and status retry/no overlap during slow reconciles. |
| `tailnet-gateway/serve_test.go` | **Shrink**: replace `TestSuspendedRefusesNewAndClosesOpen` with omitted-link coverage and `TestClosedBeforeChangeClosesAllOpenConnections` with revision change; drop `last_error` assertions, not refusal/timeout cases. **Must keep** wrong/unknown/tagged peer and nil-login refusal before primary, rate limit, pre-banner timing, pipe, revoke during wake/dial, removing refusal and activity counting. |
| `tailnet-gateway/testdata/connect.json` | **Keep** `{peer_login, peer_node}` fixture; no target fields from the peer. |
| `tailnet-gateway/testdata/links.json` | **Shrink**: remove `login_after`, replace `closed_before` with `access_revision`, keep `on` and `removing` rows. |
| `tailnet-gateway/testdata/status.json` | **Shrink** to the connection-count map and removed IDs (no `enrollment`, node state, URL, metadata or last error); keep shared Go/Python contract tests. |
| `tests/test_smoke_tailnet_ssh_worker.py` | **Keep** context/cleanup, chart pod identities and banner checks; adapt to the shared builder, assert the dummy Secret ref is absent from the stand-in’s env, and replace the custom error-relay assertion with a failing-probe check. |
| `tests/api/test_api.py`, `tests/api/test_computers.py` | **Keep** `held_worker_keys` maintenance coverage and the existing Computer behavior. |
| `tests/test_ci_release_dispatch.py`, `tests/test_ci_workflow_permissions.py` | **Keep** release matrix, job permissions, and pinned workflow action coverage. |
| `tests/test_mindroom_dockerfiles.py`, `tests/test_orchestrator_runtime.py` | **Keep** image packages and internal-listener lifecycle tests. |
| `frontend/src/test/fixtures/config-schema.json` | **Keep/regenerate** `AgentConfig.tailnet_ssh`, the root section and both `TailnetSshConfig` maps with new config descriptions; this is schema, not a portal card. |
| `tailnet-gateway/go.sum`, `uv.lock` | **Keep** generated dependencies and Kubernetes 37 pin; regenerate as needed, do not revert the fix. |

#### Docs and deferred files (excluded from line total)

| File | Action |
| --- | --- |
| `docs/baspowers/specs/2026-11-19-tailnet-ssh-design.md` | **Keep** the original record plus this superseding section. |
| `docs/baspowers/plans/2026-11-19-tailnet-ssh.md` | **Shrink/update in the implementation pass**: drop Tasks 8 and 13, the user contract and interactive steps; update enrollment, tests and rollout for one key, and use Task 14 for operator setup and documented limits. |
| `docs/architecture/code-map.md` | **Shrink**: delete the `api/tailnet_ssh.py` row and update link store, eligibility and gateway entries. |
| `docs/architecture/security-posture.md` | **Shrink**: remove portal, old enrollment and URL handling claims; record same-tailnet reachability, config-only keys, credential custody, revocation and remaining isolation guarantees. |
| `skills/mindroom-docs/references/page__architecture__security-posture__index.md`, `skills/mindroom-docs/references/llms-full.txt` | **Regenerate** from corrected posture docs, not independently. |
| `frontend/src/connections/TailnetSsh.tsx`, `frontend/src/connections/TailnetSsh.test.tsx`, `frontend/src/connections/Connections.tsx` | **Do not add** the planned component, tests, or mount point; the card is not in the current branch diff. |
| `docs/tailnet-ssh.md`, `docs/connections-portal.md`, `docs/deployment/docker.md`, `docs/deployment/kubernetes.md`, `zensical.toml` | **Drop** the planned card instructions and portal link; keep only applicable single-tailnet operator setup and deployment pointers in the implementation pass. |
