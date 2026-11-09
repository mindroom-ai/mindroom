---
icon: lucide/terminal
---

# Persistent Terminals

A persistent terminal is an interactive Bash in a pty that keeps running between calls, keeps its recent output, and lets viewers attach and detach.
The sandbox runner keeps terminals, and the primary reaches them through `WorkerTerminals` in `mindroom.terminal.client`.
Agents use them through the `terminal` toolkit, and chat clients through the routes under [Chat client routes](#chat-client-routes).

## Decisions

- Terminals live in the sandbox runner process of the worker where the agent's shell commands run, not in the shell supervisor and not in the primary.
  The runner process lives as long as the worker, and a pty there inherits the worker's container, user, mounts, egress rules, and limits, so the worker stays the only isolation boundary.
  The per-request children that serve `/execute` exit with their request, so they cannot own a shell that outlives a call.
  An agent whose shell runs in the primary therefore has no terminal, and the toolkit offers it no function; a terminal host in the primary would be a separate decision.
- A terminal opens from the request a shell call of the agent's shell toolkit would send, prepared by the same runner code as `/execute`, so its environment, workspace, `PATH`, and the call's own environment changes are the ones the agent's commands get.
  `tests/terminal/test_worker_terminals.py` compares the two environments in both runner execution modes.
- The shell is `bash --noprofile --norc -i`: it reads no startup files, as the Bash that runs a shell command string reads none, so opening a terminal runs nothing that worker code wrote into the workspace's `~/.bashrc` or `~/.profile`.
- Input and resize travel only on a websocket the runner serves its viewers, so there is no second input path to keep consistent with it.
- Opening a terminal starts the agent's dedicated worker, as a shell call does; listing, attaching, and closing only look the worker up, so a stopped worker has no terminals and nothing starts one to answer.
  An attached viewer marks the worker used every minute, so the worker is not stopped as idle while someone watches it.
- The primary derives a terminal's owner from the request's worker key, routing agent, and workspace, so agents that share a worker or the shared runner never see each other's terminals, while requesters whose calls reach the same agent workspace share them.
  The runner takes the owner the primary sends, because only the primary holds the runner token.
- A session ID becomes a path on the runner, so the client accepts only the runner's own ID alphabet, and an ID such as `../../execute` cannot send the runner token to another runner endpoint.
- People type in a terminal only where its worker mounts the agent's workspace, a dedicated worker or a `static_runner` the deployment says mounts the agents' storage, so what they type acts on the files they see as the agent's `root`; on a runner that sees only its own scratch storage they watch.
- A terminal's shell never starts with a response's CLI grant: the shell outlives the response, while the grant is only for that response's commands.
  `WorkerTerminals.open` builds its request with the grant unbound rather than removing the grant's names afterwards, so the call's other environment changes stay exactly as a shell call gets them.

## Agent typing

An agent types into terminals only while its `run_shell_command` calls never ask for approval; otherwise the toolkit offers it only `terminal_read` and `terminal_list`, and every path a call takes, the model loop, the agent CLI, scheduled calls, and scripts, calls only what the toolkit offers, so no approval card can ask for typing it may not do, as it would under `tool_approval.default: require_approval` if the typing functions were offered and refused when called.
Approval rules judge a command's text, and what text typed into a persistent shell runs depends on what ran in it before: aliases and functions defined earlier, history expansion and other shell options, a shell started inside, and a program reading the keys instead of the shell.

- Rejected: judging typing through approval aliases as the command its Enter runs, from a record of the terminal's line prepared before approval and checked at the run.
  Seven review rounds each found another way the shell ran something other than the judged text, so a sound version would have to model the shell itself.
- Rejected: asking before every typing call of a restricted agent, because the card would show text whose effect neither the rules nor the approver can read from it, and an operator's rule for `run_shell_command` would become a card for the same commands through another function.
- People who may type keep typing whatever the agent's rules say, because their typing is their own and passes no approval.

## The line record

- The primary records every input it sends a terminal, an agent's and people's, one send at a time per terminal, in `mindroom.terminal.lines`, so `terminal_run` types only at an empty prompt and knows which prompt ends its command.
  It sends all of a terminal's input on one connection, which it opens while anyone watches and closes with the last viewer, because the runner applies input from separate connections in no promised order, and a keystroke recorded before a run could otherwise reach the shell after it and join the command.
  When the runner has ended the input connection, the next send counts the line as holding typing, since what the old connection carried last may not have arrived.
- The shell's prompt command prints an OSC 133 `D` mark with the last exit status and a prompt counter before each prompt, and the primary reads the marks from any stream attached to the terminal.
  The counter lets marks replayed with the scrollback count once, and the exit status is what `terminal_run` reports.
  The prompt command removes itself from the environment of what the shell starts, because a shell started inside would print marks counting from 1 again; until it exits, the line waits for a prompt.
- After an Enter or a Ctrl-C the shell is busy until its next prompt, because a running command or a continuation reads what is typed meanwhile; that prompt's line holds what was typed while busy, unless a Ctrl-C dropped it, and keys the shell reads first can keep it from printing that prompt.
  An Enter typed while busy keeps the shell busy past its prompts until a Ctrl-C, because the shell runs the typed-ahead lines one after another, and their prompts say nothing of whether more are queued.
  The line counts the prompts the shell owes, one after each Enter or Ctrl-C at the prompt, so a prompt owed from before a command, such as the one after a Ctrl-C, never ends that command.
  `terminal_send` refuses keys after a Ctrl-C in the same send, since the terminal may drop part of them, and `terminal_run` types only once the shell prompts after it, which a person's keystroke landing just before can keep it from doing, so the agent sends another Ctrl-C.
- Any other key at the prompt leaves the line holding typing until an Enter or a Ctrl-C; a send that may not have arrived, and a terminal the primary did not see open, such as after a primary restart, keep the shell busy until a Ctrl-C.
- Limitations: output a program prints, such as a file it shows, can carry forged prompt marks, which can end a command early in the record, so `terminal_run` types while that program reads the keys, or make the record ignore later prompts, so `terminal_run` refuses until the terminal is replaced.

## Chat client routes

Routes under `/api/rooms/{room_id}` sign the caller in with a client session as plugin routes do, admit them as `RoomReader.agent()` does, and name the agent by its Matrix user ID; a terminal is always one of that agent's workspace as a turn of it for the caller resolves it, and no path or worker comes from the client.

| Route | Body | Answer |
| --- | --- | --- |
| `GET /terminals` | none | `{"terminals": [...]}`: each terminal of the workspaces the caller may watch in the room, with `agent_user_id`, `id`, `cols`, `rows`, `alive`, `exit_code`, `created_at`, and `writable` |
| `POST /agents/{agent_user_id}/terminals` | `{"cols", "rows"}`, optional | `201` with the new terminal; `403` without `type` |
| `DELETE /agents/{agent_user_id}/terminals/{id}` | none | `204`; `403` without `type`; `404` for an ID that is not one of the workspace's terminals |
| `WS /agents/{agent_user_id}/terminals/{id}` | none | the stream below |

The stream signs in with `client_stream`, sends `{"type": "writable", "writable"}` first and whenever it changes, the scrollback and live output as binary frames, and `{"type": "exit", "code"}` before it closes with 1000 when the shell exits.
It takes `{"type": "input", "data"}` with at most 16384 characters, so a client sends a long paste in parts, and `{"type": "resize", "cols", "rows"}`, dropping both while the viewer may only watch, and closes with 1003 on any other frame.
Every 15 seconds, as the Computer's stream does, it rechecks `agent()` under the config served then: losing `type` sends `writable: false`, an agent no longer open to the caller or a moved `root` closes it with 1008, and a check that cannot be answered closes it with 1013, which the client may retry.
It also closes with 1008 for a terminal the workspace does not have, and with 1013 when the terminal's own stream ended, after which the client attaches again.

## Lifecycle and limits

- A session ends when its shell exits, when it has had no input and no attached viewer for 30 minutes, or when the runner stops.
  Output alone does not keep a session alive, so a forgotten shell is reclaimed even while a process in it keeps printing.
- An ended session keeps its scrollback and exit code for 30 minutes after its shell ends or its last viewer leaves, whichever is later, or until it is closed.
- Each session keeps its last 256 KiB of output for viewers that attach later.
- A viewer that falls 4 MiB behind is dropped and must attach again, so a slow viewer cannot grow the runner's memory without bound.
- A runner holds at most 8 sessions per owner and 64 in total; an owner at its limit first forgets its oldest ended session.

## Wire contract

Every route lives below the runner's API root at `/api/sandbox-runner/terminal`, requires the `X-Mindroom-Sandbox-Token` header, and names the owner in the `owner` query parameter.

| Route | Body | Answer |
| --- | --- | --- |
| `POST /sessions` | `{"request": <a shell call's ExecuteRequest>, "cols", "rows", "cwd"}` | the new session |
| `GET /sessions` | none | the owner's sessions, oldest first |
| `DELETE /sessions/{id}` | none | `204` after the shell ends |
| `WS /sessions/{id}/io` | none | the viewer stream below |

A session is `{"id", "cols", "rows", "alive", "exit_code", "created_at"}`, with `created_at` in Unix seconds.
`cwd` is a folder relative to the workspace, and the runner refuses a folder outside it with `400`.
The websocket sends the scrollback, when there is any, as one binary frame, then live output as binary frames, then `{"type": "exit", "code"}` as a text frame when the shell exits.
It takes `{"type": "input", "data"}` and `{"type": "resize", "cols", "rows"}` text frames.
It closes without an exit frame when its viewer fell too far behind, or sent a frame the runner refuses: a malformed one, input over 64 KiB, or input while 1 MiB of earlier typing waits unread, so a client sends a long paste in parts.
A viewer whose stream closed without an exit frame attaches again and gets the scrollback, and the exit if the shell has ended.
The stream refuses the handshake with `403` when the owner has no such session.
