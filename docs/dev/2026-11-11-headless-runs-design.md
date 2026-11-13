# Headless Runs

This record covers why headless runs work as they do and what was rejected.
The behavior itself is in [Headless Runs](../headless.md).

## Goal

Let another Python program run one configured agent's turn, with no Matrix homeserver and no `mindroom run`, in a directory the caller chooses.
Callers are scripts and CI jobs that hand an agent a task, evaluation harnesses that run the same tasks across agents, models, and configurations (as [Compare modes](../tools/agent-cli.md#compare-modes) asks for), and applications that embed an agent.

## Decisions

1. The API is a coroutine awaited on the caller's event loop, with no thread or subprocess, so the caller's cancellation reaches the turn and its shell commands.
   The turn seam `ai_response` needs no Matrix; `mindroom run` first waits for a homeserver.
   The loop's pooled model connections close when the last headless turn running on that loop ends, because concurrent turns share the pool and closing it ends their streams.
2. Headless turns have their own execution channel, `headless`, beside `openai_compat`.
   Delegation outside Matrix needs a bound detached requester on a non-Matrix channel, and the prompt drops Matrix identity and sender attribution for both channels.
   A headless agent has an identity prompt of its own, `HEADLESS_AGENT_IDENTITY_CONTEXT_TEMPLATE`, because the API's names the API.
   Reusing `openai_compat` would have needed no new sites but would label headless tool calls, logs, and subagent records as API traffic.
3. The requester must be an administrator, the first one by default.
   Delegation rechecks the requester's responder access to each target, and outside a room only administrators pass it for every agent.
4. A run records the router's, every agent's and team's, and the internal user's Matrix accounts in `matrix_state.yaml` without credentials.
   Turn code resolves every configured entity's Matrix ID, for prompts, sender attribution, and authorization.
   A storage that already holds credentialed accounts is refused, because a later `mindroom run` on it stops with `Stored Matrix credentials for <agent> are incomplete` for any agent that got such a record.
5. Refusals cover only what a headless run cannot serve: a running MindRoom indexes knowledge bases, semantic file memory, and the knowledge graph, loads plugins, connects MCP servers, and runs workers.
   Without the refusals, knowledge, plugin tools, and MCP tools would silently be missing from the turn.
   `shell` without `defaults.allow_shell_without_worker` is refused because it would answer every command with an error.
   Mem0 recall and file memory run headless, so they are not refused; a caller that wants no memory sets `memory: none`.
   Teams are out of scope for now.
6. `workdir` is a parameter of the run, not a config key, so `base_dir` stays unauthorable in `config.yaml`.
   It is bound in a context variable for the turn, which the tools of every subagent are built in, so subagents share it.
7. Saved tool outputs stay under `storage`, in the workspace's `mindroom_tool_outputs/`, and their receipts give the absolute path.
   A receipt's workspace-relative path would resolve against the workdir, where the file is not, or where a different file of the same name may be.
   Under `file_access: workspace`, `file` and `coding` get that one directory as an extra read-only root, which keeps their confinement as narrow as possible while they can still read what the receipts name.
   The system prompt leaves out its notes on saving outputs and skills into the workspace, because the tools would resolve those relative paths in the workdir.
   The other workspace paths that the prompt and the tool descriptions name relatively, such as subagent profiles and delegation records, stay as they are, and the tool environment section names the workdir and the workspace by absolute path instead; rendering each of those overridable prompts and descriptions per channel would split templates that configs can override.
   A delegation receipt beside a workdir names the child's record by its absolute path, because that record lives in the child agent's workspace, which the caller's prompt does not name.
   Only saved outputs move: tools that keep files of their own in the workspace, such as `file_generation` or `google_drive`, keep it, so a workdir changes nothing for them.
8. `HeadlessResult.tool_calls` counts only the lead's calls in its final attempt, which the run metadata already carries.
   Counting subagent runs and their calls would need plumbing through delegation that only some callers need, and those callers see every request in their own model traffic.

## Rejected alternatives

- `/v1` without Matrix, through `mindroom run` with only its API.
  `/v1` exists only inside `mindroom run`, which needs a homeserver; delegation over `/v1` needs an API key mapped to a human Matrix user; and a caller would own a server's lifecycle and an HTTP request that its cancellation does not cross.
- Pointing the agent's workspace at the caller's directory.
  It would put context files, `mindroom_tool_outputs/`, subagent profiles, and delegation records among the caller's files.
- Saving tool outputs in the workdir.
  They are MindRoom's state, and they would appear among the caller's files.
- Pointing every toolkit's workspace at the saved-output directory beside a workdir.
  It would move the files of tools that the workdir does not concern.
- Giving `file` and `coding` the whole agent workspace as an extra root.
  Reading saved outputs needs only their directory.
- Refusing Mem0 memory.
  It runs headless, and a caller that wants none can say so in its config.

## Limitations

- Agents run in standard mode only: minimal mode needs `mindroom run`'s API for `mindroom-agent`.
- Under `file_access: workspace`, `file` and `coding` beside a workdir cannot reach the workspace's skills, subagent profiles, or delegation records while `storage` is outside the workdir; `shell` and `python`, being unconfined, and `file_access: unrestricted` still reach them.
- Beside a workdir, the skills note still says editing the agent's own workspace skills with file tools is fine, which the previous limitation contradicts; leaving it out needs splitting a prompt that configs can override.
- Tools that work on files of their own, such as `file_generation`, keep the workspace beside a workdir, and the workspace-relative paths their replies name do not resolve in the workdir.
