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
- The shell is `bash --noprofile --norc +H -i` with `INPUTRC=/dev/null` and `HISTFILE=`: it reads no startup files, as the Bash that runs a shell command string reads none, so opening a terminal runs nothing that worker code wrote into the workspace's `~/.bashrc` or `~/.profile`, and it expands no `!` history and reads no key bindings or history file, which worker code could write to change what a judged line runs.
- Input and resize travel only on a websocket the runner serves its viewers, so there is no second input path to keep consistent with it.
- Opening a terminal starts the agent's dedicated worker, as a shell call does; listing, attaching, and closing only look the worker up, so a stopped worker has no terminals and nothing starts one to answer.
  An attached viewer marks the worker used every minute, so the worker is not stopped as idle while someone watches it.
- The primary derives a terminal's owner from the request's worker key, routing agent, and workspace, so agents that share a worker or the shared runner never see each other's terminals, while requesters whose calls reach the same agent workspace share them.
  The runner takes the owner the primary sends, because only the primary holds the runner token.
- A session ID becomes a path on the runner, so the client accepts only the runner's own ID alphabet, and an ID such as `../../execute` cannot send the runner token to another runner endpoint.
- People type in a terminal only where its worker mounts the agent's workspace, a dedicated worker or a `static_runner` the deployment says mounts the agents' storage, so what they type acts on the files they see as the agent's `root`; on a runner that sees only its own scratch storage they watch.
- A terminal's shell never starts with a response's CLI grant: the shell outlives the response, while the grant is only for that response's commands.
  `WorkerTerminals.open` builds its request with the grant unbound rather than removing the grant's names afterwards, so the call's other environment changes stay exactly as a shell call gets them.

## How typing is judged

`run_shell_command`'s approval rules judge the toolkit's typing through approval aliases: `terminal_open` as a shell no argument describes, `terminal_run` as its command, which it types only on an empty line at the shell's prompt, so the command is all it runs, and `terminal_send` as the command its final Enter runs, joined to what the terminal's line held before; keys after an Enter make the judgment unknown, because what that Enter runs may read them, as `read -n` does.
An alias's judge reads only the call's arguments, so the line comes from trusted preparation before approval (`ToolFunction.prepare_approval`, applied where the model loop plans a gated call), which adds the line's revision and text to the stored arguments that the approval card shows and the run receives.
The run types only while the line still has that revision and text, so keys approved for one line never land on another, a command approved before someone typed in the terminal, such as a `cd`, does not run after it, and a path that skips the preparation and claims a line it does not have is refused.
Without a line, as through the agent CLI, a script, or a scheduled call, which resume or run the call with the arguments it was sent with, the judge cannot tell what an Enter runs, so the rules ask whenever they may.

- The primary records every input it sends a terminal, an agent's and people's, one send at a time per terminal, in `mindroom.terminal.lines`; the line is what was typed since its last Enter or Ctrl-C.
  It sends all of a terminal's input on one connection, which it opens while anyone watches and closes with the last viewer, because the runner applies input from separate connections in no promised order, and a keystroke recorded before a judged call could otherwise reach the shell after it, onto the line that call was judged against.
  When the runner has ended the input connection, the next send leaves the line unknown before it checks the line it was judged against, since what the old connection carried last may not have arrived.
  Records are keyed by the worker, agent and workspace the shell stand-ins name, because preparation runs synchronously in the model loop and cannot look the worker up.
- The shell's prompt command prints an OSC 133 `D` mark with the last exit status and a prompt counter before each prompt, and the primary reads the marks from any stream attached to the terminal.
  The counter lets marks replayed with the scrollback count once, and the exit status is what `terminal_run` reports.
- After an Enter or a Ctrl-C the line is unknown until the shell's next prompt, because a running command or a continuation reads what is typed meanwhile, and Ctrl-C drops unread typing, so the shell may lose part of keys typed right after it, and keys it reads first can keep it from printing that prompt; typing before that prompt leaves it unknown there, and a Ctrl-C at that prompt empties it.
  The line counts the prompts the shell owes, one after each Enter or Ctrl-C at the prompt, so a prompt owed from before a command, such as the one after a Ctrl-C, never ends that command.
  `terminal_send` refuses keys after a Ctrl-C in the same send, and `terminal_run` types only once the shell prompts after it, which a person's keystroke landing just before can keep it from doing, so the agent sends another Ctrl-C.
- Keys that edit the line, recall history, complete, or move the cursor make it unknown, and so does a send that may not have arrived and a terminal the primary did not see open, such as after a primary restart.
- Limitations: rules judge text, so state a terminal keeps, such as aliases, functions, and variables typed earlier, is invisible to them, as with any shell command string; output a program prints, such as a file it shows, can carry forged prompt marks, which can end a command early in the line's record, so keys meant for that program are judged as shell text, or make the record ignore later prompts, so `terminal_run` refuses until the terminal is replaced; after a primary restart a replayed mark can make the line look like a prompt while a program that ignores Ctrl-C reads the keys.

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
