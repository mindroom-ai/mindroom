---
icon: lucide/shield-half
---

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

## Protecting the primary from worker code

Hardening that protects the primary runtime and other tenants from untrusted worker code is always in scope.
Examples are symlinks or files planted in shared workspaces that the primary later follows, worker-writable metadata the primary trusts, Git config the primary executes, and secrets mounted or passed into workers.

### Config and state kept out of workers

Runners and dedicated workers never receive the primary's config file, its directory, its `.env`, a ConfigMap holding it, or the credential encryption key.
The config sent to runners with each request, and the config file projected into each Docker worker, hold only the fields runners resolve, such as agent execution scopes, file access, workspace and knowledge paths, tool names, and plugin paths.
Models, MCP servers, plugin settings, and every other section stay in the primary, and a worker-routed call carries only the called tool's own inline overrides.

Dedicated Docker and Kubernetes workers mount only agent workspaces, never the agent state roots around them, so sessions, memory, learning, Mem0 data, and private-instance identity records stay out of every worker.
The Kubernetes `static_runner` sidecar still mounts `agents` and `private_instances` read-write, so records the primary treats as authority live below the primary-only `tracking/` directory instead.
These are the invited-room and pending-invite ledgers, personal-room records, conversation modes, and the primary's copy of each private-instance owner record, together with their locks.
The thread exporter writes into a private instance only for the requester that the primary's owner record names, so an owner record planted in `private_instances` gets no export.

### Workspace mounts by worker scope

