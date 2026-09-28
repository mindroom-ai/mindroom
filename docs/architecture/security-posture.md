# Security Posture

This page records MindRoom's security model and the behaviors that are intentional.
Reviewers and agents must check a suspected vulnerability against this page before reporting or fixing it.
If a finding contradicts an intentional behavior below, do not change the code.
Raise the posture itself with the maintainers instead, and explain which scenario the current posture fails to protect.

## Trust model

A worker container, reached through the sandbox proxy, is the only security boundary between an agent and the MindRoom runtime.
Everything else in this page follows from that rule.

Tools that run arbitrary programs cannot be confined inside the process that runs them.
A shell or Python tool can start any program, and that program ignores any path or command check MindRoom applies in-process.
MindRoom therefore never filters the paths, commands, or imports of code-execution tools as a security measure.
They are isolated only by routing them to a worker with `worker_tools`.

An agent whose code-execution tools run in the primary process is trusted with everything that process can reach.
Restricting that agent's other tools protects nothing, because its shell can already read, write, and upload the same files.

An agent whose code-execution tools run in a worker must not reach more through its primary-process tools than its worker can.
Several tools run in the primary process by default even when code tools use workers, for example `browser`, `attachments`, `matrix_message`, `gmail`, and `google_drive`.
Those tools follow the agent's `file_access` setting, so the default keeps them inside the agent workspace.

Hardening that protects the primary runtime and other tenants from untrusted worker code is always in scope.
Examples are symlinks or files planted in shared workspaces that the primary later follows, worker-writable metadata the primary trusts, Git config the primary executes, and secrets mounted or passed into workers.

Dedicated Docker and Kubernetes workers mount only agent workspaces, never the agent state roots around them, so sessions, memory, learning, Mem0 data, and private-instance identity records stay out of every worker.
Which workspaces a worker mounts follows its scope.
Only private workspaces separate requesters: a non-private agent's workspace, `agents/<agent>/workspace`, is the same directory in every requester's worker that mounts it.

- A `shared` or unscoped worker mounts only its agent's workspace.
- A `user_agent` worker runs per requester and agent and mounts one agent's workspace: for a non-private agent that shared directory, and for a private agent only that requester's private workspace.
- A `user` worker runs per requester, not per agent: it mounts the workspace of every non-private agent whose worker scope is `user` and that requester's private workspace of every `private.per: user` agent, so code working in one of them can read and write the others.
A workspace is mounted only when it is a real directory reached from the storage root without links.
Docker resolves mount targets again on every container start, so before creating or restarting a worker the backend walks the targets inside the worker's own writable root without following links and refuses one that worker code replaced with a link.
Assigned knowledge outside the workspace reaches a worker only when its configured path is a real directory or file, reached without links, outside every directory other workers write: Kubernetes mounts it read-only, because kubelet follows links inside the volume when it mounts, and Docker copies it into the worker's read-only config snapshot through no-follow descriptors.
The primary treats everything inside a mounted workspace as worker-controlled.
Files it reads or writes there, such as skills, context files, delegation records, knowledge sources, call transcripts, callback scripts, script-run snapshots, todo templates, scheduled-run receipts, workspace knowledge links, and thread exports, are reached through `path_confinement` descriptors walked from the workspace root, which refuse links, open files non-blocking so a FIFO cannot stall the primary, and publish files by atomic replacement or create them exclusively; the few files appended to in place, call transcripts and delegation event logs, are refused when another hard link shares their inode, so a hard link an older worker planted never redirects a write.
The primary never takes a file lock inside a workspace, because worker code could hold it forever; writers of one delegation record exclude each other within the primary process instead.
Reads through those descriptors are capped per surface.

