# Tailnet SSH Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: implement this plan task-by-task with the skill named in **Execution**. Steps use checkbox (`- [ ]`) syntax.

**Execution:** baspowers:subagent-driven-development — 14 tasks across three PRs share one wire contract and one eligibility rule, and a mistake in either is a tenant-isolation bug.

**Goal:** A person runs `ssh <hostname>` from their own tailnet and lands in the one worker container that holds only their files, also when that worker is asleep.

**Architecture:** A small Go gateway holds one `tsnet` node per link and is a pipe: every accepted connection asks the primary, which checks eligibility, wakes the worker, projects `sshd_config`, keys and a host key into the worker's read-only `.runtime`, has the runner start sshd, and names the target.
All decisions live in `src/mindroom/tailnet_ssh/`; the portal card talks to `/api/connections/tailnet-ssh`.

**Tech Stack:** Python 3.13 (FastAPI, pydantic, `cryptography`, httpx/aiohttp), Go 1.27.4 with `tailscale.com` v1.104.7 (`tsnet`), OpenSSH 10 server in the worker image, Helm, React + vitest, Headscale 0.29.7 for the integration test.

**Spec:** `docs/baspowers/specs/2026-11-19-tailnet-ssh-design.md` (read it with this plan). Spike evidence: `~/.mindroom-chat/mindroom_data/agents/mindroom_dev/workspace/tailscale-brainstorm/spike-report.md`, prototype `spike-gateway/main.go` + `go.mod` beside it.

**Worktree and branches:** `/home/basnijholt/repos/worktrees/mindroom-tailnet-ssh`, branch `feat/tailnet-ssh`. Tasks 1–8 are **PR 1a**, 9–12 **PR 1b**, 13–14 **PR 2**; each PR is opened when its last task is green and the next PR's branch stacks on it.

## Global Constraints

- Backend tests: `NIX_PATH=nixpkgs=flake:nixpkgs nix-shell --run 'uv run pytest tests/<file>.py -x -n 0 --no-cov'`. "Run" in a task means this command with that task's file.
- Heavy suites (full pytest, helm render sweeps, image builds, `go test` that pulls tsnet, the Headscale test) run on gce-dev under `/work/devagent-tailnet-<task>` (skill `working-across-machines`), never locally.
- Every new module gets a `[[modules]]` entry in `tach.toml`, and every declared module that imports a new one lists it in `depends_on`; verify with `uv run --with tach tach check --dependencies --interfaces`.
- `ruff format` and `ruff check` only the files the task changed. Async fixtures use `@pytest_asyncio.fixture`. `Toolkit` is imported from `mindroom.tool_system.authoring` (no task here adds a tool).
- Never `git stash` (worktrees share refs). Do not commit the plan or spec changes with code.
- Every commit ends with `--trailer "Assisted-by: MindRoom DevAgent:<model id> <https://github.com/mindroom-ai/mindroom>"`, `<model id>` being the model that implemented the task.
- Least new code: reuse the helpers each task names; no new abstraction a task does not list.
- Feature gate: on only when the primary has `MINDROOM_TAILNET_SSH_GATEWAY_TOKEN`; the internal listener port is `MINDROOM_TAILNET_SSH_PORT`, default `8769`, never the public API port.
- Worker SSH port `2222`; account `mindroom` (uid 1000); pod and container label `mindroom.ai/tailnet-ssh: "true"`.
- Eligibility reasons, exactly: `backend`, `not_enabled`, `scope`, `shared_workspace`, `joined_directory`, `not_authorized`.
- Timings from the spec: reconcile every 5 s, status at least every 30 s, pre-banner line `MindRoom: waking worker` every 3 s, six connects a minute per link, a connection counts as activity once 75 s old, status older than 90 s is ignored, `LoginGraceTime 60`, unenrolled links dropped after 24 h, connect may take the worker ready timeout (600 s hosted).
- `login_url` leaves the primary only in `GET /api/connections/tailnet-ssh` to the link's owner; no log line, tool, Matrix event, or other route carries it.
- The gateway token and node keys never reach a worker: the token is not in any worker env or manifest, and no worker mounts the gateway volume.
- Decisions on the spec's open questions: (1) assigned knowledge bases pass, read-only; (2) an owned shared directory passes; (3) tagged devices are refused; (4) docs name a reusable tagged auth key, the gateway passes `TAILNET_SSH_AUTH_KEY` to `tsnet` unchanged and nothing OAuth-specific is built; (5) the SSH shell is built from a shell call's request outside a conversation (it is cheap: `agent_terminals` + `worker_call`); (6) default hostname is the agent name with `_` as `-`; (7) gateway egress is direct TCP 443 by NetworkPolicy, `httpsProxy` optional; (8) real-Tailscale and kind checks are PR 4, not here.
- Docker: a worker's `2222` is published on `publish_host` only, and never when that is a wildcard address (`0.0.0.0`, `::`); such a worker gets no mark and no SSH.

## Plan decisions beyond the spec

- `WorkerHandle.ssh_endpoint` is a `WorkerSshEndpoint(host, port, projection_dir)`, so the link service never branches on the backend to find `<worker root>/.runtime/ssh`.
- The runner passes `HostKey`, `AuthorizedKeysFile` and `ForceCommand` to sshd as `-o` options, because only the worker knows its mount path and interpreter; the projected `sshd_config` holds everything else and no path.
- The gateway applies `closed_before` and `login_after` when their value changes, not by comparing them with its own clock.
- `not_authorized`, a missing agent, and a changed worker key remove a link; every other failing condition suspends it.
- Chart value `tailnetSsh.extraEgress` (raw egress rules) is added; with `httpsProxy` set the public-443 rule is not rendered and `validate.yaml` requires `extraEgress` to admit the proxy.
- `maintain_leased_workers(..., computer_worker_keys=)` is renamed `held_worker_keys=` and receives the union of Computer and SSH keys.

## Review Focus

1. **Gateway and primary clocks differ.** A key removal must still cut every open session of the link → Task 10 `TestClosedBeforeChangeClosesAllOpenConnections`.
2. **The primary restarts while links are enrolled.** With no status yet, an enrolled link stays `enrolled`, is still listed `on`, and nothing is removed or re-enrolled → Task 6 `test_enrolled_link_without_status_stays_listed_and_enrolled`.
3. **Requester IDs with `:`, a port, uppercase, or `%` in a worker key, and two agents whose names normalize alike.** The launch mark reads the right person or refuses → Task 1 `test_user_agent_worker_key_requester_round_trips_odd_ids`, Task 3 `test_ambiguous_agent_name_is_never_marked`.
4. **What people paste as a key.** Several lines at once, CRLF, a leading `command=` option, a private key, a 21st key → Task 1 `test_normalize_public_key_shapes`, Task 2 `test_set_keys_splits_pasted_lines_dedupes_and_caps_at_twenty`, Task 8 `test_put_keys_refuses_private_key_with_a_sentence`.
5. **sshd does not come up, dies later, or two connects race on a sleeping worker.** One sshd, restarted when gone, and a 503 with sshd's own last line → Task 5 `test_concurrent_starts_spawn_one_sshd`, `test_start_restarts_sshd_that_exited`, `test_start_reports_sshd_stderr_when_it_exits_at_once`.

---

## File Structure

| File | PR | Responsibility |
| --- | --- | --- |
| `src/mindroom/config/tailnet_ssh.py` (new) | 1a | `TailnetSshConfig` (`logins`, `authorized_keys`) and `normalize_public_key`. |
| `src/mindroom/config/agent.py`, `config/main.py` | 1a | `AgentConfig.tailnet_ssh`, root `tailnet_ssh`, load validation. |
| `src/mindroom/tailnet_ssh/eligibility.py` (new) | 1a | The one rule: `config_error`, `link_ineligibility`, `ssh_eligible_launch`. |
| `src/mindroom/tailnet_ssh/store.py` (new) | 1a | `Link`, `LinkBook`, `read_links`, `update_links`, `new_link`, `remove_links`. |
| `src/mindroom/tailnet_ssh/projection.py` (new) | 1a | `write_projection`: `sshd_config`, `authorized_keys`, host key. |
| `src/mindroom/tailnet_ssh/links.py` (new) | 1a | Desired state, status intake, connect sequence, operator links, activity keys. |
| `src/mindroom/tailnet_ssh/session.py` (new) | 1a | sshd `ForceCommand` target inside the worker; stdlib only. |
| `src/mindroom/api/tailnet_ssh_gateway.py` (new) | 1a | Internal listener: three bearer-token routes on their own port. |
| `src/mindroom/api/tailnet_ssh.py` (new) | 1a | User routes under `/api/connections/tailnet-ssh`. |
| `src/mindroom/api/worker_ssh.py` (new) | 1a | Runner `POST /api/sandbox-runner/ssh/start`. |
| `src/mindroom/workers/models.py`, `backends/docker.py`, `backends/kubernetes.py`, `backends/kubernetes_resources.py`, `backends/_lifecycle.py` | 1a | `WorkerSshEndpoint`, the launch mark, the handle field. |
| `src/mindroom/tool_system/worker_routing.py` | 1a | `user_agent_worker_key_requester`. |
| `src/mindroom/api/workers.py`, `api/main.py`, `api/app_state.py`, `orchestrator.py`, `api/sandbox_runner_app.py` | 1a | Wiring: activity touch, listener start, routers. |
| `local/instances/deploy/Dockerfile.mindroom` | 1a | `openssh-server rsync` on the apt line. |
| `tailnet-gateway/` (`go.mod`, `main.go`, `primary.go`, `reconcile.go`, `serve.go`, `node.go`, `Dockerfile`, `*_test.go`) | 1b | The gateway. |
| `.github/workflows/build-mindroom.yml`, `.github/workflows/tailnet-gateway.yml` (new) | 1b | Image publish, `go test`, Headscale job. |
| `cluster/k8s/runtime/templates/tailnet-ssh.yaml` (new), `deployment.yaml`, `validate.yaml`, `values.yaml`, `README.md` | 1b | Chart. |
| `tests/test_tailnet_ssh_headscale_e2e.py` (new), `docs/architecture/security-posture.md` | 1b | Integration test, posture row. |
| `frontend/src/connections/TailnetSsh.tsx` (new), `Connections.tsx` | 2 | Portal card. |
| `docs/tailnet-ssh.md` (new), `docs/connections-portal.md`, `docs/deployment/docker.md` | 2 | User and operator docs. |

## Contracts (shared by Tasks 6–13; copy values exactly)

**Internal listener** (primary, `Authorization: Bearer <token>`, JSON):