[Worker scopes](../deployment/sandbox-proxy.md#worker-scopes) lists the workspaces each dedicated worker mounts.
A `user` worker mounts several agents' workspaces, so code working in one of them can read and write the others.

A workspace is mounted only when it is a real directory reached from the storage root without links.
Because Docker resolves mount targets again on every container start, the backend walks the targets inside the worker's writable root without following links before creating or restarting a worker, and refuses one that worker code replaced with a link.
Assigned knowledge outside the workspace reaches a worker only when its configured path is a real directory or file, reached without links, outside every directory other workers write.
Kubernetes mounts such knowledge read-only, because kubelet follows links inside the volume when it mounts, and Docker copies it into the worker's read-only config snapshot through no-follow descriptors.

### Workspace files the primary reads and writes

The primary treats everything inside a mounted workspace as worker-controlled.
Files it reads or writes there, such as skills, context files, delegation records, knowledge sources, call transcripts, callback scripts, script-run snapshots, todo templates, scheduled-run receipts, workspace knowledge links, and thread exports, are reached through `path_confinement` descriptors walked from the workspace root.
Those descriptors refuse links, never write to a file that has another hard link, open files non-blocking so a FIFO cannot stall the primary, and publish files by atomic replacement or exclusive creation.
The primary never takes a file lock inside a workspace, because worker code could hold it forever.
Delegation records keep their working state below `tracking/`, and the `run.json`, `events.jsonl`, and `transcript.md` in the child's workspace are exports the primary never reads.
Worker code that makes one of those exports or the caller's receipt unwritable only leaves the record's exports or the receipt stale; the delegation still records its events and settles.

In the primary, `mindroom_output_path`, attachment saves, Google Drive and E2B downloads, `file_generation` saves, `visualization` charts, workspace knowledge links, workspace todo templates, and `file` and `coding` reads, writes, and deletes open the authorized workspace as spelled rather than its resolved target, so they refuse a workspace replaced by a link after runtime resolution.
`file` and `coding` also pin their workspace when they are built, refusing one that is a link or that changed while it was resolved, and their listing and search refuse a pinned workspace that no longer resolves to itself.
No-follow applies to the workspace's final path component only, so a replaced parent such as `agents/<name>` is not refused.
With the default workspace names that is not exploitable in the hosted layout, because no primary-only directory there contains an entry named `workspace`; an authored `private.root` that matches a primary directory name, such as `credentials`, could be reached through a replaced parent.

Git commands the primary runs in a workspace, for knowledge checkouts and the `coding` tool's ignore check, use the hardened Git command and environment, so programs named in workspace Git config never run.

### Read limits on workspace files

Reads of worker-controlled files are capped per surface.

| Surface | Cap | Above the cap |
|---|---|---|
| Context files | 1 MiB | Truncated with a warning; context preload truncation shortens them further |
| Workspace `SKILL.md`, skill references and scripts | 1 MiB per file; names 64 characters; descriptions 1024 characters; 256 listed scripts and 256 references; per workspace 256 skills and 8 MiB of `SKILL.md` files and listings, including files that fail to load; 1,024 examined entries per `skills/`, `scripts/`, `references/`, or `skills/.history/<skill>/` directory, for skill loading and skill learning alike | An oversized file or a skill with a longer name is refused, a longer description or listing is truncated, and skills beyond the budget or count are skipped, each with a warning; entries past the first 1,024 in directory order are not examined |
| Call transcripts sent to Mem0 | 64 MiB | Truncated |
| Knowledge sources, including operator-managed ones | 64 MiB | Left out of the listing with a warning |
| Scheduled-run receipts, `file` and `coding` reads, `airflow` DAG reads | 64 MiB | Refused with a logged error |
| `e2b` uploads, sandbox files that `e2b` downloads or reads, and `e2b` command and code output | 64 MiB | Refused with a tool error; `stream_command()` returns its first 64 MiB |
| Files a `file` content search reads | 500 KiB each, Agno's search limit | Skipped |
| `browser` upload snapshots, kept in the browser's temp directory until their tab closes | 256 MiB in total per browser | Refused with a tool error |
| `moviepy_video_tools` staged inputs | 1 GiB per video; 1 MiB per caption file | The call fails before staging more than the cap |

Thread-export files are read and built under the per-file, per-thread, and per-room limits documented in [Thread Exports](../thread-exports.md).
Workspace todo templates have the size, render, and listing limits documented in [`todo`](../tools/project-management.md#todo), and they render in a short-lived, memory-limited child process instead of the primary, because sandboxed Jinja alone does not bound their cost.

Workspace `SKILL.md` frontmatter, todo templates, and thread-export files are refused before parsing when their YAML uses aliases, `%TAG` directives, deep nesting, or other structures that would let a small file cost the primary unbounded memory, stack depth, or parse time; [Skills](../skills.md#skillmd-format-openclaw-compatible) lists the exact limits.

## Hosted tenant isolation

Hosted tenants share the `mindroom-instances` namespace, and a tenant can run code in its own primary and sandbox-runner sidecar by design.
The boundary between tenants is the cluster configuration around those pods.

[Multi-Tenant Architecture](../deployment/saas-platform.md#multi-tenant-architecture) lists those controls.
The instance chart refuses dedicated Kubernetes workers because RBAC cannot confine a worker manager to one tenant's Deployments, Services, PVCs, and Secrets in a shared namespace.

## File access

`defaults.file_access` sets the default and `agents.<name>.file_access` overrides it for one agent.

| Value | Path tools may use |
|---|---|
| `workspace` (default) | Files inside the agent workspace and attachments available in the conversation |
| `unrestricted` | Any file the tool's process can reach; on a worker that is the worker container |

Every tool, including plugin tools, must declare one of these file access classes when it registers, so no tool silently defaults to taking no paths.

| Tool class | Tools | Behavior |
|---|---|---|
| Path tools | `attachments` (including `view_file`), `matrix_message`, `chat_ui` canvas pages (`show_canvas` with `path`), `gmail`, `google_drive`, `browser` uploads, `e2b` uploads, `openai` and `groq` audio files, `airflow`, `moviepy_video_tools` | Follow the agent's `file_access` and read through no-follow descriptors, so replaced workspace roots and swapped files are refused; `airflow` and `moviepy_video_tools` also write by atomic replacement without following links below the workspace, and MoviePy and FFmpeg work only on private staged copies, refusing video inputs that FFmpeg would open as playlists or manifests |
| Worker path tools | `file`, `coding` | Follow the agent's `file_access`; reads, writes, chunk edits, deletes, and every file a `file` content search reads go through no-follow descriptors, and writes replace the file atomically; listing and the other searches walk by path, as the known gap below describes |
| Unconfined tools | Code-execution tools (`shell`, `python`, `docker`, `script`, `claude_agent`) and tools whose queries, paths, or URLs reach local files without confinement (`duckdb`, `csv`, `pandas`, `sql`, `composio`, `postgres`, `redshift`, `browserbase`, `slack`) | Class `unconfined`: not confined by `file_access`, whatever the agent's setting; authored tool config may only state `file_access: unconfined` |
| Other tools | Everything else, including `web_browser_tools`, which opens only `http` and `https` URLs | Take no local file paths |

[`browser_mcp`](../tools/worker-computer.md#native-playwright-mcp-provider) runs only in a worker and confines its upload, drop, screenshot, and PDF paths to the worker workspace whatever the agent's `file_access`.

MCP servers on the local `stdio` transport are unconfined too, because they are operator-launched programs; remote `sse` and `streamable-http` servers take no local file paths.
Whether a tool executes code is a separate metadata flag from its file access class; only the code-execution tools above carry it.
A worker isolates the unconfined tools that support worker routing (`shell`, `python`, `docker`, `csv`, `postgres`, `redshift`).
The rest require the primary runtime and cannot run in a worker (`claude_agent`, `script`, `duckdb`, `pandas`, `sql`, `composio`, `browserbase`, `slack`), so enable them only for agents trusted with everything the primary runtime can reach.
MindRoom logs a warning when an agent routes code-execution tools to a worker while primary-process tools stay unconfined, meaning unconfined tools or path tools under `file_access: unrestricted`, because those tools can then read runtime secrets the worker was meant to keep away.
The model sees the effective file access and the unconfined tools in its tool execution environment description.

## Known gaps

These are tracked gaps, not intentional behaviors; fix them rather than documenting around them.

- The unconfined non-code tools `composio`, `postgres`, `redshift`, `browserbase`, and `slack` do not yet follow `file_access`; a separate change will confine their explicit path and URL arguments.
- SQL-capable tools (`duckdb`, `csv`, `sql`, and `pandas` query helpers) embed file paths inside queries, so guarding explicit path arguments cannot confine them; they stay unconfined until a query-level mechanism exists.
- The listing and search functions of `file` and `coding` (`list_files`, `search_files`, `grep`, `find_files`, and `ls`) refuse a workspace that no longer resolves to the pinned directory and paths that lead outside it, but then walk and read by path.
  This matters only when an operator routes these tools to the primary process while worker code writes the same workspace, such as the Kubernetes `static_runner` sidecar with `worker_tools` that leave out `file` and `coding`.
  Code that swaps the workspace, a checked directory, or a file for a link between that check and the walk can make that one call list or return any file the primary process can read, and a planted FIFO can stall a `coding` content search.
  By default these tools run in a worker, where they see only what the worker already mounts.
  The `file` tool's `search_content` reads each file it finds through no-follow descriptors, so under `file_access: workspace` it returns only workspace content.
- Knowledge Git commands refuse a worktree whose path goes through a link, but Git then reopens that worktree by path.
  For a Git-backed knowledge base inside a workspace a worker writes, swapping the knowledge folder for a link between that check and the Git command can make that one sync check out or update files at the link target with the primary's permissions; the next sync refuses the link.

## Intentional behaviors

Do not report or "fix" these; they are deliberate.

- An operator who runs MindRoom without workers, for example inside a dedicated LXC container or VM, has chosen full trust; `file_access: unrestricted` matches that choice.
- `file_access: unrestricted`, or unconfined tools in the primary process, are allowed together with worker routing; they log a warning instead of failing.
- Hosted tenants may set `file_access: unrestricted`; it exposes only their own instance.
- With `file_access: workspace`, the browser, attachments, `matrix_message`, `chat_ui` canvases, Gmail, and Google Drive may still use every file in the agent workspace.
- A `chat_ui` canvas can leak what the user types into it through WebRTC, which browsers do not let a page block, or, with libraries on, through the addresses it requests, so agents should only show pages they wrote; see [Privacy and safety](../canvases.md#privacy-and-safety).
- The browser and `matrix_message` also accept `att_*` IDs of attachments available in the conversation; Gmail and Google Drive take file paths only, so an agent first saves a received attachment into the workspace with `get_attachment(mindroom_output_path=...)` and then passes that workspace path.
- `mindroom_output_path`, attachment saves, Google Drive downloads, `file_generation` saves, `visualization` charts, and report publishing always write inside the workspace regardless of `file_access`, because they produce MindRoom-owned output.
- Writes into `.git` directories stay blocked for `file` and `coding` in both modes, because MindRoom runs Git in checkouts that may sit inside agent workspaces.
- A skill review may resend the reviewed conversation unredacted to the provider that just received it, and a digest replay redacts credentials only on a best-effort basis; see [What a review sees](../skills.md#what-a-review-sees).
  The check that refuses skill files holding a literal credential is a heuristic for common formats; a miss is not a vulnerability, because skills live in the agent's own workspace next to the conversation history that already held the value.
- Automatic skill learning trusts the `metadata.mindroom` frontmatter flags and the provenance and usage in `skills/.usage.json` inside the workspace, which worker code can change; that only lets it make the learner edit or archive skills worker code could already change itself.
- `skill_manage`, used in chat by agents that list it or learn skills and by skill reviews, runs in the primary process and writes only below the agent's workspace `skills/` directory through no-follow descriptors, regardless of `file_access`, like MindRoom-owned output.
- A team's `access` authorizes its exact member agents for team requests, in Matrix and the OpenAI-compatible API, even members whose own `access` would not admit the requester directly; adding an agent to a team is a deliberate grant.
- A response's CLI grant, in minimal mode or for a standard-mode agent whose shell can reach MindRoom, is in the environment of its shell commands, so other code in the same worker can use that requester's tools through it until the response ends; code that shares a worker already shares its trust.
- A non-private agent's workspace is shared by every requester's runtime for that agent, including `user` and `user_agent` workers, so files one requester leaves there (such as `.mindroom/worker-env.sh`, `.pth` files, or Git config) can run in another requester's runtime; use `private` agents when requesters need isolation from each other.
- Likewise, a non-private agent's primary-process `browser` profiles and default artifacts live in that agent's state root and are shared by all of its requesters, so one requester can use web sessions another signed in and upload screenshots another took; they never cross agents or the requesters of a private agent.
- A browser egress proxy in a sandbox runner receives destination hostnames, because an approved-egress proxy decides by name, so rebinding between the relay's check and that proxy's own lookup is the proxy's responsibility; in the primary the relay tunnels to the address it validated.
- A proxy environment the primary cannot follow, such as a SOCKS proxy or `auto_proxy`, makes the relay dial directly within the browser's destination policy instead of refusing to start, because no MindRoom egress policy depends on that proxy there; a sandbox runner refuses.
- A shared knowledge base whose path lies inside an agent workspace, such as that agent's thread exports, is bound without following links below the workspace, so even an operator-made link there is refused; shared knowledge outside every agent workspace still follows operator links.
- A team approval continuation checks only that each approved call keeps the arguments its card showed, not whether the saved team run holds other calls ready to run, because team runs live below `teams/`, which no worker mounts.
- The Kubernetes `static_runner` sidecar mounts `agents` and `private_instances` read-write as the primary's user, so tool code it runs can read and change every agent's sessions, memory, learning data, and private-instance contents; the shared sidecar is not an isolation boundary between agents or requesters, and dedicated Kubernetes workers are the option that is.
- After an upgrade from workers that mounted whole state roots, startup stops those workers before serving, failing until none remain, and warns; it does not scan for or repair links they may have planted above workspaces, because a scan on every start is costly and an automatic repair could itself follow a planted entry, so an operator runs the [upgrade check](../deployment/upgrades.md#workspace-only-worker-mounts).
- Conversation OAuth connect and reset links for shared-scope credentials work without a dashboard login; the short-lived single-use link and the recheck of the issuing requester's credential-management permission authorize them, because some deployments give users no dashboard access.
- Anyone who can use an agent may connect or reset their own requester-owned OAuth connection (user or user-agent credential scope) through its chat links, as on the Connections portal; the browser must authenticate as the link's requester, and shared-scope connections still require an administrator or credential manager.

## Reviewing security findings

Classify every finding before proposing a change.

1. Name the boundary it crosses: worker to primary, tenant to tenant, Matrix user to agent, or none.
2. Check whether the agent involved is trusted under the trust model above.
3. Check the intentional behaviors list.
4. Only a finding that crosses a real boundary and matches no intentional behavior is a bug.

A fix that restricts a trusted setup, or that blocks workspace and attachment flows an agent legitimately needs, is not a security fix.
When a maintainer rejects a finding as intended behavior, add it to the list above in the same change so the next reviewer does not rediscover it.