| Surface | Cap | Above the cap |
|---|---|---|
| Context files | 1 MiB | Truncated with a warning; context preload truncation shortens them further |
| Workspace `SKILL.md`, skill references and scripts | 1 MiB each; names 64 characters; descriptions 1024 characters; 256 listed scripts and references each; 8 MiB of names, descriptions, instructions, metadata, and listings and 256 skills per workspace | The file or a skill with a longer name is refused, a longer description or listing is truncated, and skills beyond the budget or count are skipped, each with a warning |
| Call transcripts sent to Mem0 | 64 MiB | Truncated |
| Knowledge sources, including operator-managed ones | 64 MiB | Left out of the listing with a warning |
| Delegation event logs and `run.json` | 4 MiB per event; 64 MiB and 65,536 events per log; 4 MiB per `run.json`; 64 KiB per inline output, error, or usage value | An event or `run.json` that would exceed a cap is refused before it is written, and other events stop 1 MiB and one event short of the log caps so the terminal event still fits. A planted log or `run.json` above a cap, a `run.json` missing or mistyping a field, and a `run.json` or terminal event whose output, error, or usage exceeds 64 KiB, which the primary always moves to artifacts, make the record unreadable. `run.json` keeps only its known fields and is written as compact JSON, each write reads the log one event at a time, and `transcript.md`, with event data as compact JSON, is rendered only when the delegation finishes |
| Todo templates, scheduled-run receipts, thread-export files, `file` and `coding` reads | 64 MiB | Refused with a logged error |

Git commands the primary runs in a workspace, for knowledge checkouts and the `coding` tool's ignore check, use the hardened Git command and environment so programs named in workspace Git config never run.

Which Matrix user may drive an agent, act in a room, or approve a change is a separate question.
Access policy and requester authorization govern it, independently of the tool trust model.

## File access

`defaults.file_access` sets the default and `agents.<name>.file_access` overrides it for one agent.

| Value | Path tools may use |
|---|---|
| `workspace` (default) | Files inside the agent workspace and attachments available in the conversation |
| `unrestricted` | Any file the tool's process can reach; on a worker that is the worker container |

Every tool declares how its own file access relates to this setting.

| Tool class | Tools | Behavior |
|---|---|---|
| Path tools | `attachments` (including `view_file`), `matrix_message`, `gmail`, `google_drive`, `browser` uploads, `e2b` uploads | Follow the agent's `file_access` and read through no-follow descriptors, so replaced workspace roots and swapped files are refused |
| Worker path tools | `file`, `coding` | Follow the agent's `file_access`; reads, writes, chunk edits, and deletes walk to the checked path through no-follow descriptors, and writes replace the file atomically, while listing and search check the resolved path and then walk it by path, as the known gap below describes |
| Unconfined tools | Code-execution tools (`shell`, `python`, `docker`, `script`, `claude_agent`) and tools whose queries, paths, or URLs reach local files without confinement (`duckdb`, `csv`, `pandas`, `sql`, `composio`, `postgres`, `redshift`, `visualization`, `moviepy_video_tools`, `groq`, `openai`, `airflow`, `browserbase`, `agentql`, `newspaper`, `slack`, `web_browser_tools`) | Class `unconfined`: not confined by `file_access`, whatever the agent's setting; authored tool config may only state `file_access: unconfined` |
| Other tools | Everything else | Take no local file paths |

MCP servers on the local `stdio` transport are unconfined too, because they are operator-launched programs; remote `sse` and `streamable-http` servers take no local file paths.
Every tool, including plugin tools, must declare its class when it registers, so a tool cannot silently default to taking no paths.
Whether a tool executes code is a separate metadata flag from its file access class; only the code-execution tools above carry it.
A worker isolates unconfined tools that support worker routing (`shell`, `python`, `docker`, `csv`, `postgres`, `redshift`, `visualization`, `moviepy_video_tools`, `groq`, `openai`, `airflow`, `agentql`, `newspaper`, `web_browser_tools`).
The rest require the primary runtime and cannot run in a worker (`claude_agent`, `script`, `duckdb`, `pandas`, `sql`, `composio`, `browserbase`, `slack`), so enable them only for agents trusted with everything the primary runtime can reach.
MindRoom logs a warning when an agent routes code-execution tools to a worker while primary-process tools stay unconfined, meaning unconfined tools or path tools under `file_access: unrestricted`, because those tools can then read runtime secrets the worker was meant to keep away.
The model sees the effective file access and the unconfined tools in its tool execution environment description.

