# Minimal Agent Mode

Minimal mode gives an existing agent a short prompt and one Bash tool instead of its full system prompt and tool schemas, so each request carries far fewer tokens.
The agent uses `mindroom-agent` in that Bash to discover and call its other tools.
Its identity, model, workspace, memory, history, and tool permissions stay the same, and switching modes does not create a second workspace or memory store.
Standard mode is the default.

You can use minimal mode for one agent in one conversation with `!mode`, or for one subagent run with `run_subagent(minimal=True)`.
Standard-mode agents with the `shell` tool can also call `mindroom-agent` inside their shell commands.
All three need the [deployment requirements](#deployment-requirements).

## Conversation mode

### `!mode`

Switch one agent's tool interface for its next Matrix responses in the current conversation:

```text
!mode helper minimal
!mode helper show
!mode helper standard
!mode helper reset
```

`standard` and `reset` both remove the saved choice and restore standard mode.
You must be authorized to use the named agent.
For agents using threads, run the command inside an existing thread and keep talking to the agent in that thread.
For agents using `thread_mode: room`, the choice applies to the whole room, including commands sent from a thread.
Private agents store the choice separately for each requester.
The choice survives restarts without changing `config.yaml`, and other agents and conversations are unaffected.
Teams and OpenAI-compatible API requests do not use this selection.
A response already in progress keeps the mode it started with.

Selecting minimal mode does not grant shell access or add tools.
If the agent or deployment cannot support minimal mode, the reply lists every missing requirement with its fix, names the `.env` file for deployment settings, and links to the [deployment requirements](#deployment-requirements), without saving the choice.
If shell permissions or deployment settings change after minimal mode was selected, minimal responses fail instead of running; run `!mode helper standard` in the same conversation to return to standard mode.

### Prompt and `minimal_instructions`

The minimal prompt identifies the agent, lists its loaded workspace context files, and names the toolkits available through `mindroom-agent`.
With file memory, it also names the workspace-relative `MEMORY.md` and `memory/` locations.
File contents, the role, ordinary instructions, and tool guidance are not in the prompt; the agent reads them through `mindroom-agent context` when it needs them.

`minimal_instructions` (list of strings, default `[]`) is appended to the minimal prompt on every minimal request:

```yaml
agents:
  helper:
    display_name: Helper
    tools: [shell]
    minimal_instructions:
      - Read the project instructions before changing files.
```

Because guidance moved out of the prompt is seen only when the agent looks it up, put short guidance that must apply on every request in `minimal_instructions`, and keep durable notes in the agent's memory.

## Minimal subagents

A caller with the [`delegate`](agent-orchestration.md#delegate) tool can pass `minimal=True` to `run_subagent` to run that child in minimal mode, and `continue_subagent` keeps the child's mode.
Use it for self-contained tasks where the child does not need its role, instructions, and tool guidance up front; it can still read them with `mindroom-agent context`.
The option is offered only in Matrix conversations, only for allowed subagents that have the `shell` tool and meet the [deployment requirements](#deployment-requirements), and never while shell commands require approval.
A minimal subagent cannot pause for approval, so approval-gated tools are hidden from it.
A listed child whose shell permissions or workspace still rule out minimal mode fails with the reason and a hint to start a new subagent without minimal.

## Standard mode

A standard-mode agent with the `shell` tool can use `mindroom-agent` inside its shell commands, so one script can combine many tool calls, loop over results, or filter them before anything returns to the model.
It is available automatically when the agent answers a Matrix conversation itself and its shell meets the [deployment requirements](#deployment-requirements); the agent's instructions then mention it.
Team members, call agents, workflow participants, and OpenAI-compatible requests do not get it.
The agent keeps all of its tools as ordinary tools as well.
Tools that may require approval, ask the requester a question, delegate to another agent, or end the turn are not offered through the CLI in standard mode; the agent calls them directly instead.
The CLI is not offered when the agent's shell commands themselves require approval, and after a response pauses for any approval, the rest of that response continues without it.
Media returned by CLI calls reaches the model with the shell command's result, and streamed responses show CLI calls as their own tool-trace entries.

## Discover and call tools

```bash
mindroom-agent --help
mindroom-agent tools list
mindroom-agent tools search calendar
mindroom-agent tools describe TOOLKIT FUNCTION
mindroom-agent tools call TOOLKIT FUNCTION --json '{"argument":"value"}'
mindroom-agent calls get CALL_ID
mindroom-agent calls wait CALL_ID
mindroom-agent context list
mindroom-agent context read NAME --offset 0 --limit 8000
```

Tool names are qualified by toolkit, and `TOOLKIT.FUNCTION` works anywhere `TOOLKIT FUNCTION` does.
Discovery and calls use the current agent's permissions and requester context.
Use command-specific `--help` for arguments and options.

`tools call` waits up to 30 seconds and prints the call's receipt: call ID, status, and the outcome once finished.
Use `--timeout SECONDS` to wait longer, or `--timeout 0` to return at once.
For a call that is still queued, running, or waiting for a decision, `calls wait` polls for up to 30 seconds (or `--timeout SECONDS`) and then returns the current receipt with exit code `3`; reaching the timeout does not cancel the tool.
Waiting pauses that shell command and its response; other conversations continue.

Arguments come from `--json`, a final positional argument (`tools call TOOLKIT FUNCTION '{...}'`), `--json-file arguments.json`, or `--json-stdin`.
Use only one of these; the value must be a JSON object, and omitting all of them sends an empty object.
Use `--call-id UUID` to fix the call ID before submission; reusing an ID with the same tool and arguments joins the existing call, and different arguments are rejected.

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

`context read` accepts only names returned by `context list` or by other commands, not filesystem paths.
Large schemas return a context name for paged reading.
Large tool results return a preview and a workspace file path holding the full output; for an agent without a workspace, they are shortened instead.
Images, audio, and files are handled like ordinary response attachments.

## Exit codes

| Code | Meaning |
| --- | --- |
| `0` | Discovery succeeded or the call completed |
| `1` | Call failed or was cancelled |
| `2` | Invalid input or rejected request |
| `3` | Call remains queued, running, or waiting |
| `4` | Authority or transport unavailable; submission outcome may be unknown |

Scripts using `set -e` must handle exit code `3` explicitly.
After exit code `4`, check the original call ID before retrying, because submitting with a fresh ID may repeat a side effect.

## Approvals, shells, and limits

CLI calls follow the agent's existing approval rules and interactive questions, and an approval applies to the exact tool and arguments shown.
Each Bash command starts a fresh shell: workspace files persist, but shell variables and directory changes do not carry over.
Shell commands can run in parallel, and each keeps its own calls and media.
A command can submit new calls only while it is running or its earlier calls are finishing; later calls, including those from background commands whose Bash call has returned, are rejected rather than queued.
`calls wait` on earlier receipts works until the response ends.

CLI calls are limited by the agent's `max_tool_calls_per_turn`, counted separately from its direct tool calls; calls past the limit return a failed receipt.
One response keeps at most 1024 call receipts and runs at most 64 CLI operations at once; further submissions are rejected with an error.
Call receipts do not survive a restart.
A pending approval still resumes after a restart once it is decided, but MindRoom does not rerun the Bash script that requested it; when the approval was for the Bash command itself, the agent is told the command did not run and can issue it again.

## Deployment requirements

Minimal Bash runs where the agent's ordinary shell runs.
Every mode that uses `mindroom-agent` needs the `shell` tool on the agent and MindRoom running with its API server, not `--no-api`.
Minimal mode and minimal subagents also need:

- the shell's run, check, and kill commands allowed;
- an agent workspace, which `memory_backend: file` or a `private` agent provides.

`!mode <agent> minimal` lists every missing requirement at once.
Set environment settings in the runtime's `.env` and restart MindRoom.

### Shell in MindRoom itself

When the agent's shell runs in the MindRoom process, which is the default without a worker backend, nothing else is needed.
Such an agent is already fully trusted, as described in [the security posture](../architecture/security-posture.md).

### Shell in a worker

When the agent's shell runs in a worker, Bash runs in that same worker, which must use an image from the same MindRoom release.
Worker shells can reach MindRoom's API, so the API must be locked down:

- Set `MINDROOM_API_KEY`; `mindroom run` adds a generated key to `.env` when Docker or Kubernetes workers are configured and the dashboard has no credential, while an explicitly empty `MINDROOM_API_KEY=` keeps open access and leaves minimal mode unavailable.
- With trusted-upstream authentication enabled, set `MINDROOM_TRUSTED_UPSTREAM_REQUIRE_JWT=true`.
- Unset `OPENAI_COMPAT_ALLOW_UNAUTHENTICATED`.

Workers call MindRoom at `MINDROOM_AGENT_CLI_PRIMARY_URL`, a plain `http(s)://host:port` origin, when it is set.
Otherwise MindRoom uses its own API address; with Docker workers and the default API bind on every interface, workers reach it through `host.docker.internal`.
Kubernetes workers and shared static runners need `MINDROOM_AGENT_CLI_PRIMARY_URL` unless the API listens on a specific non-loopback address.
An API that listens only on loopback cannot be reached from workers, and a host firewall may drop connections from Docker networks; a `mindroom-agent` call that cannot connect names the address it tried.
With the runtime chart's worker egress policy, allow the API through `egressProxy.networkPolicy.extraEgress`, and admit workers in `networkPolicy.apiIngressFrom` when that list is set, because `mindroom-agent` connects directly rather than through the egress proxy.

The shell receives a grant scoped to its own active response, not provider or administrator credentials, and provider credentials stay where the tools already run.
The grant cannot select another agent, requester, or conversation, and it stops working when the response ends.
Other code in the same worker can read the grant while the response runs, and anything that copies it can use the same permissions from any host that reaches MindRoom's API until then; agents and requesters that share a worker already share its trust.
The external MCP gateway keeps its own authentication and tool restrictions; minimal mode does not widen them.

## Compare modes

A smaller initial prompt alone does not establish lower total cost or better task quality.
To evaluate minimal mode, run the same tasks with the same agent, model, tool configuration, and starting workspace in both modes, including tool-heavy work, approval waits, and tasks that depend on long instructions.
Compare task success, input and output tokens, elapsed time, editing failures, and discovery calls, and record the model and configuration versions.
