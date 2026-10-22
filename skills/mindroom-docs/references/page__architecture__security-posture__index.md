# Security Posture

This page records MindRoom's security model, the `file_access` setting, and the behaviors that are intentional.
Use it to explain what an agent's tools can reach and to check a suspected vulnerability before reporting or fixing it.
If a finding contradicts an intentional behavior below, do not change the code; raise the posture with the maintainers and explain which scenario it fails to protect.

## Trust model

A worker container, reached through the sandbox proxy, is the only security boundary between an agent and the MindRoom runtime.

Tools that run arbitrary programs cannot be confined inside their own process, because a program started by a shell or Python tool ignores any in-process path or command check.
MindRoom therefore never filters the paths, commands, or imports of code-execution tools as a security measure; it isolates them only by routing them to a worker with `worker_tools`.

An agent whose code-execution tools run in the primary process is trusted with everything that process can reach, so restricting its other tools protects nothing.

An agent whose code-execution tools run in a worker must not reach more through its primary-process tools than its worker can.
Tools such as `browser`, `attachments`, `matrix_message`, `gmail`, and `google_drive` run in the primary process by default even then, so they follow the agent's [`file_access`](#file-access) setting, which by default keeps them inside the agent workspace.

Which Matrix user may drive an agent, act in a room, or approve a change is a separate question governed by access policy and requester authorization.
An agent or team mentioned in another entity's reply acts for the human or configured bot account that requested that reply, so neither can reach an entity whose `access` excludes them by asking a different entity to mention it.
Work no person started, such as a shared agent's automation run and the delegations, todos, mentions, and schedules that mention another agent it makes, acts as that agent's own account, which every other agent and team admits only through its own `access`.
When an agent-created schedule that mentions no agent reaches one of several responders through a router hand-off, the hand-off drops the managed requester, so the target checks the router's account, which the default `access` admits because the router joins every managed room.

## Protecting the primary from worker code

Hardening that protects the primary runtime and other tenants from untrusted worker code is always in scope.
Examples are symlinks or files planted in shared workspaces that the primary later follows, worker-writable metadata the primary trusts, Git config the primary executes, and secrets mounted or passed into workers.

### Config and state kept out of workers

Runners and dedicated workers never receive the primary's config file, its directory, its `.env`, a ConfigMap holding it, or the credential encryption key.
The config sent to runners with each request, and the config file projected into each Docker worker, hold only the fields runners resolve; models, MCP servers, plugin settings, and every other section stay in the primary, and a worker-routed call carries only the called tool's own inline overrides.

Of the agents' data, dedicated Docker and Kubernetes workers mount only workspaces and shared directories, never the agent state roots around them, so sessions, memory, learning, Mem0 data, and private-instance identity records stay out of every worker.
The Kubernetes `static_runner` sidecar still mounts `agents`, `private_instances`, and `shared_directories` read-write, so records the primary treats as authority live below the primary-only `tracking/` directory instead: the invited-room and pending-invite ledgers, personal-room records, conversation modes, the shared-directory membership ledger, and the primary's copy of each private-instance owner record, together with their locks.
The thread exporter writes into a private instance only for the requester that the primary's owner record names, so an owner record planted in `private_instances` gets no export.
Automations likewise run for a private instance only as the requester that record names, and post only in that requester's personal room, so their replies quoting private files never reach another room.
The knowledge graph database lives in the primary-only `knowledge_graph/` storage directory, and its embedded server opens no TCP port and listens on a Unix socket in a private temporary directory of the primary, so no worker can reach it.

