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
| Worker path tools | `file`, `coding` | Follow the agent's `file_access` with lexical path checks only; they run in a worker by default, and the known gap below covers routing them to the primary process |
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
- `file` and `coding` confine paths lexically but do not open through no-follow descriptors, so when an operator routes them to the primary process while worker code shares the workspace, a link swapped into the workspace can redirect them; they run in a worker by default, where worker code already shares their trust.
- `tests/test_file_access_contract.py` holds every tool that follows `file_access` to the confinement scenarios and the descriptor-based path tools also to the link-swap scenarios; a tool must be added there before it can be declared, and `file` and `coding` are explicitly exempt from the link-swap scenarios until they read through descriptors.
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

## Reviewing security findings

Classify every finding before proposing a change.

1. Name the boundary it crosses: worker to primary, tenant to tenant, Matrix user to agent, or none.
2. Check whether the agent involved is trusted under the trust model above.
3. Check the intentional behaviors list.
4. Only a finding that crosses a real boundary and matches no intentional behavior is a bug.

A fix that restricts a trusted setup, or that blocks workspace and attachment flows an agent legitimately needs, is not a security fix.
When a maintainer rejects a finding as intended behavior, add it to the list above in the same change so the next reviewer does not rediscover it.
