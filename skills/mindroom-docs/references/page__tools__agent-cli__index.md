# Minimal Agent Mode

Minimal mode gives an existing agent a short prompt and one Bash tool.
The agent uses `mindroom-agent` inside its shell worker to discover and call its other tools.
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
If the deployment or agent cannot support minimal mode, the command reports why and keeps the previous selection.

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
Discovery and calls use the current agent's permissions and requester context.
Calling a tool returns JSON containing its call ID and current status.
Top-level `--help` lists every command, including `calls wait CALL_ID` for queued, running, or waiting calls.
Use command-specific `--help` for arguments and options.
Use `calls wait` when the result is still queued, running, or waiting for a decision.
It polls for up to 30 seconds by default, then returns the current receipt with exit code `3` if still pending.
Use `--timeout SECONDS` to choose another polling budget; reaching it does not cancel the tool.
Waiting pauses that shell command and its owning response; other conversations can continue.
New tool calls and schema preparation require an active Bash call.
Background shell commands that submit new work after Bash has returned are rejected instead of being queued for a later call.

Arguments can also come from `--json-file arguments.json` or `--json-stdin`.
These three JSON input options are mutually exclusive and require a JSON object.
Omitting them supplies an empty object.
Use `--call-id UUID` when the caller needs to retain a known ID before submission.
Reusing an ID with the same tool and arguments joins the existing live call; different arguments are rejected.

| Command | Options |
| --- | --- |
| `tools list` | `--cursor`, `--limit` (default 100) |
| `tools search QUERY` | `--toolkit`, `--limit` (default 20) |
| `tools describe TOOLKIT FUNCTION` | None |
| `tools call TOOLKIT FUNCTION` | `--call-id`, `--json`, `--json-file`, `--json-stdin` |
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
Large tool results provide a bounded preview and a workspace artifact path containing the full output.
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
The isolated worker remains owned by the response across its live waits and continuations, then retires when the response ends.
Each Bash command starts a fresh shell; workspace files persist, while shell variables and working-directory changes do not carry into the next command.
Background command handles belong to that response's worker.

CLI tool calls count against the agent's `max_tool_calls_per_turn` budget; calls past it return a failed receipt.
One response keeps at most 1024 call receipts and runs at most 64 CLI operations at once; further submissions are rejected with an explanatory error.
Ordinary call results and duplicate-call tracking live only as long as their response owner.
They are unavailable after process restart.
Pending approvals use existing durable approval recovery.
If a pending approval outlasts the CLI grant's 24-hour limit, its live shell retires and the approval resumes with a fresh worker after the decision.
Recovery does not directly rerun a saved outer Bash script, and an already claimed interrupted approval is not dispatched again.
When the recovered approval is for the outer Bash command itself, the agent is told that the command was not run and can issue it again.
A recovered approval streams its progress into the paused reply through the same continuation driver as a standard approval.
If MindRoom stops abruptly while a Bash call is running, that unfinished turn is left out of later model history.
Normal agent restart behavior still applies to subsequent model decisions; this is not a guarantee against repeating semantically equivalent work.

## Deployment requirements

The first supported profile requires a dedicated Docker worker with the `mindroom-agent` CLI installed and the agent's existing shell permission.
Local and static shell runners are unsupported.
Docker worker images from earlier releases lack the CLI routes; minimal mode reports that the image must be updated, while standard mode keeps using them.
Configure the existing Docker worker backend and separate origins for `MINDROOM_AGENT_CLI_GATEWAY_URL` and `MINDROOM_AGENT_CLI_PRIMARY_URL`.
The primary API must require `MINDROOM_API_KEY` authentication.
Unauthenticated OpenAI execution and spoofable trusted-upstream header authentication are unsupported.

The CLI gateway forwards only `POST /api/agent-cli/operations` and `GET /api/agent-cli/calls/<call-id>`.
Other API and worker-control routes must not be forwarded by that gateway.
Startup checks verify those routes and reject unsupported authentication setups.
The Docker profile isolates processes, mounted state, and injected credentials; it is not a network sandbox.
Outbound internet, LAN services, and cloud metadata endpoints remain reachable when the deployment's network allows them.
Operators must block access to network-provided credentials, including cloud metadata, through their deployment's egress controls.
The startup probes check MindRoom's protected routes; they do not certify isolation from arbitrary network services.

Provider credentials stay at the tools' existing authorized execution locations.
The shell receives a grant for its own active response, not provider or administrator credentials.
That grant is readable by its authorized shell and cannot select another agent, requester, or conversation.
The grant is bearer authority: a process that can read and export it can use the same scoped permissions from any host that can reach the gateway, until expiry or revocation.
The external MCP gateway keeps its separate authentication and compatible-tool restrictions; minimal mode does not widen its exposed tool set.

## Compare modes

Run the same tasks with the same agent, model, tool configuration, and starting workspace in both modes.
Compare task success, input and output tokens, elapsed time, editing failures, and discovery calls.
Include tool-heavy work, approval waits, and tasks that depend on long instructions.
Record model and configuration versions with the results.
A smaller initial prompt alone does not establish lower total cost or better task quality.