Egress broker secrets (credential services named `egress_*`) are stored in primary-only credential stores and never reach worker-writable locations, matching the placement of primary-built tool settings.
When the broker is enabled, the worker-routed `shell` and `python` calls of agents with the `egress_credentials` tool receive a signed proxy token in their environment, and the broker injects the configured secret when such a request matches a service rule; other agents' calls get no token and no secret.
Worker code can read its own proxy token, but the token only authorizes that worker's scope and expires after `MINDROOM_EGRESS_BROKER_TOKEN_TTL_SECONDS` (default 7 days), so a leaked token exercises only that scope's configured services until it expires.
Broker refusals never carry a secret, token, query string, or connect link, but they name a shared agent's credential managers, so worker code can read those Matrix IDs.
Agents without a worker scope all share the global store, so a token from any of them reaches the same global keys.
A service can also take its secret from an OAuth account that a user connected (GitHub, Google, Atlassian) when no API key is stored for the scope, and an explicit key always wins over the account.
The primary fetches and refreshes those access tokens through the OAuth credential lifecycle and the broker injects them as the matched rule's auth type; workers never receive an OAuth access token or a refresh token, only the proxy token and placeholder values, and the broker's logs and audit records never contain them.
Account scope follows each provider's own policy: GitHub and Atlassian are requester-scoped, so a worker's requests use the requester named in its verified proxy token, while the Google providers follow the agent's worker scope.
The broker uses a requester-scoped account only on `user` and `user_agent` workers that run on a dedicated (Docker or Kubernetes) worker backend by default.
A `shared` or unscoped worker, and every worker on the static runner, runs every requester's commands in one sandbox, where a later command from another user can read an earlier caller's proxy token (for example from the environment of a process left running) and act with that caller's account until the token expires, so there the broker injects no GitHub or Atlassian account, adds no placeholder for it, and offers no connect link.
A service's `oauth_on_shared_workers: true` removes that gate: every user of such a shared sandbox can then act with any other user's connected account until that user's proxy token expires.
The `connect_url` that a 403 response carries goes only to the worker whose proxy token has that scope; it is the same short-lived single-use connect link that conversation OAuth uses, and it starts a connection for that one scope after the recheck of the requester's permission.
The link also requires the browser to be signed in to MindRoom as that requester.
A shared-scope link would skip that sign-in, and any process in a shared worker could read it, so the broker never puts one in a response; users of a shared agent connect its account from the egress pages instead.
A shared Google service account (`GOOGLE_SERVICE_ACCOUNT_FILE`) is never injected, so the broker cannot be used to spend it.
On the shared static runner, including the hosted `static_runner` sidecar, a scoped call's broker token, valid for the token TTL, lives in a process shared with other users' calls, consistent with the static runner not being an isolation boundary; use dedicated `user` or `user_agent` workers on `private` agents when requesters must not be able to use each other's brokered credentials (see [Shared workspaces on non-private agents](https://docs.mindroom.chat/deployment/egress-broker/#shared-workspaces-on-non-private-agents)), because a dedicated `shared` or unscoped worker also runs every requester's commands in one sandbox.
Only administrators and an agent's credential managers can change the keys, services, and account connection of a shared or unscoped agent.
The credential managers of one key scope, and of all unscoped agents, can change each other's keys and services (see [Egress Broker Design Decisions](https://docs.mindroom.chat/architecture/egress-broker-design/#secrets-and-identity)).
Users can define egress services of their own, which apply only to their own scope, route only that scope's keys and accounts, never widen the administrator's policy, and cannot take a config service's name or set `oauth_on_shared_workers`.
A user service can use a connected account only in a personal scope, and its placeholder variables are limited to credential-like names and plain values, because a shared agent's services set variables in every requester's sandbox.
`restrict_to_rules` is not a network boundary: under `passthrough` a worker can reach the same server by IP address through a tunnel the broker does not intercept, which carries no credential, so the real boundary stays the token's own permissions on the upstream side.
The risks the broker cannot prevent (code in the primary process, write access to the config, agents with the `egress_credentials` tool acting with their scope's full keys and accounts, hosts that echo requests, wildcard rules on shared hosting domains, keys that can create other keys, and shared workspaces on `user` and `user_agent` agents that are not `private`) are listed in [What the broker cannot protect against](https://docs.mindroom.chat/deployment/egress-broker/#what-the-broker-cannot-protect-against).

The egress broker listener (when `MINDROOM_EGRESS_BROKER_PORT` is set) is a worker-reachable primary runtime surface: workers must reach it to make brokered requests, so it must be accessible from the worker network (Docker `docker0` interface or the Kubernetes broker Service).
The hosted `static_runner` sidecar shares the primary pod's network instead, so there the broker binds only loopback (`127.0.0.1`) and is reachable from no other pod.
The broker port must not be published beyond the worker network, because a leaked token could otherwise be used from arbitrary network locations until it expires; expired tokens get 407.
Firewall rules and network policies confine the broker port to worker-originated connections only.

### Testing and review of the egress broker

The broker's transport around `ssl`, `h11`, and `cryptography`, and its path checks, are hand-written and covered by tests, including a request-smuggling corpus and fuzzing, but have had no independent adversarial review.
A human penetration test of the proxy core is recommended before the broker is exposed widely.

### Workspace mounts by worker scope

[Worker scopes](https://docs.mindroom.chat/deployment/sandbox-proxy/#worker-scopes) lists the workspaces each dedicated worker mounts.
A `user` worker mounts several agents' workspaces, so code working in one of them can read and write the others.

A shared directory lives at `shared_directories/<owner>/<name>/` and mounts read-write into its owner's dedicated workers and read-only into each member's.
No dedicated worker mounts `shared_directories/` or an owner directory below it, so worker code cannot replace a directory another worker mounts; the primary creates and empties these directories through no-follow descriptors, reads and writes files there only through the path tools' no-follow confinement and atomic writes, and indexes a knowledge base configured inside one without following links.
Primary-process `file` and `coding` reach shared directories only when no worker is configured, because worker code writes them and these tools list and search by path after checking it.
For the same reason the primary never runs Git for a knowledge base inside a shared directory.
A workspace or shared directory is mounted only when it is a real directory reached from the storage root without links.
Because Docker resolves mount targets again on every container start, a worker whose mount target inside its writable root was replaced with a link by worker code is refused when it is created or restarted.
Assigned knowledge outside the workspace reaches a worker only when its configured path is a real directory or file, reached without links, outside every directory other workers write, or inside a shared directory that worker already mounts.
Kubernetes mounts such knowledge read-only, because kubelet follows links inside the volume when it mounts, and Docker copies it into the worker's read-only config snapshot through no-follow descriptors.

### Workspace files the primary reads and writes

The primary treats everything inside a mounted workspace as worker-controlled.
Files it reads or writes there, such as skills, subagent profiles, context files, delegation records, knowledge sources, call transcripts, callback scripts, script-run snapshots, todo templates, scheduled-run receipts, workspace knowledge links, thread exports, the files `prompt_curation` measures, and the memory files, staging copies, reports, verdicts, and thread-export `index.json` files `dreaming` reads, are reached from the workspace root without following links, never written through a file that has another hard link, opened non-blocking so a FIFO cannot stall the primary, and published by atomic replacement or exclusive creation.
The primary never takes a file lock inside a workspace, because worker code could hold it forever.
`dreaming` writes each run's agenda, staging copy of `memory/`, and patch under `.mindroom/dreaming/runs/`, and deletes finished staging and old runs without following links.
It writes an approved proposal into `memory/` from bytes worker code staged, which gives the agent nothing it could not write itself; it writes only Markdown under `memory/` other than today's daily note and context files, and keeps its progress below `tracking/`, outside the workspace.
Delegation records keep their working state below `tracking/`, and the `run.json`, `events.jsonl`, and `transcript.md` in the child's workspace are exports the primary never reads.
Worker code that makes one of those exports or the caller's receipt unwritable only leaves the record's exports or the receipt stale; the delegation still records its events and settles.

In the primary, `mindroom_output_path`, attachment saves, Google Drive, Microsoft Files, Outlook mail attachment, and E2B downloads, `file_generation` saves, `visualization` charts, workspace knowledge links, workspace todo templates, and `file` and `coding` reads, writes, and deletes refuse a workspace replaced by a link after runtime resolution.
That check covers only the workspace's final path component, so a replaced parent such as `agents/<name>` is not refused.
With the default workspace names that is not exploitable in the hosted layout, because no primary-only directory there contains an entry named `workspace`; an authored `private.root` that matches a primary directory name, such as `credentials`, could be reached through a replaced parent.

A background job's saved result keeps the bytes a tool returned with a media file instead of reopening its path, and reads a media file the tool returned only by path as a regular file, opened non-blocking without following a final link.

Git commands the primary runs in a workspace, for knowledge checkouts and the `coding` tool's ignore check, use the hardened Git command and environment, so programs named in workspace Git config never run.

### Read limits on workspace files

Reads of worker-controlled files are capped per surface.

| Surface | Cap | Above the cap |
|---|---|---|
| Context files | 1 MiB | Truncated with a warning |
| `prompt_curation` snapshots of `MEMORY.md` and context files | 1 MiB per file | Refused, so the check is skipped with a warning or verify reports the file as unreadable |
| `dreaming` memory files, staging copies, reports, and verdicts | 1 MiB per file | A memory file is left out of the run, a staged file stops the run, and a report or verdict counts as missing, so the run stops or the proposal is rejected |
| `dreaming` thread-export `index.json` | 8 MiB per room | Ignored, so that room's conversations are aged by file modification time |
| Workspace `SKILL.md`, skill references and scripts | 1 MiB per file; names 64 characters; descriptions and the reason a skill did not load 1024 characters; 256 listed scripts and 256 references; per workspace 256 skills and 8 MiB of `SKILL.md` files and listings, including files that fail to load; 1,024 examined entries per `skills/`, `scripts/`, `references/`, or `skills/.history/<skill>/` directory, for skill loading and skill learning alike | An oversized file or a skill with a longer name is refused, a longer description, listing, or reason is truncated, and skills beyond the budget or count are skipped, each with a warning; the agent's system prompt names each skill refused for its frontmatter or name with the reason, which can quote that frontmatter; entries past the first 1,024 in directory order are not examined |
| Workspace subagent profiles (`subagents/*.md`) | 64 KiB per file; names 64 characters; descriptions 1024 characters; the listing reads at most 256 profiles and 1 MiB; 1,024 examined entries in `subagents/` | An oversized or unreadable file is listed as invalid and cannot run, and profiles past the listing limits are left out of it but still load by name |
| Call transcripts sent to Mem0 | 64 MiB | Truncated |
| Knowledge sources, including operator-managed ones | 64 MiB | Left out of the listing with a warning |
| Scheduled-run receipts, `file` and `coding` reads, `airflow` DAG reads | 64 MiB | Refused with a logged error |
| `e2b` uploads, sandbox files that `e2b` downloads or reads, and `e2b` command and code output | 64 MiB | Refused with a tool error; `stream_command()` returns its first 64 MiB |
| `microsoft_files` uploads | 250 MB, Microsoft Graph's single-request limit | Refused with a tool error before any request |
| `outlook_mail` attachments | 3 MB per file, Microsoft Graph's inline attachment limit | Refused with a tool error before any request |
| Files a `file` content search reads | 500 KiB each, Agno's search limit | Skipped |
| `browser` upload snapshots, kept in the browser's temp directory until their tab closes | 256 MiB in total per browser | Refused with a tool error |
| Media files a background job's result reads by path | 48 MiB in total per result, within its 64 MiB encoded limit | The job fails with a size-limit error |
| `moviepy_video_tools` staged inputs | 1 GiB per video; 1 MiB per caption file | The call fails before staging more than the cap |

Thread-export files are read and built under the per-file, per-thread, and per-room limits documented in [Thread Exports](https://docs.mindroom.chat/thread-exports/).
The exporter's drift check reads at most 8 MiB of a room's `index.json` and rebuilds a larger one from the thread files.
Workspace todo templates have the size, render, and listing limits documented in [`todo`](https://docs.mindroom.chat/tools/project-management/#todo), and they render in a short-lived, memory-limited child process instead of the primary, because sandboxed Jinja alone does not bound their cost.

Workspace `SKILL.md` and subagent profile frontmatter, todo templates, and thread-export files are refused before parsing when their YAML uses aliases, `%TAG` directives, deep nesting, or other structures that would let a small file cost the primary unbounded memory, stack depth, or parse time; [Skills](https://docs.mindroom.chat/skills/#skillmd-format-openclaw-compatible) lists the exact limits.

## Hosted tenant isolation

Hosted tenants share the `mindroom-instances` namespace, and a tenant can run code in its own primary and sandbox-runner sidecar by design.
The boundary between tenants is the cluster configuration around those pods.

[Multi-Tenant Architecture](https://docs.mindroom.chat/deployment/saas-platform/#multi-tenant-architecture) lists those controls.
The instance chart refuses dedicated Kubernetes workers because RBAC cannot confine a worker manager to one tenant's Deployments, Services, PVCs, and Secrets in a shared namespace.

## File access

`defaults.file_access` sets the default and `agents.<name>.file_access` overrides it for one agent.

| Value | Path tools may use |
|---|---|
| `workspace` (default) | Files inside the agent workspace and attachments available in the conversation; for `file` and `coding`, also the agent's shared directories, read-only where it only joined |
| `unrestricted` | Any file the tool's process can reach; on a worker that is the worker container |

Every tool, including plugin tools, must declare one of these file access classes when it registers, so no tool silently defaults to taking no paths.

| Tool class | Tools | Behavior |
|---|---|---|
| Path tools | `attachments` (including `view_file`), `matrix_message`, `chat_ui` canvas pages (`show_canvas` with `path`), `gmail`, `google_drive`, `microsoft_files`, `outlook_mail` attachments, `browser` uploads, `e2b` uploads, `openai` and `groq` audio files, `airflow`, `moviepy_video_tools` | Follow the agent's `file_access` and refuse replaced workspace roots and swapped files; `airflow` and `moviepy_video_tools` writes do not follow links below the workspace, and MoviePy and FFmpeg work only on private staged copies, refusing video inputs that FFmpeg would open as playlists or manifests |
| Worker path tools | `file`, `coding` | Follow the agent's `file_access`; reads, writes, edits, deletes, and every file a `file` content search reads refuse replaced workspace roots and swapped files, while listing and the other searches walk by path, as the known gap below describes |
| Unconfined tools | Code-execution tools (`shell`, `python`, `docker`, `script`, `claude_agent`) and tools whose queries, paths, or URLs reach local files without confinement (`duckdb`, `csv`, `pandas`, `sql`, `composio`, `postgres`, `redshift`, `browserbase`, `slack`) | Class `unconfined`: not confined by `file_access`, whatever the agent's setting; authored tool config may only state `file_access: unconfined` |
| Other tools | Everything else, including `web_browser_tools`, which opens only `http` and `https` URLs | Take no local file paths |

[`browser_mcp`](https://docs.mindroom.chat/tools/worker-computer/#native-playwright-mcp-provider) runs only in a worker and confines its upload, drop, screenshot, and PDF paths to the worker workspace whatever the agent's `file_access`.

MCP servers on the local `stdio` transport are unconfined too, because they are operator-launched programs; remote `sse` and `streamable-http` servers take no local file paths.
Whether a tool executes code is a separate metadata flag from its file access class; only the code-execution tools above carry it.
A worker isolates the unconfined tools that support worker routing (`shell`, `python`, `docker`, `csv`, `postgres`, `redshift`).
The rest require the primary runtime and cannot run in a worker (`claude_agent`, `script`, `duckdb`, `pandas`, `sql`, `composio`, `browserbase`, `slack`), so enable them only for agents trusted with everything the primary runtime can reach.
MindRoom logs a warning when an agent routes code-execution tools to a worker while primary-process tools stay unconfined, meaning unconfined tools or path tools under `file_access: unrestricted`, because those tools can then read runtime secrets the worker was meant to keep away.
The model sees the effective file access and the unconfined tools in its tool execution environment description.

## Connections portal sign-in

This section holds the mechanics behind [opening Connections from MindRoom Chat](https://docs.mindroom.chat/connections-portal/#open-it-from-mindroom-chat).
The portal asks the window that opened it for a Matrix OpenID token over `postMessage`, waiting up to 30 seconds.
It then posts the token and the origin of Chat's message to `POST /api/connections/session`.
MindRoom checks that origin against `MINDROOM_CONNECTIONS_ALLOWED_ORIGINS`, which defaults to MindRoom's hosted Chat plus, for a portal on a local address, any Chat on a local address; under that default, MindRoom also reads `/_matrix/client/versions` and refuses sign-in unless the homeserver binds tokens.
It verifies the token at the configured homeserver through `/_matrix/federation/v1/openid/userinfo` and accepts only users of that homeserver.
MindRoom also sends the normalized origin of `MINDROOM_PUBLIC_URL`, or `http://localhost:<MINDROOM_PORT>` when it is unset, as the audience, so a homeserver that binds tokens accepts only a token that Chat requested for that origin; other homeservers ignore it.
MindRoom then sets the `__Host-mindroom_connections_session` cookie.
The cookie is `Secure`, `HttpOnly`, `SameSite=Lax`, and `Path=/`.
Browsers refuse `Secure` cookies over plain HTTP, so a portal on a local address served over HTTP names it `mindroom_connections_session` without `Secure`; an HTTPS portal ignores that name, so a sibling host cannot plant a session for it.
The cookie is browser-wide, so a second sign-in in another tab replaces it.
To keep a stale tab from acting as the new account, the Connections pages send the Matrix ID they show in the `X-MindRoom-Connections-User` header on every `/api/connections` request.
MindRoom answers 401 when that header names a different account than the request authenticates as, whether by a session or by trusted upstream.
Requests without the header, such as OAuth popup navigations, still work.
At sign-in, MindRoom also records in the signed cookie whether the session may open the dashboard.
It may only when the user is in `administrators` and the homeserver's `/_matrix/client/versions` lists `io.mindroom.openid_audience` as `true`, which MindRoom's Tuwunel fork does while it binds tokens.
Under binding, a token that verified with this deployment's audience was requested for this deployment, so another deployment's operator cannot replay it here; without binding they could, and the dashboard is full control.
The exception is deployments that all leave `MINDROOM_PUBLIC_URL` unset and so share the default `http://localhost:<MINDROOM_PORT>` audience: a backend a user opens at that origin can replay the token to any such deployment it reaches.
MindRoom verifies the token a second time after reading `/versions`, because `/versions` shows only the setting after the first verification: if binding came on in between, the second verification refuses the unbound token, and if it went off, the first one already proved the token bound to this deployment.
The record is fixed at sign-in, because a session started before binding was never proven bound, while `administrators` is read again on every dashboard request.
Such a session authenticates dashboard routes as the administrator's Matrix ID, except in hosted platform mode and on OAuth callbacks, which keep finishing each flow as the identity that started it.

## Known gaps

These are tracked gaps, not intentional behaviors; fix them rather than documenting around them.

- The unconfined non-code tools `composio`, `postgres`, `redshift`, `browserbase`, and `slack` do not yet follow `file_access`; a separate change will confine their explicit path and URL arguments.
- SQL-capable tools (`duckdb`, `csv`, `sql`, and `pandas` query helpers) embed file paths inside queries, so guarding explicit path arguments cannot confine them; they stay unconfined until a query-level mechanism exists.
- The listing and search functions of `file` and `coding` (`list_files`, `search_files`, `grep`, `find_files`, and `ls`) refuse a workspace that no longer resolves to the pinned directory and paths that lead outside it, but then walk and read by path.
  This matters only when an operator routes these tools to the primary process while worker code writes the same workspace, such as the Kubernetes `static_runner` sidecar with `worker_tools` that leave out `file` and `coding`.
  Code that swaps the workspace, a checked directory, or a file for a link between that check and the walk can make that one call list or return any file the primary process can read, and a planted FIFO can stall a `coding` content search.
  By default these tools run in a worker, where they see only what the worker already mounts.
- Knowledge Git commands refuse a worktree whose path goes through a link, but Git then reopens that worktree by path.
  For a Git-backed knowledge base inside a workspace a worker writes, swapping the knowledge folder for a link between that check and the Git command can make that one sync check out or update files at the link target with the primary's permissions; the next sync refuses the link.

## Intentional behaviors

Do not report or "fix" these; they are deliberate.

- An operator who runs MindRoom without workers, for example inside a dedicated LXC container or VM, has chosen full trust; `file_access: unrestricted` matches that choice.
- `file_access: unrestricted`, or unconfined tools in the primary process, are allowed together with worker routing; they log a warning instead of failing.
- Hosted tenants may set `file_access: unrestricted`; it exposes only their own instance.
- With `file_access: workspace`, the browser, attachments, `matrix_message`, `chat_ui` canvases, Gmail, Google Drive, Microsoft Files, and Outlook mail may still use every file in the agent workspace.
- A `chat_ui` canvas can leak what the user types into it through WebRTC, which browsers do not let a page block, or, with libraries on, through the addresses it requests, so agents should only show pages they wrote; see [Privacy and safety](https://docs.mindroom.chat/canvases/#privacy-and-safety).
- The browser and `matrix_message` also accept `att_*` IDs of attachments available in the conversation; Gmail, Google Drive, Microsoft Files, and Outlook mail take file paths only, so an agent first saves a received attachment into the workspace with `get_attachment(mindroom_output_path=...)` and then passes that workspace path.
- `mindroom_output_path`, attachment saves, Google Drive and Microsoft Files downloads, Outlook mail attachment downloads, `file_generation` saves, `visualization` charts, and report publishing always write inside the workspace regardless of `file_access`, because they produce MindRoom-owned output.
- Writes into `.git` directories stay blocked for `file` and `coding` in both modes, because MindRoom runs Git in checkouts that may sit inside agent workspaces.
- A skill review may resend the reviewed conversation unredacted to the provider that just received it, and a digest replay redacts credentials only on a best-effort basis; see [What a review sees](https://docs.mindroom.chat/skills/#what-a-review-sees).
  The check that refuses skill files holding a literal credential is a heuristic for common formats; a miss is not a vulnerability, because skills live in the agent's own workspace next to the conversation history that already held the value.
- Automatic skill learning trusts the `metadata.mindroom` frontmatter flags and the provenance and usage in `skills/.usage.json` inside the workspace, which worker code can change; that only lets it make the learner edit or archive skills worker code could already change itself.
- `skill_manage`, used in chat by agents that list it or learn skills and by skill reviews, runs in the primary process and writes only below the agent's workspace `skills/` directory through no-follow descriptors, regardless of `file_access`, like MindRoom-owned output.
- An authored subagent, started by `run_subagent` with `system_prompt`, `tools`, or `profile` or by a Dynamic Workflow `subagent` participant, runs as its caller with the caller's tools, credentials, workspace, and approval rules, which for a workflow participant include the caller's `dynamic_workflow` `allowed_tools` pre-approval and reading the skills it names when the caller could without approval; worker code that edits a `subagents/*.md` profile can change what such a child is told, but gives it nothing the caller lacks.
- Each agent publishes its running background jobs in each room as unencrypted room state keyed by its own user ID: job ID, thread, kind, tool name, status, requester ID, and start time, never arguments or results, so the homeserver and every room member can read them even in an encrypted room.
- Anyone who may talk to an agent in a room may cancel its background jobs there, including jobs another requester started, as with **Stop**; another agent's account never may.
- An agent's 🛑 reaction, its Stop button included, stops nothing unless it comes from [`stop_reply`](https://docs.mindroom.chat/tools/matrix-and-attachments/#stop_reply), which counts as the Stop of the user the agent acts for and needs that user's access to the stopped agent.
- The agent or team that answers in a conversation lists, reads, and cancels the background jobs its team members started there for the same requester, because the team already receives its members' output.
- The `job` function never asks for approval, even under `tool_approval.default: require_approval`, because it only reads and cancels the caller's own jobs, each of which passed its own approval.
- `config_manager`'s `manage_config(operation="inspect")` and `manage_agent(operation="validate")` calls never ask for approval, even under a matching rule or `tool_approval.default: require_approval`, because they only read configuration.
- Inviting an agent to a shared directory makes it readable to every requester of that agent, and the invitation needs only the inviting user's access to that agent, because that user could already relay the files in chat.
- A user who may use both agents can restart the invited agent's dedicated workers by inviting and removing it repeatedly, which ends their background shells.
- Removing a member or deleting a shared directory takes effect for a member's dedicated worker on that worker's next tool call, so until then its running processes can still read the directory, including files written after the removal.
- A team's `access` authorizes its exact member agents for team requests, in Matrix and the OpenAI-compatible API, even members whose own `access` would not admit the requester directly; adding an agent to a team is a deliberate grant.
- A response's CLI grant, in minimal mode or for a standard-mode agent whose shell can reach MindRoom, is in the environment of its shell commands, so other code in the same worker can use that requester's tools through it until the response and its background jobs end; code that shares a worker already shares its trust.
- A shared agent's knowledge graph holds facts from every requester's conversations and tool results, and any requester of that agent can query it, like shared-agent memory; private agents get one graph per requester.
- Redacting a message does not remove facts the knowledge graph already extracted from it; ingestion skips turns derived from messages redacted before it reached them, so raising `knowledge_graph.generation` rebuilds the graphs without them; replies and voice calls already running finish on the old graphs, which stay stored until `knowledge_graph/` is deleted with MindRoom stopped; an `exploration` prompt can still list a redacted turn's tool calls until the agent next replies in that conversation.
- An `exploration` run is an ordinary turn started by an automation prompt, with the agent's own tools and approval rules, so content it reads can steer it as it could any turn that reads untrusted content; approval rules, not the prompt's instruction to only read, stop writes.
- A shared agent's exploration follows tool calls from every requester's conversations with that agent's own credentials, so one requester can lead it to read something their own access cannot and then query the result in the shared graph; use a private agent when requesters must stay apart.
- Knowledge graph extraction reads tool results that worker code produced, so a crafted result can add wrong facts to the graph, each attributed to its source; it runs nothing in the primary.
- A non-private agent's workspace is shared by every requester's runtime for that agent, including `user` and `user_agent` workers, so files one requester leaves there (such as `.mindroom/worker-env.sh`, `.pth` files, or Git config) can run in another requester's runtime; use `private` agents when requesters need isolation from each other.
- Likewise, a non-private agent's primary-process `browser` profiles and default artifacts live in that agent's state root and are shared by all of its requesters, so one requester can use web sessions another signed in and upload screenshots another took; they never cross agents or the requesters of a private agent.
- A browser egress proxy in a sandbox runner receives destination hostnames, because an approved-egress proxy decides by name, so rebinding between the relay's check and that proxy's own lookup is the proxy's responsibility; in the primary the relay tunnels to the address it validated.
- A proxy environment the primary cannot follow, such as a SOCKS proxy or `auto_proxy`, makes the relay dial directly within the browser's destination policy instead of refusing to start, because no MindRoom egress policy depends on that proxy there; a sandbox runner refuses.
- A shared knowledge base whose path lies inside an agent workspace, such as that agent's thread exports, or inside a shared directory is bound without following links below that directory, so even an operator-made link there is refused; shared knowledge outside every agent workspace and shared directory still follows operator links.
- `egress_broker.private_networks` lets the broker, which runs in the primary, dial the listed private ranges for workers, including primary-host addresses in them (such as the `docker0` gateway) and destinations a Kubernetes worker's NetworkPolicy blocks; loopback and metadata addresses stay refused, and [Private networks](https://docs.mindroom.chat/deployment/egress-broker/#private-networks) describes the exposure.
- The egress broker streams request bodies, so a valid request head can reach the right upstream before a bad or oversized body gets 400 or 413 and the upstream connection is aborted; the head carries the secret only to the host the request was routed to, and [Egress Broker Design Decisions](https://docs.mindroom.chat/architecture/egress-broker-design/#where-a-secret-may-go) gives the reason.
- The egress broker's check for stray bytes on a reused tunnel upstream cannot see bytes that reach the socket just after the check polls it.
  Such bytes can only come from that tunnel's own upstream, and no other tunnel, host, or requester is affected.
- A team approval continuation checks only that each approved call keeps the arguments its card showed, not whether the saved team run holds other calls ready to run, because team runs live below `teams/`, which no worker mounts.
- The Kubernetes `static_runner` sidecar mounts `agents`, `private_instances`, and `shared_directories` read-write as the primary's user, so tool code it runs can read and change every agent's sessions, memory, learning data, shared directories, and private-instance contents; the shared sidecar is not an isolation boundary between agents or requesters, and dedicated Kubernetes workers are the option that is.
- After an upgrade from workers that mounted whole state roots, startup stops those workers before serving, failing until none remain, and warns; it does not scan for or repair links they may have planted above workspaces, because a scan on every start is costly and an automatic repair could itself follow a planted entry, so an operator runs the [upgrade check](https://docs.mindroom.chat/deployment/upgrades/#workspace-only-worker-mounts).
- Conversation OAuth connect and reset links for shared-scope credentials work without a dashboard login; the short-lived single-use link and the recheck of the issuing requester's credential-management permission authorize them, because some deployments give users no dashboard access.
- Anyone who can use an agent may connect or reset their own requester-owned OAuth connection (user or user-agent credential scope) through its chat links, as on the Connections portal; the browser must authenticate as the link's requester, and shared-scope connections still require an administrator or credential manager.
- The Connections portal signs users in either through trusted upstream strict JWT auth or through a Matrix OpenID token that MindRoom Chat hands to the portal window, from the origins `MINDROOM_CONNECTIONS_ALLOWED_ORIGINS` lists or, unset, from MindRoom's hosted Chat and, for a portal on a local address, any Chat on a local address.
  Under the default list, MindRoom accepts that sign-in only while the homeserver binds tokens, because otherwise any backend a user opens from Chat could replay their token to it, and request headers cannot show that a portal is out of such a backend's reach; an explicit list is the operator's choice.
  The session cookie names the verified Matrix user and a fixed one-hour lifetime, signed with a key each MindRoom process generates at start, and it authenticates the portal and its APIs, the OAuth callback for flows it started, and the OAuth success page.
  Outside hosted platform mode, it also opens the dashboard, but only for a user in `administrators` whose token the homeserver bound to this deployment at sign-in; everyone else, and every session on a homeserver that does not bind tokens, needs the API key for the dashboard.
  Binding is proven by the homeserver's `/_matrix/client/versions` and a second verification of the token after reading it, so turning binding on or off once during a sign-in cannot promote an unbound token; turning it on and off again within that one sign-in is not covered.
  An allowlisted origin can sign a browser in as any Matrix user whose OpenID token it holds, so list only Chat origins you operate or trust; with the default list, a page served from any local address can do so for a portal on a local address.
  Over plain HTTP on a local address, the session cookie and the OAuth flow cross that network unencrypted, as the rest of a plain-HTTP stack does, and because cookies ignore ports, every other web service on that host receives the session cookie too.
  On a homeserver that does not bind tokens to one deployment, a backend that Chat opens could replay a user's token to another deployment on the same homeserver until it expires; [audience binding](https://docs.mindroom.chat/connections-portal/#token-binding) closes that where the homeserver supports it.
  The `/connections` page shell is served without authentication, while every API stays protected.
- The MCP gateway runs an agent's tools without its instructions, so it withholds agents whose worker scope is `shared` or unset unless `mcp_gateway_shared_credentials` opts one in; an opted-in agent's users can call its tools directly with its shared accounts and worker, and the shared workspace and browser profiles of a non-private `user` or `user_agent` agent stay reachable through the gateway as they are in chat.

## Reviewing security findings

Classify every finding before proposing a change.

1. Name the boundary it crosses: worker to primary, tenant to tenant, Matrix user to agent, or none.
2. Check whether the agent involved is trusted under the trust model above.
3. Check the intentional behaviors list.
4. Only a finding that crosses a real boundary and matches no intentional behavior is a bug.

A fix that restricts a trusted setup, or that blocks workspace and attachment flows an agent legitimately needs, is not a security fix.
When a maintainer rejects a finding as intended behavior, add it to the list above in the same change so the next reviewer does not rediscover it.