- `GET /links` → `[{"id": str, "hostname": str, "desired": "on"|"suspended"|"removing", "allowed_login": str|null, "closed_before": float, "login_after": float}]`. Links stored `off` are not listed.
- `POST /status` body `{"enrollment": "login"|"auth_key", "links": {"<id>": {"state": "needs_login"|"needs_approval"|"running"|"removed", "login_url": str|null, "tailnet": str|null, "dns_name": str|null, "login_name": str|null, "key_expiry": str|null, "open_connections": int, "last_error": str|null}}}` → `204`.
- `POST /links/{id}/connect` body `{"peer_login": str, "peer_node": str}` → `200 {"host": str, "port": int}`, or `403`/`404`/`409`/`503` with `{"detail": str}`; the gateway keeps `detail` as the link's `last_error`.
- A missing or wrong token answers `401` on all three.

**Gateway environment:** `TAILNET_SSH_PRIMARY_URL` (default `http://127.0.0.1:8769`), `TAILNET_SSH_GATEWAY_TOKEN` (required), `TAILNET_SSH_STATE_DIR` (default `/state`), `TAILNET_SSH_AUTH_KEY`, `TAILNET_SSH_LOGIN_SERVER`, `TAILNET_SSH_TAGS` (comma-separated), `HTTPS_PROXY`.

**User API** `GET /api/connections/tailnet-ssh` →

```json
{"enabled": true, "enrollment": "login", "keys": ["ssh-ed25519 AAAA... bas@laptop"], "github_login": "bas",
 "agents": [{"name": "mind_bas", "display_name": "Mind", "eligible": true, "reason": null, "detail": null,
   "default_hostname": "mind-bas", "knowledge_bases": ["allpurpose"], "shared_directories": [],
   "link": {"hostname": "mind-bas", "state": "enrolled", "login_url": null, "tailnet": "tail1234.ts.net",
            "dns_name": "mind-bas.tail1234.ts.net", "login_name": "bas@github", "key_expiry": null,
            "worker": "asleep", "host_public_key": "ssh-ed25519 AAAA...", "last_error": null}}]}
```

`enabled` is false and the lists empty when the feature is off. `agents` holds every agent with `tailnet_ssh: true` the requester may use. `link` is null without a link or for one stored `off`. `link.state` is one of `pending_login`, `needs_approval`, `enrolled`, `needs_login`, `suspended`, `removing`; `link.worker` is `ready`, `waking`, `asleep`, or null when not `enrolled`.

**Worker files:** the primary writes `<worker root>/.runtime/ssh/{sshd_config,authorized_keys,ssh_host_ed25519_key}`; the runner keeps the session launch at `/tmp/mindroom-ssh/launch.json` (0600, `{"argv": [...], "env": {...}, "cwd": str}`).

---

### Task 1 (PR 1a): Config field and the eligibility rule

**Files:**
- Create: `src/mindroom/config/tailnet_ssh.py`, `src/mindroom/tailnet_ssh/__init__.py` (empty), `src/mindroom/tailnet_ssh/eligibility.py`
- Modify: `src/mindroom/config/agent.py` (`AgentConfig`), `src/mindroom/config/main.py` (root field, `_OPTIONAL_SECTION_NAMES`, validator), `src/mindroom/tool_system/worker_routing.py`, `tach.toml`
- Test: `tests/test_tailnet_ssh_eligibility.py`

**Interfaces:**
- Consumes: `agent_policy(config, name)` (`config/resolution.py`), `resolve_responder_access(config, name)` (`access_policy.py`), `is_concrete_matrix_user_id` (`matrix_identifiers.py`), `is_sender_allowed_for_responder` (`authorization.py`), `worker_backend_kind(paths).dedicated`, `resolved_worker_key_scope`, `subagent_worker_state_scope_key`, `worker_key_agent_name`, `normalize_worker_key_part`, `load_shared_directories`, `shared_directory_access` (`shared_directories/store.py`), `SharedDirectoryMount.read_only`, `WorkerLaunchConfig.config` and `.agent_policies`.
- Produces:
  - `AgentConfig.tailnet_ssh: bool = False`; `Config.tailnet_ssh: TailnetSshConfig` with `logins: dict[str, str]` and `authorized_keys: dict[str, list[str]]`, both keyed by concrete Matrix user IDs, `extra="forbid"`.
  - `normalize_public_key(line: str) -> str` in `config/tailnet_ssh.py`: returns `"<type> <base64>[ <comment>]"` checked with `cryptography`'s `load_ssh_public_key`; `ValueError` with a sentence for an options prefix, a private key (`"That is a private key; paste the .pub file."`), an empty line, or anything the loader refuses. `authorized_keys` values are stored normalized.
  - `worker_routing.user_agent_worker_key_requester(worker_key: str) -> str | None`: the requester a `user_agent` key names; None for another scope, a subagent pin (`subagent_worker_state_scope_key`), a script-run pin (`script-<32 hex>` before the agent part, as `workers.models.process_worker_key` writes it), or a key that re-encoding that requester and agent part does not reproduce.
  - `eligibility.Reason = Literal["backend", "not_enabled", "scope", "shared_workspace", "joined_directory", "not_authorized"]`
  - `eligibility.Ineligible(reason: Reason, detail: str | None = None)` (frozen dataclass; `detail` is the joined directory as `owner/name`).
  - `eligibility.config_error(config: Config, agent_name: str) -> str | None`
  - `eligibility.link_ineligibility(config: Config, runtime_paths: RuntimePaths, requester_id: str, agent_name: str, worker_key: str | None, membership_index: AgentReplyMembershipIndex) -> Ineligible | None`
  - `eligibility.ssh_eligible_launch(worker_key: str, launch: WorkerLaunchConfig, private_agent_names: frozenset[str] | None, shared_mounts: Sequence[SharedDirectoryMount]) -> bool`

- [ ] **Step 1: Write the failing tests** in `tests/test_tailnet_ssh_eligibility.py`.
  One config builder with agents: `mind` (`private: {per: user_agent}`, `tailnet_ssh: true`), `mine` (`worker_scope: user_agent`, `access: {users: ["@alice:example.org"], members_of_rooms: []}`, `tailnet_ssh: true`), `coowned` (as `mine` with a second user), `roomy` (as `mine` plus `members_of_rooms: [lobby]`), `plain` (as `mine` without `tailnet_ssh`).
  Alice's key for `mine` is `v1:default:user_agent:~@alice:example.org:mine`.

```python
@pytest.mark.parametrize(("agent", "requester", "reason"), [
    ("mind", ALICE, None), ("mine", ALICE, None), ("plain", ALICE, "not_enabled"),
    ("coowned", ALICE, "shared_workspace"), ("roomy", ALICE, "shared_workspace"), ("mine", BOB, "not_authorized"),
])
def test_link_ineligibility_matrix(agent, requester, reason): ...   # docker backend env; compares .reason
def test_static_runner_backend_is_backend(): ...                    # reason == "backend", checked first
def test_subagent_pinned_key_is_scope(): ...                        # subagent_worker_key(key, "a" * 32) -> "scope"
def test_joined_directory_names_it(): ...   # mine is a member of other/specs -> Ineligible("joined_directory", "other/specs")
def test_owned_directory_and_knowledge_base_pass(): ...             # mine owns mine/notes, knowledge_bases: [docs] -> None
@pytest.mark.parametrize("scope", ["shared", "user", None])
def test_config_error_names_agent_and_scope(scope): ...  # Config load raises; message has "agents.x.tailnet_ssh" and "user_agent"
def test_config_error_requires_private_or_concrete_users(): ...     # users: ["@*:example.org"] or room grants -> raises
def test_config_load_accepts_two_users(): ...                       # coowned loads; only link_ineligibility refuses it
def test_personal_agent_template_accepts_tailnet_ssh(): ...         # personal_agents.<k>.agent.tailnet_ssh: true loads
@pytest.mark.parametrize("requester", ["@alice:example.org", "@Bas.N:example.org:8448", "@a%b:example.org"])
def test_user_agent_worker_key_requester_round_trips_odd_ids(requester): ...  # via resolve_worker_key("user_agent", identity)
def test_user_agent_worker_key_requester_refuses_other_keys(): ...  # user, shared, unscoped, subagent_worker_key(...) and
                                                                    # script_worker_key_for_run(key, "script-" + "a" * 32) -> None
def test_normalize_public_key_shapes(): ...
    # "ssh-ed25519 AAAA... me@host\r\n" -> stripped; 'command="x" ssh-ed25519 AAAA...' -> ValueError;
    # "-----BEGIN OPENSSH PRIVATE KEY-----" -> ValueError containing "private key"; "" -> ValueError
def test_config_authorized_keys_are_normalized_and_keyed_by_concrete_ids(): ...   # "@*:example.org" key -> load fails
def test_ssh_eligible_launch_matrix(): ...  # True for mind (A in private_agent_names) and mine; False for plain, coowned,
                                            # a user key, a subagent-pinned and a script-pinned key, mind with private_agent_names=None,
                                            # and any shared_mounts entry with read_only=True
```

- [ ] **Step 2: Run** `tests/test_tailnet_ssh_eligibility.py`. Expected: FAIL, `ModuleNotFoundError: mindroom.tailnet_ssh`.

- [ ] **Step 3: Implement.** `link_ineligibility` checks in this order and returns the first failure: `backend` (not dedicated), `not_enabled`, `scope` (policy scope is not `user_agent`, `worker_key` is None, not a `user_agent` key, or pinned), `not_authorized` (`is_sender_allowed_for_responder(requester_id, agent_name, None, ...)` is false, or `user_agent_worker_key_requester(worker_key) != requester_id`), `shared_workspace` (not private, and not: `users == (requester_id,)`, concrete, `members_of_rooms == ()`, `current_room_members` false), `joined_directory` (`shared_directory_access(directory, (agent_name,), is_shareable)` is `False` for a stored directory).
  `ssh_eligible_launch` applies the same conditions without the backend and membership checks, taking the person from the key, the agent as the single entry of `launch.agent_policies` whose normalized name is the key's agent part, and `joined_directory` from `shared_mounts`; a private agent must be in `private_agent_names`.
  `config_error` returns a sentence starting `agents.<name>.tailnet_ssh requires ` for a scope other than `user_agent`, or for an agent that is neither private nor limited to concrete `access.users` with no room grants; it does not count users.
  The validator `validate_tailnet_ssh_agents` in `config/main.py` imports `config_error` inside the function body and raises the first sentence.

- [ ] **Step 4: Run** the file (PASS), `tests/test_dedicated_worker_launch_goldens.py` (PASS, unchanged), and the tach check.

- [ ] **Step 5: Commit** `feat(tailnet-ssh): config field and eligibility rule`.

### Task 2 (PR 1a): Link store

