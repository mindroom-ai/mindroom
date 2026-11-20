# SSH into your own worker over Tailscale (tailnet SSH)

Date: 2026-11-19. Status: decided by Bas ("let's build it"), ready to plan.
Code facts are from `mindroom` `origin/main` (branch `feat/tailnet-ssh`), `mindroom-chat` c65efdffe, and `mindroom-infra` `origin/main`.
Tailscale and sshd behaviour is from the feasibility spike (`tailscale-brainstorm/spike-report.md` in the DevAgent workspace, prototype in `spike-gateway/`), cited as "spike Q1" to "spike Q6": tsnet 1.104.7, Headscale 0.29.7, OpenSSH 10.0, the hosted worker seccomp profile under Docker.

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