## Known gaps

These are tracked gaps, not intentional behaviors; fix them rather than documenting around them.

- The unconfined non-code tools listed above do not yet follow `file_access`; a separate change will confine their explicit path and URL arguments.
- The listing and search functions of `file` and `coding` (`list_files`, `search_files`, `search_content`, `grep`, `find_files`, and `ls`) resolve links and refuse paths that lead outside the workspace, but then walk and read by path. This matters only when an operator routes these tools to the primary process while worker code writes the same workspace: code that swaps a checked directory or file for a link between that check and the walk can make them list or return the contents of any file the primary process can read, including other workspaces and primary-owned state, and a planted FIFO can stall a content search. They run in a worker by default, where they see only what the worker already mounts.
- `tests/test_file_access_contract.py` holds every tool that follows `file_access` to the confinement scenarios and the descriptor-based path tools also to the link-swap scenarios; a tool must be added there before it can be declared, and `file` and `coding` cover their descriptor-based reads with their own link-swap test there.
- Knowledge Git commands refuse a worktree whose path goes through a link, but Git itself then reopens that worktree by path. For a Git-backed knowledge base inside a workspace a worker writes, code that swaps the knowledge folder for a link in the short window between that check and the Git command can make that one sync check out or update files at the link target with the primary's permissions; the next sync refuses the link.
- SQL-capable tools (`duckdb`, `csv`, `sql`, and `pandas` query helpers) embed file paths inside queries, so guarding explicit path arguments cannot confine them; they stay unconfined until a query-level mechanism exists.

## Intentional behaviors

Do not report or "fix" these; they are deliberate.

- Code-execution tools in the primary process can read, write, and upload anything the MindRoom process can reach.
- No in-process path, command, or import filter is added to `shell`, `python`, or any other code-execution tool.
- An operator who runs MindRoom without workers, for example inside a dedicated LXC container or VM, has chosen full trust; `file_access: unrestricted` matches that choice.
- `file_access: unrestricted`, or unconfined tools in the primary process, are allowed together with worker routing; they log a warning instead of failing.
- Hosted tenants may set `file_access: unrestricted`; it exposes only their own instance.
- With `file_access: workspace`, the browser, attachments, `matrix_message`, Gmail, and Google Drive may still use every file in the agent workspace.
- The browser and `matrix_message` also accept `att_*` IDs of attachments available in the conversation; Gmail and Google Drive take file paths only, so an agent first saves a received attachment into the workspace with `get_attachment(mindroom_output_path=...)` and then passes that workspace path.
- `register_attachment` copies the file's current bytes into managed attachment storage, so later edits or replacement of the source file never change what the attachment sends.
- `mindroom_output_path`, attachment saves, Google Drive downloads, and report publishing always write inside the workspace regardless of `file_access`, because they produce MindRoom-owned output.
- Writes into `.git` directories stay blocked for `file` and `coding` in both modes, because MindRoom runs Git in checkouts that may sit inside agent workspaces.
- A team's `access` authorizes its exact member agents for team requests, in Matrix and the OpenAI-compatible API, even members whose own `access` would not admit the requester directly; adding an agent to a team is a deliberate grant.
- A non-private agent's workspace is shared by every requester's runtime for that agent, including `user` and `user_agent` workers, so files one requester leaves there (such as `.mindroom/worker-env.sh`, `.pth` files, or Git config) can run in another requester's runtime; use `private` agents when requesters need isolation from each other.
- A shared knowledge base whose path lies inside an agent workspace, such as that agent's thread exports, is bound without following links below the workspace, so even an operator-made link there is refused; shared knowledge outside every agent workspace still follows operator links.
- After an upgrade from workers that mounted whole state roots, startup stops those workers before serving, failing until none remain, and warns; it does not scan for or repair links they may have planted above workspaces, because a scan on every start is costly and an automatic repair could itself follow a planted entry, so an operator runs the check in the migration guide.
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