**Files:**
- Create: `src/mindroom/tailnet_ssh/store.py`
- Modify: `tach.toml`
- Test: `tests/test_tailnet_ssh_store.py`

**Interfaces:**
- Consumes: `normalize_public_key` (Task 1); `tracking_dir`, `advisory_file_lock`, `write_json_file_durable(..., strict_atomic_replace=True)` exactly as `contacts/store.py` `update_contacts` uses them; `cryptography` `Ed25519PrivateKey`.
- Produces:
  - `Link(BaseModel)`: `id`, `requester_id`, `agent_name`, `worker_key`, `hostname`, `desired: Literal["on", "off", "removing"]`, `host_key` (OpenSSH private key text), `host_public_key`, `enrolled_login: str | None`, `closed_before: float`, `login_after: float`, `created_at: float`, `enrolled_at: float | None`, `park_when_removed: bool = False`.
  - `LinkBook(BaseModel)`: `links: list[Link]`, `keys: dict[str, list[str]]`; `link(requester_id, agent_name) -> Link | None`; `by_id(link_id) -> Link | None`.
  - `MAX_KEYS = 20`; `HOSTNAME = re.compile(r"[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?")`; `default_hostname(agent_name: str) -> str`.
  - `read_links(runtime_paths) -> LinkBook`; `update_links[T](runtime_paths, change: Callable[[LinkBook], T]) -> T`; file `tracking_dir(runtime_paths) / "tailnet_ssh.json"`, lock `tailnet_ssh.lock`.
  - `new_link(requester_id: str, agent_name: str, worker_key: str, hostname: str, *, now: float) -> Link` (`id = secrets.token_hex(8)`, fresh Ed25519 host key, `desired="on"`, `login_after=now`).
  - `set_keys(book: LinkBook, requester_id: str, lines: Sequence[str], *, now: float) -> None`: splits each entry on newlines, drops blank lines, normalizes, de-duplicates, enforces `MAX_KEYS`, and when a stored key is gone sets `closed_before = now` on that person's links.
  - `remove_links(book: LinkBook, where: Callable[[Link], bool], *, now: float, park: bool = False) -> list[Link]`: the only function that sets `desired="removing"`; also sets `closed_before = now` and `park_when_removed = park`.
  - `finish_removal(book: LinkBook, link_id: str) -> None`: a `removing` link becomes `off` (with `enrolled_login`, `enrolled_at` cleared) when `park_when_removed`, else leaves the book.

- [ ] **Step 1: Write the failing tests**

```python
def test_set_keys_splits_pasted_lines_dedupes_and_caps_at_twenty(): ...   # one entry holding two lines -> two keys;
                                                                          # 21 distinct keys -> ValueError naming 20
def test_removing_a_key_sets_closed_before_adding_does_not(): ...
def test_new_link_has_16_hex_id_and_loadable_host_key(): ... # re.fullmatch("[0-9a-f]{16}", id); host_public_key starts "ssh-ed25519 "
def test_default_hostname(): ...   # "mind_bas" -> "mind-bas"; "A__b_" -> "a-b"; a 80-char name -> 63 chars, HOSTNAME matches
def test_remove_links_then_finish_drops_or_parks(): ...      # park=False -> gone; park=True -> desired == "off", enrolled_login None
def test_update_links_round_trips_and_writes_under_tracking(): ...
def test_one_link_per_requester_and_agent(): ...             # book.link(...) finds it; another requester's is not returned
```

- [ ] **Step 2: Run** `tests/test_tailnet_ssh_store.py`. Expected: FAIL, import error.
- [ ] **Step 3: Implement** the interfaces above; `read_links` of a missing file returns an empty book.
- [ ] **Step 4: Run** the file (PASS) and the tach check.
- [ ] **Step 5: Commit** `feat(tailnet-ssh): link and key store`.

### Task 3 (PR 1a): Launch marks and `WorkerHandle.ssh_endpoint`

**Files:**
- Modify: `src/mindroom/workers/models.py`, `src/mindroom/workers/backends/_lifecycle.py` (`worker_handle`), `src/mindroom/workers/backends/docker.py` (`_render_launch` and its two callers, `_scoped_storage_mount_specs`, `_container_host_port`, `_to_handle`), `src/mindroom/workers/backends/kubernetes_resources.py` (`_pod_template`, `_deployment_manifest`, `_service_manifest`, `apply_service`), `src/mindroom/workers/backends/kubernetes.py` (`ensure_worker`, `_ReadyWorkerCacheEntry`, `_handle_from_deployment`, `_handle`), `tach.toml`
- Create: `tests/fixtures/dedicated_worker_launch/docker_user_agent_tailnet_ssh.json`, `kubernetes_user_agent_tailnet_ssh.json` (written by the golden test with `MINDROOM_UPDATE_WORKER_LAUNCH_GOLDENS=1`, then read and reviewed)
- Test: `tests/test_dedicated_worker_launch_goldens.py`, `tests/test_docker_worker_backend.py`, `tests/test_kubernetes_worker_backend.py`

**Interfaces:**
- Consumes: `ssh_eligible_launch` (Task 1); `plan_shared_directory_mounts`; `container_config_labels`; `service_host`'s naming (`<worker_id>.<namespace>.svc.cluster.local`).
- Produces:
  - `models.WORKER_SSH_PORT = 2222`, `models.LABEL_TAILNET_SSH = "mindroom.ai/tailnet-ssh"`.
  - `models.WorkerSshEndpoint(host: str, port: int, projection_dir: Path)` (frozen, slots); `projection_dir` is the primary's path to `<worker root>/.runtime/ssh`.
  - `WorkerHandle.ssh_endpoint: WorkerSshEndpoint | None = None`, passed through `worker_handle(..., ssh_endpoint=None)`.
  - Docker: a marked launch has `labels[LABEL_TAILNET_SSH] = "true"` and `ports["2222/tcp"] = (publish_host, None)`. The handle's endpoint is `(config.endpoint_host, <published host port of 2222>, paths.state.root / ".runtime" / "ssh")`, set only for a running container that carries the label.
  - Kubernetes: a marked pod template has the label (pod template and Deployment metadata, never the selector) and container port `{"containerPort": 2222, "name": "ssh"}`; its Service gains `{"name": "ssh", "port": 2222, "targetPort": 2222}`.
    The handle's endpoint is `("<worker_id>.<namespace>.svc.cluster.local", 2222, (storage_root / state_subpath).resolve() / ".runtime" / "ssh")`, set only when the Deployment's metadata carries the label.

- [ ] **Step 1: Write the failing tests.** In the goldens file add agent `desk` (`private: {per: user_agent}`, `tailnet_ssh: true`) to `_CONFIG_TEMPLATE`, key `_SSH_KEY = "v1:tenant-123:user_agent:~@alice:example.org:desk"`, and one case per backend named `user_agent_tailnet_ssh`. Then:

```python
# tests/test_docker_worker_backend.py
def test_marked_docker_launch_publishes_2222_on_publish_host(): ...
    # default config: rendered ports["2222/tcp"] == ("127.0.0.1", None); labels["mindroom.ai/tailnet-ssh"] == "true"
@pytest.mark.parametrize("publish_host", ["0.0.0.0", "::"])
def test_docker_ssh_port_is_never_published_on_a_wildcard_address(publish_host): ...
    # same eligible launch: "2222/tcp" not in ports, label absent, handle.ssh_endpoint is None
def test_docker_handle_endpoint_needs_label_and_running_container(): ...
    # labelled + running + bound 2222 -> WorkerSshEndpoint("127.0.0.1", <port>, state_root/".runtime"/"ssh");
    # stopped, or unlabelled with a 2222 binding -> None
def test_joining_a_directory_changes_the_docker_launch_hash(): ...   # read-only shared mount -> no mark, hash differs
# tests/test_kubernetes_worker_backend.py
def test_marked_pod_has_label_port_and_service_port(): ...  # selector matchLabels has no tailnet-ssh key
def test_unmarked_scopes_have_no_ssh_port_or_label(): ...   # user, shared, unscoped, script keys
def test_kubernetes_handle_endpoint_follows_the_deployment_label(): ...  # also for a handle served from the ready cache
def test_ambiguous_agent_name_is_never_marked(): ...        # agents "my-desk" and "my.desk" both tailnet_ssh: no mark
```

- [ ] **Step 2: Run** the three files. Expected: FAIL (`WorkerSshEndpoint` missing; new golden absent).
- [ ] **Step 3: Implement.** Docker: add `_shared_directory_mounts(spec, launch) -> tuple[SharedDirectoryMount, ...]` around the existing `plan_shared_directory_mounts` call, use it in `_scoped_storage_mount_specs`, and give `_render_launch` a keyword `ssh: bool` computed by both callers as `ssh_eligible_launch(spec.worker_key, launch, spec.private_agent_names, self._shared_directory_mounts(spec, launch))` and the publish host not being unspecified (`ipaddress.ip_address(host).is_unspecified`; a host that is not an address counts as wildcard).
  Both backends also refuse the mark when `resolve_state_scope_worker_key(spec.worker_key, spec.state_scope_worker_key) != spec.worker_key`, so a pinned worker is denied twice.
  `_container_host_port` takes the container port as a parameter.
  Kubernetes: `KubernetesResourceManager.ssh_marked(worker_key, *, launch, private_agent_names, state_scope_worker_key) -> bool` calls `ssh_eligible_launch` with `self.plan_shared_directory_mounts(...)`; `_pod_template`, `_deployment_manifest` and `apply_service(worker_id, *, ssh: bool)` use it; `_ReadyWorkerCacheEntry` gains `ssh: bool` so a cached handle keeps its endpoint; `_handle(..., ssh: bool = False)`.
- [ ] **Step 4: Run** the three files (PASS) after generating the two new goldens; read them (label and port present); `git status --short tests/fixtures/dedicated_worker_launch/` lists only the two new files, so the `user`, `shared`, unscoped and script goldens are byte-identical; run the tach check.
- [ ] **Step 5: Commit** `feat(tailnet-ssh): mark eligible worker launches and expose the SSH endpoint`.

### Task 4 (PR 1a): Projection

**Files:**
- Create: `src/mindroom/tailnet_ssh/projection.py`
- Modify: `tach.toml`
- Test: `tests/test_tailnet_ssh_projection.py`

**Interfaces:**
- Consumes: `Link.host_key`, `LinkBook.keys` (Task 2); `Config.tailnet_ssh.authorized_keys` (Task 1).
- Produces:
  - `authorized_key_lines(config: Config, book: LinkBook, requester_id: str) -> list[str]`: portal keys then config keys, de-duplicated, each `restrict,pty <key>`.
  - `write_projection(projection_dir: Path, link: Link, key_lines: Sequence[str]) -> None`: writes `sshd_config`, `authorized_keys` and `ssh_host_ed25519_key` atomically (temp file + `os.replace`), directory 0755, the host key 0600, the others 0644.

