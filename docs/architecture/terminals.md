---
icon: lucide/terminal
---

# Persistent Terminals

A persistent terminal is an interactive Bash in a pty that keeps running between calls, keeps its recent output, and lets viewers attach and detach.
The sandbox runner keeps terminals, and the primary reaches them through `WorkerTerminals` in `mindroom.terminal.client`.

## Decisions

- Terminals live in the sandbox runner process of the worker where the agent's shell commands run, not in the shell supervisor and not in the primary.
  The runner process lives as long as the worker, and a pty there inherits the worker's container, user, mounts, egress rules, and limits, so the worker stays the only isolation boundary.
  The per-request children that serve `/execute` exit with their request, so they cannot own a shell that outlives a call.
- A terminal opens from the request a shell call of the agent's shell toolkit would send, prepared by the same runner code as `/execute`, so its environment, workspace, `PATH`, and the call's own environment changes are the ones the agent's commands get.
  `tests/terminal/test_worker_terminals.py` compares the two environments in both runner execution modes.
- The shell is `bash --noprofile --norc -i`, which reads no startup files, as the Bash that runs a shell command string reads none, so opening a terminal runs nothing that worker code wrote into the workspace's `~/.bashrc` or `~/.profile`.
- Input and resize travel only on the viewer's websocket, so there is no second input path to keep consistent with it.
- Opening a terminal starts the agent's dedicated worker, as a shell call does; listing, attaching, and closing only look the worker up, so a stopped worker has no terminals and nothing starts one to answer.
  An attached viewer marks the worker used every minute, so the worker is not stopped as idle while someone watches it.
- The primary derives a terminal's owner from the request's worker key, routing agent, and workspace, so agents that share a worker or the shared runner never see each other's terminals, while requesters whose calls reach the same agent workspace share them.
  The runner takes the owner the primary sends, because only the primary holds the runner token.
- A session ID becomes a path on the runner, so the client accepts only the runner's own ID alphabet, and an ID such as `../../execute` cannot send the runner token to another runner endpoint.

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
