# Minimal Agent Mode

Minimal mode gives an existing agent a short prompt and one Bash tool.
The agent uses `mindroom-agent` in that Bash, where its shell normally runs, to discover and call its other tools.
Its identity, model, workspace, memory, history, and tool permissions stay tied to the same agent.
Standard mode is the default.

## Select a mode

In the conversation, use:

```text
!mode helper minimal
!mode helper show
!mode helper standard
!mode helper reset
```

The selection uses the selected agent's conversation scope.
For a thread-mode agent, issue the command inside an existing thread; for a room-mode agent, it applies to the room.
Private-agent selections retain the requester's private storage scope.
Teams and the OpenAI-compatible API do not use these Matrix conversation overrides.
An active response keeps the mode it started with.
Reset removes the saved override and restores the standard default.
Selecting minimal mode does not grant shell access or add tools.
If the deployment or agent cannot support minimal mode, the command keeps the previous selection and lists every missing requirement with its fix, the `.env` file for deployment settings, and a link to the [deployment requirements](#deployment-requirements).

### `!mode`

Switch one agent's tool interface for its next Matrix response in the current conversation.
Minimal mode exposes only Bash to the model; the agent's configured tools remain available through the MindRoom CLI.
Standard mode restores the usual tool interface.

```
!mode helper minimal
!mode helper show
!mode helper standard
!mode helper reset
```

The choice survives restarts without changing `config.yaml`.
For agents using threads, run the command inside an existing thread and continue talking to the agent in that same thread.
For agents using `thread_mode: room`, the choice applies to the whole room, including commands sent from a thread.
Private agents store the choice separately for each requester.
Other agents and conversations are unaffected; teams and OpenAI-compatible API requests do not use this selection.

You must be authorized to use the named agent.
Minimal mode requires the agent's existing run, check, and kill shell permissions, a canonical workspace, and MindRoom's API server, reachable from wherever the agent's shell runs.
When anything is missing, the reply lists every missing requirement with its fix and the `.env` file for deployment settings, without saving the choice.
See [deployment requirements](#deployment-requirements).
If shell permissions or deployment settings later change, minimal responses fail closed.
Run `!mode helper standard` or `!mode helper reset` in the same conversation to remove the saved choice and restore standard mode.

## Minimal subagents

A caller with the [`delegate`](agent-orchestration.md#delegate) tool can pass `minimal=True` to `run_subagent` to run that child in minimal mode.
The child starts with the short prompt and Bash tool described here instead of its full system prompt and tool schemas, so each of its requests carries far fewer tokens.
Use it for self-contained tasks whose child does not need its role, instructions, and tool guidance up front; the child can still read them with `mindroom-agent context`.
`continue_subagent` keeps the child's mode.
The tool description and schema offer minimal mode only for allowed subagents that have the `shell` tool and whose shell location meets the [deployment requirements](#deployment-requirements); otherwise the option is hidden.
A listed child whose shell permissions or workspace still rule out minimal mode fails with the reason and a hint to start a new subagent without minimal.
A minimal subagent cannot pause for approval, so approval-gated tools are hidden from it, and minimal mode is not offered while shell commands require approval.
Minimal subagents run only inside Matrix conversations.
A minimal child runs Bash where its own shell runs.

## Standard mode

Standard-mode agents with the `shell` tool can also use `mindroom-agent` inside their shell commands, so one script can combine many tool calls, loop over results, or filter them before anything returns to the model.
It is available automatically when the agent answers a Matrix conversation itself and its shell meets the [deployment requirements](#deployment-requirements); the agent's instructions then mention it.
Team members, call agents, workflow participants, and OpenAI-compatible requests do not get it.
The agent keeps all of its tools as ordinary tools as well.
Tools that may require approval, ask the requester a question, delegate to another agent, or end the turn are not offered through the CLI in standard mode; the agent calls them directly instead.
The CLI is not offered when the agent's shell commands themselves require approval, and after a response pauses for any approval, the rest of that response continues without it.
Calls are admitted only while the shell command that makes them is running or its earlier calls are finishing; shell commands still run in parallel, and each keeps its own calls and their media.
Media returned by those calls reaches the model with the shell command's result.
Calls through the CLI count against their own `max_tool_calls_per_turn` budget, separate from the agent's ordinary tool calls.
Streamed responses show calls made through the CLI as their own tool-trace entries.

## Instructions and context

Minimal mode replaces the automatically assembled long prompt with discovery guidance and optional `minimal_instructions`:

```yaml
agents:
  helper:
    display_name: Helper
    tools: [shell]
    minimal_instructions:
      - Read the project instructions before changing files.
```

Role, ordinary instructions, context, and tool guidance remain available through `mindroom-agent context`.
The short prompt identifies the agent, lists loaded workspace context files, and names the toolkits available through `mindroom-agent`.
File memory adds the workspace-relative `MEMORY.md` and `memory/` locations using the agent's existing memory configuration.
File contents are not automatically added to the minimal prompt.
Command syntax and interactive-question guidance are available through `mindroom-agent --help` and its context commands.
Custom `minimal_instructions` are appended after this generated prompt.
Moving guidance out of the initial prompt changes when the model sees it.
Use `minimal_instructions` for concise guidance that should be present on every minimal request.
Use the agent's existing memory system for durable notes and `minimal_instructions` for custom minimal-mode guidance.
Switching modes does not create a second workspace or memory store.

### Conversation mode

Standard mode is the default.
An existing shell-enabled agent can select minimal mode per conversation with `!mode <agent> minimal`.
Minimal mode presents one Bash tool and discovers other tools through `mindroom-agent`.
It keeps the same identity, workspace, memory, history, and permissions.
The optional `minimal_instructions` list defaults to `[]` and supplies concise guidance on every minimal request; ordinary instructions remain available through CLI context discovery.

## Discover and call tools

```bash
mindroom-agent --help
mindroom-agent tools list
mindroom-agent tools search calendar
mindroom-agent tools describe TOOLKIT FUNCTION
mindroom-agent tools call TOOLKIT FUNCTION --json '{"argument":"value"}'
mindroom-agent calls get CALL_ID
mindroom-agent calls wait CALL_ID
```

Tool names are qualified by toolkit.
`TOOLKIT.FUNCTION` works anywhere `TOOLKIT FUNCTION` does.
Discovery and calls use the current agent's permissions and requester context.
Calling a tool waits up to 30 seconds for the result and prints its receipt: call ID, status, and the outcome once it has finished.
Use `--timeout SECONDS` on the call to choose another budget, or `--timeout 0` to return at once.
Top-level `--help` lists every command, including `calls wait CALL_ID` for queued, running, or waiting calls.
Use command-specific `--help` for arguments and options.
Use `calls wait` when the result is still queued, running, or waiting for a decision.
It polls for up to 30 seconds by default, then returns the current receipt with exit code `3` if still pending.
Use `--timeout SECONDS` to choose another polling budget; reaching it does not cancel the tool.
Waiting pauses that shell command and its owning response; other conversations can continue.
New tool calls and schema preparation must come from a Bash call that is still running or whose earlier calls are still finishing; later ones are rejected instead of being queued for a later call.

Arguments can also be given as a final argument (`tools call TOOLKIT FUNCTION '{...}'`), or come from `--json-file arguments.json` or `--json-stdin`.
These JSON inputs are mutually exclusive and require a JSON object.
Omitting them supplies an empty object.
Use `--call-id UUID` when the caller needs to retain a known ID before submission.
Reusing an ID with the same tool and arguments joins the existing live call; different arguments are rejected.

| Command | Options |
| --- | --- |
| `tools list` | `--cursor`, `--limit` (default 100) |
| `tools search QUERY` | `--toolkit`, `--limit` (default 20) |
| `tools describe TOOLKIT FUNCTION` | None |
| `tools call TOOLKIT FUNCTION [JSON]` | `--call-id`, `--json`, `--json-file`, `--json-stdin`, `--timeout` (seconds to wait for the result, default 30) |
| `calls get CALL_ID` | None |
| `calls wait CALL_ID` | `--timeout` (polling seconds, default 30) |
| `context list` | `--cursor`, `--limit` (default 100) |
| `context read NAME` | `--offset` (default 0), `--limit` (default 8000) |

## Read context and large results

```bash
mindroom-agent context list
mindroom-agent context read NAME --offset 0 --limit 8000
```

Use names returned by context discovery.
Context reads do not accept arbitrary filesystem paths.
Large schemas provide a context name for paged readback.
Large tool results provide a bounded preview and a workspace artifact path containing the full output; for an agent without a workspace, they are shortened instead.
Images, audio, and files remain available through the normal response attachment handling.

## Exit codes

| Code | Meaning |
| --- | --- |
| `0` | Discovery succeeded or the call completed |
| `1` | Call failed or was cancelled |
| `2` | Invalid input or rejected request |
| `3` | Call remains queued, running, or waiting |
| `4` | Authority or transport unavailable; submission outcome may be unknown |

Exit code `3` is an unfinished call, so scripts using `set -e` must handle it explicitly.
After an uncertain submission, retain the original call ID and check it before deciding what to do next.
Submitting a fresh ID may repeat a side effect.

## Approvals, shell lifetime, and restart

Calls use the agent's existing approval rules and interactive response handling.
An approval decision applies to the saved tool and arguments.
The response keeps its grant across its live waits and continuations and revokes it when the response ends.
Each Bash command starts a fresh shell; workspace files persist, while shell variables and working-directory changes do not carry into the next command.
Background commands behave like the agent's ordinary shell commands, but their new `mindroom-agent` calls are rejected once their Bash call has returned and its calls have finished; `calls wait` on earlier receipts works until the response ends.

CLI tool calls count against the agent's `max_tool_calls_per_turn` budget; calls past it return a failed receipt.
One response keeps at most 1024 call receipts and runs at most 64 CLI operations at once; further submissions are rejected with an explanatory error.
Ordinary call results and duplicate-call tracking live only as long as their response owner.
They are unavailable after process restart.
Pending approvals use existing durable approval recovery.
If a pending approval outlasts the CLI grant's 24-hour limit, its grant expires and the approval resumes with a fresh grant after the decision.
Recovery does not directly rerun a saved outer Bash script, and an already claimed interrupted approval is not dispatched again.
When the recovered approval is for the outer Bash command itself, the agent is told that the command was not run and can issue it again.
A recovered approval streams its progress into the paused reply through the same continuation driver as a standard approval.
If MindRoom stops abruptly while a Bash call is running, that unfinished turn is left out of later model history.
Normal agent restart behavior still applies to subsequent model decisions; this is not a guarantee against repeating semantically equivalent work.

## Deployment requirements

Minimal mode runs Bash where the agent's shell already runs, and needs the agent's existing run, check, and kill shell permissions, its workspace, and MindRoom's API server.
`!mode <agent> minimal` lists every missing requirement at once; set environment settings in the runtime's `.env` and restart MindRoom.

### Shell in MindRoom itself

When the agent's shell runs in the MindRoom process, which is the default without a worker backend, minimal mode needs no setup.
Bash runs in MindRoom like the agent's ordinary shell, and `mindroom-agent` calls the running API over its local address.
Such an agent is already fully trusted, as described in [the security posture](../architecture/security-posture.md), so no worker, key, or URL is required.

### Shell in a worker

When the agent's shell runs in a worker, minimal Bash runs in that same worker through the sandbox proxy, like the agent's ordinary shell commands.
Each command receives the response's grant and MindRoom's API address in its environment, so the worker image must come from the same MindRoom release.
Worker shells keep network access to MindRoom's API, so that API must require `MINDROOM_API_KEY`.
`mindroom run` adds a generated key to `.env` when Docker or Kubernetes workers are configured and the dashboard has no credential; an explicitly empty `MINDROOM_API_KEY=` keeps open access and leaves minimal mode unavailable.
Unauthenticated OpenAI execution and spoofable trusted-upstream header authentication are unsupported.

Workers call MindRoom back at `MINDROOM_AGENT_CLI_PRIMARY_URL` when it is set.
Otherwise MindRoom uses its own API address; with Docker workers and the default API bind on every interface, workers reach it through `host.docker.internal`.
Kubernetes workers and shared static runners need `MINDROOM_AGENT_CLI_PRIMARY_URL` unless the API listens on a specific non-loopback address.
An API that listens only on loopback cannot be reached from workers, and a host firewall may drop connections from Docker networks; a `mindroom-agent` call that cannot connect names the address it tried.
With the runtime chart's worker egress policy, allow the API through `egressProxy.networkPolicy.extraEgress`, and admit workers in `networkPolicy.apiIngressFrom` when that list is set, because `mindroom-agent` connects directly rather than through the egress proxy.

Provider credentials stay at the tools' existing authorized execution locations.
The shell receives a grant for its own active response, not provider or administrator credentials.
That grant cannot select another agent, requester, or conversation.
Other code in the same worker can read it while the response runs; agents and requesters that share a worker already share its trust, as described in [the security posture](../architecture/security-posture.md).
The grant is bearer authority: a process that can read and export it can use the same scoped permissions from any host that can reach MindRoom's API, until expiry or revocation.
The external MCP gateway keeps its separate authentication and compatible-tool restrictions; minimal mode does not widen its exposed tool set.

## Compare modes

Run the same tasks with the same agent, model, tool configuration, and starting workspace in both modes.
Compare task success, input and output tokens, elapsed time, editing failures, and discovery calls.
Include tool-heavy work, approval waits, and tasks that depend on long instructions.
Record model and configuration versions with the results.
A smaller initial prompt alone does not establish lower total cost or better task quality.