- [ ] **Step 1: Write the failing tests**

```python
def test_sshd_config_is_the_spikes(tmp_path): ...
    # exact lines present: "Port 2222", "PidFile none", "UsePAM no", "StrictModes yes", "PubkeyAuthentication yes", "PasswordAuthentication no",
    # "KbdInteractiveAuthentication no", "AllowUsers mindroom", "PermitUserEnvironment no", "PermitUserRC no",
    # "LoginGraceTime 60", "Subsystem sftp internal-sftp", "AllowTcpForwarding no", "AllowStreamLocalForwarding no",
    # "AllowAgentForwarding no", "X11Forwarding no", "PermitTunnel no", "GatewayPorts no";
    # no line starts with "HostKey", "AuthorizedKeysFile" or "ForceCommand" (the runner passes those)
def test_authorized_keys_are_restricted_and_merged(tmp_path): ...  # every line starts "restrict,pty ssh-"; config + portal, no duplicate
def test_host_key_is_0600_and_matches_link(tmp_path): ...          # stat mode; public half equals link.host_public_key
def test_rewrite_replaces_removed_key(tmp_path): ...               # second write without a key: its line is gone
def test_no_keys_writes_empty_authorized_keys(tmp_path): ...
```

- [ ] **Step 2: Run** `tests/test_tailnet_ssh_projection.py`. Expected: FAIL, import error.
- [ ] **Step 3: Implement** with the config lines above in that order.
- [ ] **Step 4: Run** the file (PASS) and the tach check.
- [ ] **Step 5: Commit** `feat(tailnet-ssh): project sshd config, keys and host key`.

### Task 5 (PR 1a): Runner `/ssh/start`, the session module, image packages

**Files:**
- Create: `src/mindroom/api/worker_ssh.py`, `src/mindroom/tailnet_ssh/session.py`
- Modify: `src/mindroom/api/sandbox_runner_app.py` (include the router; stop sshd where `close_worker_terminals` runs), `local/instances/deploy/Dockerfile.mindroom:86` (add `openssh-server rsync` to the apt line and to the comment above it), `tach.toml`
- Test: `tests/api/test_worker_ssh.py`

**Interfaces:**
- Consumes: `prepare_terminal_launch(app, payload, None) -> TerminalLaunch`, `validate_runner_token`, `app_runtime_paths(app)` (`api/sandbox_runner.py`); `runner_dedicated_worker_root(paths)` (`api/sandbox_exec.py`); `ExecuteRequest`; `TerminalError`; `sandbox_startup_manifest_path`.
- Produces:
  - `session.LAUNCH_PATH = Path("/tmp/mindroom-ssh/launch.json")`.
  - `session.session_command(launch: Mapping[str, Any], environ: Mapping[str, str]) -> tuple[list[str], dict[str, str], str]` returning `(argv, env, cwd)`; `session.main()` loads `LAUNCH_PATH`, `chdir`s and `os.execvpe`s. The module imports only the standard library.
  - `worker_ssh.router = APIRouter(prefix="/api/sandbox-runner/ssh")`; `POST /start`, body `{"request": ExecuteRequest}`, guarded by `validate_runner_token`, answers `{"port": 2222}`.
  - `worker_ssh.sshd_argv(projection_dir: Path) -> list[str]`: `["/usr/sbin/sshd", "-D", "-e", "-f", f"{projection_dir}/sshd_config", "-o", f"HostKey={projection_dir}/ssh_host_ed25519_key", "-o", f"AuthorizedKeysFile={projection_dir}/authorized_keys", "-o", f"ForceCommand={sys.executable} -m mindroom.tailnet_ssh.session"]` (Task 12 starts sshd with the same function).
  - `worker_ssh.stop_worker_ssh(app: FastAPI) -> None`.

- [ ] **Step 1: Write the failing tests**

```python
def test_session_interactive_is_login_bash_in_workspace(): ...
    # launch {"argv": ["bash","--noprofile","--norc","-i"], "env": {"PATH": "/x", "PROMPT_COMMAND": "p", "INPUTRC": "/dev/null",
    #         "TERM": "dumb", "HTTPS_PROXY": "http://proxy"}, "cwd": "/data/ws"}, environ {"TERM": "xterm", "SSH_CONNECTION": "c"}
    # -> argv == ["bash", "-l"]; cwd == "/data/ws"; env["HOME"] == "/data/ws"; env["TERM"] == "xterm";
    #    env["HTTPS_PROXY"] == "http://proxy"; env["SSH_CONNECTION"] == "c"; env["USER"] == "mindroom";
    #    "PROMPT_COMMAND" not in env and "INPUTRC" not in env
def test_session_command_runs_through_bash_c(): ...        # SSH_ORIGINAL_COMMAND="rsync --server ." -> ["bash", "-c", "rsync --server ."]
def test_session_sftp_runs_the_sftp_server(): ...          # SSH_ORIGINAL_COMMAND="internal-sftp" -> ["/usr/lib/openssh/sftp-server"]
def test_session_module_imports_only_stdlib(): ...         # ast-parse session.py: no import starting with "mindroom"
def test_start_requires_runner_token(client): ...          # 401 without it
def test_start_writes_launch_0600_and_spawns_sshd(client, fake_sshd): ...
    # launch.json mode 0o600 holds the TerminalLaunch of a monkeypatched prepare_terminal_launch; the spawned argv ==
    # sshd_argv(<dedicated root>/.runtime/ssh); answer {"port": 2222}
def test_start_503_when_image_has_no_sshd(client): ...     # /usr/sbin/sshd absent -> 503 "The worker image has no SSH server."
def test_concurrent_starts_spawn_one_sshd(client, fake_sshd): ...        # two overlapping posts -> one spawn
def test_start_restarts_sshd_that_exited(client, fake_sshd): ...         # kill it; next post spawns again
def test_start_reports_sshd_stderr_when_it_exits_at_once(client): ...    # fake exits 1 printing "bad ownership" -> 503 with that line
def test_start_refuses_a_non_shell_request(client): ...                  # TerminalError(400) -> 400
```

`fake_sshd` monkeypatches the module's spawn and port probe: a stand-in process that listens on a free port, with `worker_ssh.WORKER_SSH_PORT` patched to it.

- [ ] **Step 2: Run** `tests/api/test_worker_ssh.py`. Expected: FAIL, import error.
- [ ] **Step 3: Implement.** One `asyncio.Lock` and the sshd process live on `app.state` as `_terminals` keeps its manager.
  `/start`: `prepare_terminal_launch` in a thread; write the launch file (directory 0700, file 0600, atomic replace); if no live sshd, spawn `sshd_argv(sandbox_startup_manifest_path(runner_dedicated_worker_root(paths)).parent / "ssh")` with stderr captured; wait up to 15 s for `127.0.0.1:2222` to accept, answering 503 with sshd's last stderr line if it exits first or never listens.
  `session_command` builds the environment as: launch env without `PROMPT_COMMAND` and `INPUTRC`; then `HOME` = launch cwd, `USER` = `LOGNAME` = `mindroom`, `SHELL=/bin/bash`; then `TERM`, `SSH_CONNECTION`, `SSH_CLIENT`, `SSH_TTY` from sshd's environment when set.
- [ ] **Step 4: Run** the file (PASS) and the tach check. On gce-dev build the image and check `docker run --rm --entrypoint sh <image> -c 'test -x /usr/sbin/sshd && test -x /usr/lib/openssh/sftp-server && command -v rsync'` exits 0.
- [ ] **Step 5: Commit** `feat(tailnet-ssh): runner starts sshd on demand with the agent's shell environment`.

### Task 6 (PR 1a): Link service

**Files:**
- Create: `src/mindroom/tailnet_ssh/links.py`
- Modify: `tach.toml`
- Test: `tests/test_tailnet_ssh_links.py`

**Interfaces:**
- Consumes: Tasks 1–4; `resolve_agent_execution(agent_name, config, paths, execution_identity=...).worker_target` (`runtime_resolution.py`); `lease_configured_primary_worker_manager(paths)`; `WorkerSpec(worker_key, private_agent_names=...)`; `agent_terminals(config, paths, agent_name, identity, create=True)` (`terminal/resolution.py`) and its `.stand_ins`; `build_requester_execution_identity`; `worker_call(stand_ins, SHELL_FUNCTION, start=False) -> WorkerCall | None` inside `unbound_agent_cli_shell_env()` as `WorkerTerminals.open` does; `WorkerBackendError`.
- Produces:
  - `LinkStatus` (dataclass): `state`, `login_url`, `tailnet`, `dns_name`, `login_name`, `key_expiry`, `open_connections`, `last_error`, `worker_key`.
  - `GatewayStatus` (dataclass): `enrollment: Literal["login", "auth_key"] | None = None`, `received_at: float = 0.0` (monotonic), `links: dict[str, LinkStatus]`, `operator_sync_at: float = 0.0`.
  - `ConnectRefused(status_code: int, detail: str)` (exception).
  - `resolve_target(config, runtime_paths, requester_id: str, agent_name: str) -> ResolvedWorkerTarget`: `resolve_agent_execution` with `build_requester_execution_identity(...)`; the same worker key `resolve_connection_agent` gives the portal (Task 8 tests the equality).
  - `desired_links(config, runtime_paths, membership_index, status: GatewayStatus, *, now: float) -> list[dict[str, object]]`: reconciles the store, then returns the `GET /links` rows.
  - `record_status(runtime_paths, status: GatewayStatus, payload: Mapping[str, Any], *, now: float, monotonic: float) -> None`.
  - `link_state(link: Link, status: LinkStatus | None, *, suspended: bool) -> str` (the `link.state` values of the user API).
  - `async connect(config, runtime_paths, membership_index, status, link_id: str, peer_login: str, peer_node: str) -> tuple[str, int]`.
  - `refresh_projections(config, runtime_paths, requester_id: str) -> None`: rewrites the projection of each of that person's links whose listed worker handle has `ssh_endpoint`.
  - `active_worker_keys(status: GatewayStatus, *, monotonic: float) -> frozenset[str]`.

- [ ] **Step 1: Write the failing tests** with a fake worker manager (`ensure_worker`, `list_workers`) and monkeypatched `agent_terminals`, `worker_call` and the runner POST; record the call order.

```python
def test_desired_rows_have_the_contract_keys(): ...           # exactly id, hostname, desired, allowed_login, closed_before, login_after
def test_off_links_are_not_listed_and_suspended_is_computed(): ...   # joined directory -> desired == "suspended", store still "on"
@pytest.mark.parametrize("trigger", ["agent_removed", "not_authorized", "worker_key_changed", "never_enrolled_24h"])
def test_removal_triggers_set_removing(trigger): ...          # store desired == "removing", closed_before == now
def test_removed_report_drops_or_parks(): ...                 # status state "removed" -> finish_removal
def test_first_running_report_pins_enrolled_login(): ...      # enrolled_login == "bas@github", enrolled_at set; a later
                                                              # report with another login leaves both unchanged
def test_enrolled_link_without_status_stays_listed_and_enrolled(): ...  # fresh GatewayStatus: row desired "on",
                                                              # link_state == "enrolled", store unchanged
def test_operator_mode_adds_links_once_a_minute(): ...        # enrollment auth_key; a listed worker with ssh_endpoint and no
                                                              # link gains one with allowed_login from tailnet_ssh.logins;
                                                              # a second call 10 s later adds nothing; an "off" link is not re-added
def test_operator_link_without_login_refuses_everyone(): ...  # allowed_login is None
async def test_connect_order(): ...                           # ["eligibility", "ensure_worker", "projection", "ssh_start"]; returns (host, port)
async def test_connect_without_ssh_endpoint_calls_no_runner(): ...   # ConnectRefused(409); "ssh_start" not in calls
async def test_connect_refuses_wrong_peer_unknown_suspended_and_removing(): ...  # 403, 404, 409, 409; ensure_worker not called
async def test_connect_logs_peer_but_never_a_login_url(caplog): ...
async def test_runner_503_becomes_connect_refused_with_its_detail(): ...
def test_active_worker_keys_window(): ...                     # open_connections > 0 and status 89 s old -> key; 91 s old -> empty
def test_key_change_rewrites_projection_of_running_workers_only(): ...
```

- [ ] **Step 2: Run** `tests/test_tailnet_ssh_links.py`. Expected: FAIL, import error.
- [ ] **Step 3: Implement.** Reconcile inside one `update_links`: remove (`remove_links`) links whose agent left `config.agents`, whose `link_ineligibility` is `not_authorized`, whose stored `worker_key` differs from `resolve_target(...).worker_key`, or that are unenrolled 24 h after `created_at`; in `auth_key` mode, at most every 60 s, add `new_link(...)` for each `list_workers()` handle with `ssh_endpoint`, a `user_agent_worker_key_requester`, and no link of any `desired`.
  `allowed_login` is `config.tailnet_ssh.logins.get(requester_id)` in `auth_key` mode, else `link.enrolled_login`.
  `connect` runs, in order: link exists and is `on` (404, 409); `peer_login == allowed_login` (403); `link_ineligibility` is None (409, detail the reason); in a thread, `ensure_worker(WorkerSpec(target.worker_key, private_agent_names=target.private_agent_names))` under the lease as `_resolve_worker` in `api/computers.py` does (503 on `WorkerBackendError` or a handle not `ready`); `handle.ssh_endpoint` set (409, `"This worker has no SSH port; with Docker, MINDROOM_DOCKER_WORKER_PUBLISH_HOST must not be a wildcard address."`); `write_projection`; the shell request from `agent_terminals` + `worker_call` (409 `"This agent has no shell tool."` when either is None, 409 when `call.request.worker_key != link.worker_key`); `POST {call.api_root}/ssh/start` with `call.headers` and `{"request": call.request.model_dump(mode="json")}`, 30 s timeout (503 with the runner's detail); return `(endpoint.host, endpoint.port)`.
  Log one `tailnet_ssh_connect` line with `link_id`, `peer_login`, `peer_node`.
- [ ] **Step 4: Run** the file (PASS) and the tach check.
- [ ] **Step 5: Commit** `feat(tailnet-ssh): link service with connect sequence and lifecycle`.

### Task 7 (PR 1a): Internal listener, wiring, activity touch

**Files:**
- Create: `src/mindroom/api/tailnet_ssh_gateway.py`
- Modify: `src/mindroom/api/app_state.py` (field `tailnet_ssh_status: GatewayStatus | None = None` beside `computer_sessions`), `src/mindroom/orchestrator.py:1662-1682` (add the listener to the `async with` beside `serve_plugin_origins`), `src/mindroom/api/workers.py` and `src/mindroom/api/main.py` (`computer_worker_keys` → `held_worker_keys` in `maintain_leased_workers`, `_cleanup_workers_once`, and their callers), `tests/api/test_api.py`, `tests/api/test_computers.py` (the rename), `tach.toml`
- Test: `tests/api/test_tailnet_ssh_gateway_api.py`

**Interfaces:**
- Consumes: `desired_links`, `record_status`, `connect`, `ConnectRefused`, `active_worker_keys`, `GatewayStatus` (Task 6); `serve_listener`, `listener_port`, `SCRIPT_GATEWAY_PORT_ENV` (`api/script_gateway.py`); `config_lifecycle.read_app_committed_runtime_config(app)[0]`, `config_lifecycle.app_state(app).runtime.agent_reply_memberships`; `public_worker_startup_env`, `isolated_worker_runtime_env` (`runtime_env_policy.py`).
- Produces:
  - `TOKEN_ENV = "MINDROOM_TAILNET_SSH_GATEWAY_TOKEN"`, `PORT_ENV = "MINDROOM_TAILNET_SSH_PORT"`, `DEFAULT_PORT = 8769`.
  - `gateway_app(api_app: FastAPI, token: str) -> FastAPI` with the three routes of **Contracts** and no others (`openapi_url=None`, `docs_url=None`, `redoc_url=None`).
  - `serve_tailnet_ssh_gateway_listener(runtime_paths, *, host: str, api_port: int, api_app: FastAPI, log_level: str) -> AsyncIterator[None]` (async context manager): yields without serving while the token is unset; otherwise sets `app_state(api_app).tailnet_ssh_status = GatewayStatus()`, serves with `serve_listener(..., name="tailnet_ssh_gateway_listener", ws="none", access_log=False)`, and clears the field on exit.
    `ValueError` when the port equals `api_port` or the script gateway's.
  - `tailnet_ssh_status(app: FastAPI) -> GatewayStatus | None`; `active_tailnet_ssh_worker_keys(app: FastAPI) -> frozenset[str]` (empty when the feature is off).

- [ ] **Step 1: Write the failing tests** against `TestClient(gateway_app(api_app, "tok"))` with a published test config.

```python
@pytest.mark.parametrize("route", [("GET", "/links"), ("POST", "/status"), ("POST", "/links/abc/connect")])
def test_routes_need_the_bearer_token(route): ...          # none -> 401; "Bearer wrong" -> 401
def test_listener_app_has_only_three_routes(): ...          # {r.path for r in app.routes} == {"/links", "/status", "/links/{link_id}/connect"}
def test_get_links_returns_desired_rows(): ...              # a stored "on" link -> one row with the six contract keys
def test_status_is_stored_and_answers_204(): ...            # tailnet_ssh_status(api_app).links["<id>"].state == "running"
def test_connect_answers_endpoint_or_refusal_detail(): ...  # monkeypatched connect: (h, 2222) -> 200 {"host": h, "port": 2222};
                                                            # ConnectRefused(409, "x") -> 409 {"detail": "x"}
def test_config_not_loaded_answers_503(): ...
def test_listener_is_off_without_token_and_refuses_the_api_port(): ...
def test_gateway_token_never_reaches_a_worker_env(): ...    # public_worker_startup_env({TOKEN_ENV: "t"}) == {} and
                                                            # isolated_worker_runtime_env({TOKEN_ENV: "t"}) == {}
def test_both_maintenance_paths_touch_ssh_workers(): ...    # a status with open_connections=1: POST /api/workers/cleanup and
                                                            # _cleanup_workers_once each call touch_worker(link.worker_key)
```

- [ ] **Step 2: Run** `tests/api/test_tailnet_ssh_gateway_api.py`. Expected: FAIL, import error.
- [ ] **Step 3: Implement.** The token is compared with `secrets.compare_digest`. Blocking store work runs in `asyncio.to_thread`. Both maintenance call sites pass `held_worker_keys=active_computer_worker_keys(app) | active_tailnet_ssh_worker_keys(app)`, read on the event loop as the Computer keys are.
- [ ] **Step 4: Run** the file, `tests/api/test_computers.py`, `tests/api/test_api.py`, `tests/test_script_gateway_listener.py` (PASS) and the tach check.
- [ ] **Step 5: Commit** `feat(tailnet-ssh): gateway-only listener and SSH activity keeps workers up`.

### Task 8 (PR 1a): User API

**Files:**
- Create: `src/mindroom/api/tailnet_ssh.py`
- Modify: `src/mindroom/api/main.py:505` (include the router after `egress_credentials_router`), `tach.toml`
- Test: `tests/api/test_tailnet_ssh_api.py` (set up as `tests/api/test_egress_credentials_api.py` does: `_trust_signed_users`, `_signed_headers`, a portal fixture)

**Interfaces:**
- Consumes: `require_connections_user`, `CONNECTIONS_HEADERS`, `resolve_connection_user`, `resolve_connection_agent`, `require_connections_same_origin` (`api/auth.py`, `api/connection_agents.py`), used as `_egress_user_accepting` and `_require_egress_same_origin` in `api/egress_credentials.py` use them; `tailnet_ssh_status(app)` (Task 7); Tasks 1, 2, 6; for the GitHub login, the context `_credential_context` builds in `api/github_requests.py` (`build_requester_execution_identity(agent_name="oauth", ...)`, `resolve_oauth_credential_context(github_oauth_provider(), ...)`), `load_oauth_credentials_status(context, create_store=False)`, `oauth_verified_claim(credentials, "login")`.
- Produces: `router = APIRouter(prefix="/api/connections/tailnet-ssh")` with:

| Route | Body | Answers |
| --- | --- | --- |
| `GET ""` | | The **Contracts** document. |
| `PUT /keys` | `{"keys": [str]}` | 204; 422 with `normalize_public_key`'s sentence or the 20-key limit. |
| `POST /keys/github` | | `{"added": int}`; 409 `"Connect a GitHub account first."`; 502 when GitHub fails. |
| `PUT /agents/{agent}` | `{"hostname": str}` | 204; 404 unknown or unusable agent; 422 hostname not matching `HOSTNAME`; 409 with the reason code when ineligible; 409 `"Add an SSH public key first."`; 409 `"Disconnect first to change the hostname."`; 409 `"Still disconnecting; try again in a few seconds."` |
| `POST /agents/{agent}/login` | | 204 (sets `login_after = now`); 404 without a link. |
| `DELETE /agents/{agent}` | | 204 (`remove_links`, `park=True` when enrollment is `auth_key`); 404 without a link. |

- [ ] **Step 1: Write the failing tests**

```python
def test_feature_off_reports_disabled_and_mutations_404(portal): ...     # no status object
def test_listing_shows_only_my_tailnet_ssh_agents(portal): ...           # bob's agent and agents without the field are absent
def test_another_persons_link_is_invisible(portal): ...                  # bob's GET has link null for a shared-name agent; bob's DELETE -> 404
def test_login_url_goes_only_to_the_owner(portal): ...                   # alice sees it; it is absent from bob's whole response text
def test_ineligible_agent_reports_reason_and_detail(portal): ...         # {"eligible": false, "reason": "joined_directory", "detail": "other/specs"}
def test_put_keys_normalizes_and_replaces(portal): ...
def test_put_keys_refuses_private_key_with_a_sentence(portal): ...       # 422, detail contains "private key"
def test_removing_a_key_rewrites_projection_and_sets_closed_before(portal): ...
def test_github_import_adds_keys(portal, monkeypatch): ...               # _fetch_github_keys patched -> {"added": 2}; no account -> 409
def test_connect_creates_link_with_session_worker_key(portal): ...       # stored worker_key == resolve_connection_agent(...).target.worker_key
                                                                         # == links.resolve_target(...).worker_key; body has no key or user field
@pytest.mark.parametrize("hostname", ["Mind", "-a", "a_b", "a" * 64, ""])
def test_connect_refuses_bad_hostnames(portal, hostname): ...            # 422
def test_connect_needs_a_key_and_eligibility(portal): ...                # 409 twice, second detail == "not_enabled"
def test_connect_turns_an_off_link_back_on(portal): ...
def test_login_moves_login_after(portal): ...
def test_disconnect_sets_removing_and_parks_in_operator_mode(portal): ...
def test_mutations_refuse_cross_origin_and_query_parameters(portal): ...
def test_link_state_and_worker_are_reported(portal): ...                 # running status + handle status "starting" -> state "enrolled", worker "waking"
```

- [ ] **Step 2: Run** `tests/api/test_tailnet_ssh_api.py`. Expected: FAIL, 404 on every route.
- [ ] **Step 3: Implement.** The requester and agent come only from the session (`resolve_connection_user`, `resolve_connection_agent`); no body or path carries a Matrix ID or worker key.
  `_fetch_github_keys(login: str) -> list[str]` uses `httpx.AsyncClient(follow_redirects=False, timeout=10)` on `https://github.com/<login>.keys`, reads at most 64 KiB, and keeps the lines `normalize_public_key` accepts.
  Key changes call `refresh_projections`. `link.worker` comes from `list_workers()` in a thread (`ready` → `ready`, `starting` → `waking`, otherwise `asleep`), read only when a link of the requester is `enrolled`.
  `knowledge_bases` is the agent's configured list; `shared_directories` the `owner/name` of directories it owns.
- [ ] **Step 4: Run** the file and `tests/api/test_egress_credentials_api.py` (PASS), the tach check, then on gce-dev the full backend suite for PR 1a.
- [ ] **Step 5: Commit** `feat(tailnet-ssh): connections API for keys and links`, then open **PR 1a**.

---

### Task 9 (PR 1b): Gateway — primary client, reconcile, status

**Files:**
- Create: `tailnet-gateway/go.mod` (module `github.com/mindroom-ai/mindroom/tailnet-gateway`; the `go` line and the `tailscale.com v1.104.7` requirement copied from `spike-gateway/go.mod`), `tailnet-gateway/primary.go`, `tailnet-gateway/reconcile.go`, `tailnet-gateway/primary_test.go`, `tailnet-gateway/reconcile_test.go`

**Interfaces:**
- Consumes: the internal-listener **Contracts**.
- Produces (package `main`):

```go
type DesiredLink struct { ID, Hostname, Desired string; AllowedLogin *string; ClosedBefore, LoginAfter float64 }  // json tags per Contracts
type LinkStatus struct { State string; LoginURL, Tailnet, DNSName, LoginName, KeyExpiry, LastError *string; OpenConnections int }
type Primary interface {
    Links(ctx context.Context) ([]DesiredLink, error)
    PostStatus(ctx context.Context, enrollment string, links map[string]LinkStatus) error
    Connect(ctx context.Context, id, peerLogin, peerNode string) (host string, port int, err error)  // *RefusedError{Detail} on 4xx/5xx
}
func NewPrimary(baseURL, token string) Primary                    // Connect uses a 610 s timeout, the others 10 s
type NodeObserved struct { State string; LoginURL, Tailnet, DNSName, LoginName, KeyExpiry *string }  // State: needs_login|needs_approval|running
type Node interface {
    Listener() net.Listener                                        // the tailnet :22 listener
    WhoIs(ctx context.Context, remoteAddr string) (login, node string, err error)  // login "" for a tagged device
    Observed() NodeObserved
    Relogin(ctx context.Context) error
    Logout(ctx context.Context) error
    Close() error
}
type NodeFactory func(dir, hostname string) (Node, error)
type Gateway struct { /* primary, factory, stateDir, enrollment, clock; nodes and connections by link id */ }
func NewGateway(p Primary, f NodeFactory, stateDir, enrollment string) *Gateway
func (g *Gateway) Reconcile(ctx context.Context) error             // one pass
func (g *Gateway) Run(ctx context.Context)                         // Reconcile at start and every 5 s; status on change and every 30 s
func (g *Gateway) Status() map[string]LinkStatus
```

- [ ] **Step 1: Write the failing tests** with an `httptest` fake primary and a fake `Node` whose listener is `net.Listen("tcp", "127.0.0.1:0")`.

```go
func TestPrimarySendsBearerAndParsesLinks(t *testing.T)            // header "Authorization: Bearer tok"; null allowed_login -> nil
func TestConnectRefusalCarriesDetail(t *testing.T)                 // 409 {"detail":"scope"} -> RefusedError.Detail == "scope"
func TestOnStartsNodeInDirNamedByID(t *testing.T)                  // factory called with <state>/<id> and the hostname; never the hostname as dir
func TestUnlistedStateIsStoppedAndKept(t *testing.T)               // dir <state>/gone exists before and after; its node is closed
func TestFailedOrEmptyLinksAnswerChangesNothing(t *testing.T)      // primary 500, then [] : dirs kept, no Logout call
func TestRemovingLogsOutDeletesDirAndReportsRemoved(t *testing.T)  // Logout called, dir gone, Status()[id].State == "removed"
func TestRemovingWithoutStateStillReportsRemoved(t *testing.T)
func TestRemovingDeletesEvenWhenLogoutFails(t *testing.T)
func TestLoginAfterChangeCallsRelogin(t *testing.T)                // only when the node is needs_login and the value moved
func TestNeedsLoginAfterRunningCallsReloginOnce(t *testing.T)
func TestStatusPostedOnChangeAndCarriesEnrollment(t *testing.T)    // body enrollment == "auth_key"; a second identical pass posts nothing
func TestInvalidLinkIDIsIgnored(t *testing.T)                      // id "../x" never touches the filesystem
```

- [ ] **Step 2: Run** on gce-dev: `cd tailnet-gateway && go test ./...`. Expected: FAIL to compile (undefined `NewGateway`).
- [ ] **Step 3: Implement.** A link id must match `^[0-9a-f]{16}$`.
  A pass that cannot read `/links` returns the error and changes nothing.
  `removing`: close the link's connections, start the node if it is not running, `Logout` with a 10 s timeout, `Close`, `os.RemoveAll(<state>/<id>)`, and keep reporting `removed` while the link is listed.
  `suspended` keeps the node and is handled by Task 10's accept path.
- [ ] **Step 4: Run** `go test ./... && go vet ./...` on gce-dev. Expected: PASS.
- [ ] **Step 5: Commit** `feat(tailnet-gateway): reconcile tsnet nodes against the primary`.

### Task 10 (PR 1b): Gateway — accept path, tsnet node, image, CI

**Files:**
- Create: `tailnet-gateway/serve.go`, `tailnet-gateway/serve_test.go`, `tailnet-gateway/node.go`, `tailnet-gateway/main.go`, `tailnet-gateway/Dockerfile`, `.github/workflows/tailnet-gateway.yml`
- Modify: `.github/workflows/build-mindroom.yml` (both `target` matrices gain `image_name: mindroom-tailnet-gateway`, the build one with `dockerfile: tailnet-gateway/Dockerfile`)

**Interfaces:**
- Consumes: `Gateway`, `Node`, `Primary` (Task 9); gateway environment of **Contracts**.
- Produces:
  - `func (g *Gateway) serve(ctx context.Context, link DesiredLink, n Node)`: the accept loop of one node.
  - `func NewTsnetNode(loginServer, authKey string, tags []string) NodeFactory` in `node.go`: `tsnet.Server{Dir, Hostname, ControlURL, AuthKey, AdvertiseTags}`, `Start()` (never `Up()`), state and `BrowseToURL` from `LocalClient().WatchIPNBus(ctx, ipn.NotifyInitialState)`, the rest from `Status()` as `spike-gateway/main.go` reads it, `Listen("tcp", ":22")`, `WhoIs`, `StartLoginInteractive`, `Logout`.
  - Image `ghcr.io/mindroom-ai/mindroom-tailnet-gateway`: static binary (`CGO_ENABLED=0`) on `gcr.io/distroless/static:nonroot`, built from the repository root as context, entrypoint `/tailnet-gateway`.

- [ ] **Step 1: Write the failing tests** (fake node; the "worker" is a local TCP listener that writes `SSH-2.0-test\r\n`).

```go
func TestPeerLoginMismatchClosesBeforeAnyConnectCall(t *testing.T)  // WhoIs "eve@x" vs allowed "bas@x": conn closed, primary saw 0 connects
func TestTaggedDeviceAndNilAllowedLoginAreRefused(t *testing.T)     // login "" refused; AllowedLogin nil refuses everyone
func TestPipeCarriesBytesBothWays(t *testing.T)                     // client reads "SSH-2.0-test\r\n", worker reads what the client wrote
func TestPreBannerLinesWhileConnectIsSlow(t *testing.T)             // connect takes 7 s (fake clock): client reads exactly two
                                                                    // "MindRoom: waking worker\r\n" lines, then "SSH-2.0-test\r\n"
func TestFastConnectSendsNoPreBanner(t *testing.T)
func TestSeventhConnectInAMinuteIsClosedWithoutCallingPrimary(t *testing.T)
func TestRefusalClosesAndSetsLastError(t *testing.T)                // Status()[id].LastError == "scope"
func TestClosedBeforeChangeClosesAllOpenConnections(t *testing.T)   // value moves to a time *earlier* than the connection's
                                                                    // local open time: still closed; an unchanged value closes nothing
func TestSuspendedRefusesNewAndClosesOpen(t *testing.T)
func TestOpenConnectionsCountsOnlyAfter75Seconds(t *testing.T)      // 74 s -> 0, 76 s -> 1
```

- [ ] **Step 2: Run** `go test ./...` on gce-dev. Expected: FAIL to compile (undefined `serve`).
- [ ] **Step 3: Implement.** Per accepted connection: `WhoIs`; refuse unless the login equals `*AllowedLogin`; apply the rate limit (six `Connect` calls in any 60 s per link); call `Connect` while a ticker writes the pre-banner line every 3 s; dial `host:port` with a 10 s timeout; copy both ways until either side closes.
  `main.go`: read the environment, fail without `TAILNET_SSH_GATEWAY_TOKEN`, call `envknob.SetNoLogsNoSupport()` and `netns.SetEnabled(false)` before any node starts, set enrollment to `auth_key` when `TAILNET_SSH_AUTH_KEY` is non-empty and `login` otherwise, then `Run`.
  The binary has no flag or code path that dials a tailnet address, runs a command, or talks to Docker or Kubernetes.
  `tailnet-gateway.yml` has job `go-test` (`go vet ./... && go test ./...` in `tailnet-gateway/`, on pushes and pull requests touching `tailnet-gateway/**`).
- [ ] **Step 4: Run** on gce-dev: `go test ./... && go vet ./...` (PASS) and `docker build -f tailnet-gateway/Dockerfile -t mindroom-tailnet-gateway:dev .` (succeeds); `docker run --rm mindroom-tailnet-gateway:dev` exits non-zero naming `TAILNET_SSH_GATEWAY_TOKEN`.
- [ ] **Step 5: Commit** `feat(tailnet-gateway): accept path, tsnet node, image and CI`.

### Task 11 (PR 1b): Chart

**Files:**
- Create: `cluster/k8s/runtime/templates/tailnet-ssh.yaml`
- Modify: `cluster/k8s/runtime/values.yaml` (the `tailnetSsh` block of the spec's Deployment section, verbatim, plus `extraEgress: null`), `cluster/k8s/runtime/templates/deployment.yaml` (beside the `scriptGateway` port at :196 and env at :352), `cluster/k8s/runtime/templates/validate.yaml` (beside the `egressBroker.port` check at :441), `cluster/k8s/runtime/README.md` (section "Tailnet SSH")
- Test: `tests/test_runtime_chart_tailnet_ssh.py` (helpers and constants as `tests/test_runtime_chart_script_gateway.py`)

**Interfaces:**
- Consumes: gateway environment and `TOKEN_ENV`/`PORT_ENV` names (**Contracts**, Task 7); helpers `mindroom-runtime.fullname`, `.labels`, `.selectorLabels`, `.workerPodLabels`, `.workerNamespace`, `.egressProxyDnsPeers`; the lookup-or-generate pattern of `api-key-secret.yaml`; the control plane's `nodeSelector`, `affinity`, `tolerations`.
- Produces, all named `<fullname>-tailnet-ssh` unless noted, all only when `tailnetSsh.enabled`:
  - Secret with key `token` (48 random characters, kept across upgrades).
  - PersistentVolumeClaim, `ReadWriteOnce`, `tailnetSsh.persistence`.
  - Service selecting the control-plane pods on `tailnetSsh.port`.
  - Deployment: `replicas: 1`, `strategy.type: Recreate`, pod labels `app.kubernetes.io/component: tailnet-ssh` plus name and instance, image `<repository>:<tag or .Chart.AppVersion>`, `automountServiceAccountToken: false`, `runAsNonRoot: true`, `readOnlyRootFilesystem: true`, `allowPrivilegeEscalation: false`, `capabilities.drop: [ALL]`, `seccompProfile.type: RuntimeDefault`, `fsGroup: 65532`, claim at `/state`, an `emptyDir` at `/tmp`, env `TAILNET_SSH_PRIMARY_URL=http://<fullname>-tailnet-ssh.<release namespace>.svc.cluster.local:<port>`, `TAILNET_SSH_GATEWAY_TOKEN` (secretKeyRef), `TAILNET_SSH_STATE_DIR=/state`, `TS_NO_LOGS_NO_SUPPORT=true`, and when set `TAILNET_SSH_AUTH_KEY` (secretKeyRef `authKeySecret`, key `auth-key`), `TAILNET_SSH_LOGIN_SERVER`, `TAILNET_SSH_TAGS` (comma-joined), `HTTPS_PROXY`.
  - Primary container: port named `tailnet-ssh`, env `MINDROOM_TAILNET_SSH_PORT` and `MINDROOM_TAILNET_SSH_GATEWAY_TOKEN` (secretKeyRef).
  - NetworkPolicy `<fullname>-tailnet-ssh-workers` in the worker namespace: pods with the worker labels and `mindroom.ai/tailnet-ssh: "true"` admit gateway pods of the release namespace on TCP 2222.
  - NetworkPolicy `<fullname>-tailnet-ssh-primary` (only when `networkPolicy.create`, as `script-gateway.yaml` does for its port): control-plane pods admit gateway pods on `tailnetSsh.port`.
  - NetworkPolicy `<fullname>-tailnet-ssh`: gateway pods, `policyTypes: [Ingress, Egress]`, no ingress rule; egress to the DNS peers on 53 UDP and TCP, to control-plane pods on `tailnetSsh.port`, to the labelled worker pods on TCP 2222, the `extraEgress` rules, and, only while `httpsProxy` is empty, TCP 443 to `0.0.0.0/0` except `10.0.0.0/8`, `172.16.0.0/12`, `192.168.0.0/16`, `100.64.0.0/10`, `169.254.0.0/16`.
    No UDP rule besides DNS.
  - `validate.yaml` failures: `tailnetSsh.enabled requires workers.backend=kubernetes`; `tailnetSsh.port must differ from runtime.apiPort, scriptGateway.port, and egressBroker.port`; `tailnetSsh.httpsProxy requires tailnetSsh.extraEgress to admit the proxy`.

- [ ] **Step 1: Write the failing tests**

```python
def test_disabled_renders_nothing_and_no_primary_env(): ...
def test_gateway_deployment_is_single_recreate_and_hardened(): ...   # every securityContext value above; no service account token
def test_gateway_env_points_at_primary_and_never_logs_to_tailscale(): ...
def test_auth_key_login_server_tags_and_proxy_are_optional_env(): ...
def test_primary_gets_port_and_token_from_the_secret(): ...
def test_worker_policy_admits_gateway_only_to_labelled_pods_on_2222(): ...
def test_primary_policy_admits_gateway_on_the_listener_port(): ...    # with networkPolicy.create=true; absent without
def test_gateway_policy_has_no_ingress_and_only_named_egress(): ...   # ports == {53/UDP, 53/TCP, 8769, 2222, 443}; one ipBlock with the five excepts
def test_https_proxy_drops_public_443_and_requires_extra_egress(): ...
@pytest.mark.parametrize("value", ["workers.backend=static_runner", "tailnetSsh.port=8765", "tailnetSsh.port=8767", "tailnetSsh.port=8768"])
def test_validation_fails_with_a_sentence(value): ...   # 8765 API, 8767 script gateway, 8768 egress broker
def test_worker_egress_policy_and_squid_are_unchanged(): ...          # the worker NetworkPolicy and approved-egress resources
                                                                      # render identically with tailnetSsh on and off
```

- [ ] **Step 2: Run** `tests/test_runtime_chart_tailnet_ssh.py` on gce-dev. Expected: FAIL (no such resources).
- [ ] **Step 3: Implement** the template in the order above; the README section states the three policies, the two enrolment modes, that the proxy must not be the workers' Squid, and the memory figure (about 25 MB plus 7 MB per link).
- [ ] **Step 4: Run** the file and `tests/test_runtime_chart_script_gateway.py` (PASS) on gce-dev, and `helm lint cluster/k8s/runtime --set tailnetSsh.enabled=true --set workers.backend=kubernetes --set workers.sandbox.proxyToken.value=t`.
- [ ] **Step 5: Commit** `feat(chart): tailnet SSH gateway deployment and policies`.

### Task 12 (PR 1b): Headscale integration test, CI job, posture row

**Files:**
- Create: `tests/test_tailnet_ssh_headscale_e2e.py`
- Modify: `.github/workflows/tailnet-gateway.yml` (job `headscale-e2e`), `docs/architecture/security-posture.md` (one row or bullet under worker network access: the gateway, its token and port, the three admission layers, forwarding off, node keys outside workers)

**Interfaces:**
- Consumes: `write_projection`, `authorized_key_lines` (Task 4); `new_link` (Task 2); `sshd_argv`, `session.LAUNCH_PATH` (Task 5); the gateway image (Task 10); `docker_worker_security_options()` (`workers/backends/worker_security.py`); the internal-listener **Contracts**.
- Produces: one module, `pytestmark = [pytest.mark.e2e, pytest.mark.timeout(1200), pytest.mark.skipif(os.environ.get("MINDROOM_TAILNET_SSH_E2E") != "1", reason="needs Docker, Headscale and the built images")]`, images from `MINDROOM_TAILNET_SSH_E2E_GATEWAY_IMAGE` and `MINDROOM_TAILNET_SSH_E2E_WORKER_IMAGE`.

- [ ] **Step 1: Write the test module.** Module fixtures start: Headscale 0.29.7 with users `alice` and `bob`; a fake primary (FastAPI on a free port serving the three routes from an in-memory list of rows, with a per-link connect delay and target); one worker container per link from the worker image with `--user 1000:1000 --read-only --tmpfs /tmp --cap-drop ALL`, `docker_worker_security_options()`, the projection directory written by `write_projection` bind-mounted read-only, `2222` published on `127.0.0.1`, a hand-written `launch.json` (`cwd` a writable bind mount), started with `sshd_argv(...)`; the gateway image with `--network host`, `TAILNET_SSH_LOGIN_SERVER` at Headscale and a named state volume; `tailscale/tailscale` userspace clients joined by pre-auth key.
  SSH runs from the test host with `-o ProxyCommand='docker exec -i <client> tailscale nc %h %p'` and a key generated in `tmp_path`.

```python
def test_enrol_by_login_url_then_exec(stack): ...           # status login_url -> `headscale auth register --auth-id <id> --user alice`
                                                            # -> status running; `ssh host id -u` prints 1000; `pwd` prints the launch cwd
def test_enrol_by_pre_auth_key(stack_auth_key): ...         # no login_url ever reported; state running
def test_pty_scp_sftp_rsync(stack): ...                     # `ssh -tt host tty` succeeds; a file round-trips by scp, sftp and rsync -a with equal sha256
def test_forwarding_is_refused(stack): ...                  # -L, -R, -W fail; with -A, `echo $SSH_AUTH_SOCK` is empty
def test_wrong_key_and_password_are_refused(stack): ...
def test_second_user_in_same_tailnet_is_refused_before_connect(stack): ...   # bob's client: connection closed; fake primary saw no connect call
def test_two_links_two_users_cannot_cross(stack): ...       # alice cannot reach bob's node and bob cannot reach alice's
def test_key_removal_cuts_an_open_session(stack): ...       # `ssh host sleep 300` running; rewrite projection without the key and move
                                                            # closed_before; the client exits within 15 s and a new login is refused
def test_connection_held_through_a_120s_start(stack): ...   # connect delay 120 s: plain `ssh host true` succeeds, `ssh -v` shows "MindRoom: waking worker"
def test_removing_logs_out_and_gateway_restart_keeps_others(stack): ...  # removed reported and the Headscale node gone; after
                                                            # `docker restart` the other link is running again with no login_url
```

- [ ] **Step 2: Run** on gce-dev in `/work/devagent-tailnet-e2e`: build both images, then `MINDROOM_TAILNET_SSH_E2E=1 MINDROOM_TAILNET_SSH_E2E_GATEWAY_IMAGE=... MINDROOM_TAILNET_SSH_E2E_WORKER_IMAGE=... uv run pytest tests/test_tailnet_ssh_headscale_e2e.py -x -n 0 --no-cov`. Expected: every case PASS; a failure here is a defect in Tasks 4, 5, 9 or 10 and is fixed there, with its unit test.
- [ ] **Step 3: Add the CI job** `headscale-e2e` (ubuntu-latest, on pull requests touching `tailnet-gateway/**`, `src/mindroom/tailnet_ssh/**`, `src/mindroom/api/worker_ssh.py`, `local/instances/deploy/Dockerfile.mindroom`, or the test): build the two images with buildx and the registry cache `build-mindroom.yml` uses, install `rsync`, run the command of Step 2.
- [ ] **Step 4: Verify** locally that without the variable the module reports its cases skipped, and that the docs build on gce-dev (`uv run zensical build`, exit 0).
- [ ] **Step 5: Commit** `test(tailnet-ssh): Headscale integration job` and open **PR 1b**.

---

### Task 13 (PR 2): Portal card

**Files:**
- Create: `frontend/src/connections/TailnetSsh.tsx`, `frontend/src/connections/TailnetSsh.test.tsx`
- Modify: `frontend/src/connections/Connections.tsx:188` (render `<TailnetSsh />` directly above `<ConnectedClients />`)

**Interfaces:**
- Consumes: the user API of **Contracts** and Task 8's routes and refusal details; `requestConnection` and `errorText` (`./request`); `ConfirmDialog`; the `Card`, `Button`, `Alert` components `ConnectedClients.tsx` uses.
- Produces: `export function TailnetSsh()`, which renders `null` while loading, when `enabled` is false, or when `agents` is empty.

Copy the card must show, exactly:

| State | Text and controls |
| --- | --- |
| Heading | `SSH access over Tailscale` |
| No key | A textarea labelled `SSH public key`, button `Save key`, and `Import from GitHub` only when `github_login` is set. |
| Not linked | Agent picker when more than one eligible agent; field `Hostname` prefilled with `default_hostname`; button `Connect to my tailnet`; the line `A shell here can read this agent's workspace` followed by its knowledge bases and shared directories when there are any; the line `MindRoom will hold a device in your tailnet. Limit it with your tailnet's access rules.` |
| `pending_login` | Link `Sign in to Tailscale` (`href` the login URL, `target="_blank"`, `rel="noreferrer"`); a spinner without the link while `login_url` is null. |
| `needs_approval` | ``Ask your tailnet admin to approve `<hostname>`.`` |
| `enrolled` | ``Connected to `<tailnet>` as `<login_name>` ``, the worker as `ready`, `waking` or `asleep`, the three copy blocks, `Disconnect` (through `ConfirmDialog`). |
| `needs_login` | Button `Reconnect` (posts `/login`) and `last_error` when set. |
| `suspended` / ineligible | One sentence by reason: `backend` → `SSH needs a Docker or Kubernetes worker backend.`; `not_enabled` → `SSH is not enabled for this agent.`; `scope` → `SSH needs an agent with its own worker per person.`; `shared_workspace` → `SSH is only available for agents that belong to you alone.`; `joined_directory` → ``Leave the shared directory `<detail>` to use SSH with this agent.``; `not_authorized` → `You may not use this agent.` |
| `removing` | `Disconnecting…` |

Copy blocks when enrolled: the `Host` block of the spec's User experience section with `<hostname>` and `<dns_name>` filled in; `<dns_name> <host_public_key>` labelled `known_hosts`; `ssh mindroom@<dns_name>`. Below them: `The first connection after the worker slept can take a minute or more. Remove any ConnectTimeout below 5 seconds.` and the worker's `last_error` when set.

- [ ] **Step 1: Write the failing tests** (mock `fetch` as `ConnectedClients.test.tsx` does).

```tsx
it("renders nothing when the feature is off or no agent has SSH")
it("asks for a key first and offers GitHub import only with a connected account")
it("connects with the default hostname and sends only the hostname")          // PUT body is {"hostname":"mind-bas"}
it("renders the login URL only as a link target")   // container text does not contain the URL; the anchor's href equals it
it("polls every 3 s while pending_login and stops when enrolled")             // fake timers
it("shows tailnet, login, worker state and the three copy blocks when enrolled")
it("shows the approval, reconnect, suspended and removing states")            // one assertion per row above
it("names the joined directory in the not-available sentence")
it("shows the server's sentence when saving a key fails")                     // 422 detail rendered
it("disconnects after confirmation")
```

- [ ] **Step 2: Run** `cd frontend && bun run test --run src/connections/TailnetSsh.test.tsx`. Expected: FAIL, module not found.
- [ ] **Step 3: Implement** one component in the shape of `ConnectedClients.tsx` (abortable load, `busy` state, error `Alert`). Poll `GET` every 3 s while any link is `pending_login`, `needs_approval`, or `removing`, and every 30 s otherwise.
- [ ] **Step 4: Run** the test file, `bun run type-check`, `bun run lint` in `frontend/` (PASS); `Connections.test.tsx` still passes.
- [ ] **Step 5: Commit** `feat(connections): SSH access over Tailscale card`.

### Task 14 (PR 2): Docs

**Files:**
- Create: `docs/tailnet-ssh.md`
- Modify: `zensical.toml` (nav entry `{ "SSH over Tailscale" = "tailnet-ssh.md" }` after "Connections Portal" at :101), `docs/connections-portal.md` (one line linking the card), `docs/deployment/docker.md` (the gateway container), `docs/deployment/kubernetes.md` (pointer to the chart README section)

**Interfaces:**
- Consumes: everything shipped in PR 1a and 1b; no code.
- Produces: `docs/tailnet-ssh.md` with these sections, one sentence per line:
  - **What it is** (inbound only; the gateway holds a device in your tailnet).
  - **Set up** (key, hostname, sign in, the `Host` block).
  - **Which agents qualify** (the six conditions in plain words, the six reason sentences of Task 13, knowledge bases and owned shared directories are fine).
  - **Config** (`tailnet_ssh: true` on an agent or a `personal_agents` template's `agent:`, root `tailnet_ssh.logins` and `tailnet_ssh.authorized_keys`, copied from the spec's Config surface).
  - **What a shell can read and do** (uid 1000, the workspace as `~`, the agent's proxy and Agent Keys, no forwarding).
  - **Security notes** (never `ssh -A`; VS Code Remote-SSH trusts the remote and the worker is not trusted; tagged devices are refused; three admission layers).
  - **Operators** (enable with the token and port; Kubernetes values; Docker: gateway with `network_mode: "service:<primary>"` or `--network host`, a named volume at `/state`, the environment of **Contracts**, port 8769 never published, and `MINDROOM_DOCKER_WORKER_PUBLISH_HOST` must be a loopback or private address, never `0.0.0.0`; operator key: a reusable, tagged auth key in `TAILNET_SSH_AUTH_KEY` with `tailnet_ssh.logins`; Headscale: `TAILNET_SSH_LOGIN_SERVER`; egress: TCP 443 to the control plane and DERP, no UDP, an optional `HTTPS_PROXY` that is not the workers' Squid; a custom worker image keeps a passwd entry for uid 1000 and `openssh-server`).
  - **Troubleshooting** (first connect is slow; `ConnectTimeout`; `needs_login` and Reconnect; a `-1` hostname after the gateway volume was lost; disconnect leaves the machine in the Tailscale console if the control plane was unreachable).

- [ ] **Step 1: Write the pages.** Every YAML and shell block is copied from the spec or from Task 11's values, not rephrased.
- [ ] **Step 2: Build** `uv run zensical build` on gce-dev. Expected: exit 0 and `site/tailnet-ssh/index.html` exists.
- [ ] **Step 3: Check the claims**: each config key, environment variable, and card sentence in the page appears verbatim in the code (`rg -F` for each).
- [ ] **Step 4: Commit** `docs: SSH into your worker over Tailscale` and open **PR 2**.

---

## Later PRs (outline only)

- **PR 3 (mindroom-chat + dev repo):** `scripts/agent-stack/stack.py --tailnet-ssh` implying `--client-sessions` and Docker workers (`build_worker_image`, `option_env`), a helper agent with `worker_scope: user_agent`, `tailnet_ssh: true`, `access.users` of the slot's test user.
- It starts Headscale (user `live`), the gateway with `--network host`, and a `tailscale/tailscale` userspace client, and prints the `headscale auth register` and `ssh -o ProxyCommand=...` lines.
- One Playwright e2e spec drives the card (key, connect, sign in, connected, disconnect); `live.py up --tailnet-ssh` passes the flag through; the spec's live run is recorded.
- **PR 4 (mindroom-infra), after a release with PRs 1a–2:** chart version, `tailnetSsh` in `helm_release.mindroom`, the gateway image in `scripts/images` and `stack/images.tf`, `tailnet_ssh: true` under `mind.agent` in `personal_agents.yaml`, a stack test for the three policies.
- Staging first with two real Tailscale accounts in two tailnets, plus the open-question-8 checks (tailnet name, login format, AuthURL lifetime, key expiry, non-OpenSSH clients, `RuntimeDefault` seccomp and `fsGroup` on the host-key mode in kind or staging, memory with large tailnets).
