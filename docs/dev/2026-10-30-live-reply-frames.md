# Live reply frames

Issue: https://github.com/mindroom-ai/mindroom/issues/3438.
Repositories: this runtime, mindroom-tuwunel (the homeserver route and sync relay), mindroom-nio (payload encryption and the send call), and mindroom-chat (applying and rendering frames).

## Why

A streamed reply used to be a placeholder followed by full-content `m.replace` edits, starting every 0.5 s and ramping to every 5 s.
Each edit resent the whole message, so bytes grew with length times updates, and each one was journaled, stored, synced to every device, and handled by four homeserver workarounds for streamed edits.
Reasoning and finer progress the runtime already had never reached the user.

## Decisions

- Frames are custom ephemeral room events (MSC2477 send route, unstable prefix) relayed by our Tuwunel fork from an in-memory buffer, because the runtime already reaches the homeserver from behind NAT, clients already hold a `/sync` connection with resume, and the homeserver already decides room membership.
- Durable edits stay as checkpoints every `update_interval` plus semantic boundaries and the terminal edit, so every client converges on the same final message and recovery keeps reading only durable writes.
- A Stop, a stream error, or an interruption ends the reply below what frames showed, not below the last checkpoint: once frames stop, the stream records their text in the reply's records, and the note, the only write a committed Stop allows, carries it.
  A checkpoint edit after a committed Stop would break the reply lifecycle, and shorter intervals would bring back the edits frames remove.
- Frames carry the Markdown the next durable edit renders, not HTML: rendering 40 KB of Markdown with markdown-it-py and Pygments took 38 ms on the development host and produced 62 KB of HTML, which at ten frames a second would cost most of a core per long reply and hit the frame size limit early.
  Chat renders it with markdown-it configured like the server; code highlighting differs slightly until the terminal edit (Prism versus Pygments), which was accepted.
- Delivery is opt-in by sync filter: a custom ephemeral type reaches, and wakes, only a sync whose filter lists it in `room.ephemeral.types`.
  Without it every agent's own sync loop and every Element device in the room would wake at the frame rate for nothing.
  This departs from standard filter semantics (absent `types` means all types), so the fork advertises it separately as `io.mindroom.ephemeral_opt_in`, which the runtime requires.
- Encrypted rooms send `{"v": 1, "encrypted": <Megolm content>}` with the room's current outbound session, and frames do not count toward the session's 100-message rotation.
  Counting would rotate every ten seconds at ten frames a second, each rotation sharing a new key with every device.
  The trade-off is that one session covers more ciphertexts, including intermediate text and reasoning that need not equal any durable event; age rotation (seven days) and membership and device changes still rotate it, and the Megolm message index still advances.
- Every durable write of a reply that sent frames carries `io.mindroom.live_checkpoint: {sid, seq}`, the last frame it covers.
  Publisher ids start with zero-padded start milliseconds, so `(sid, seq)` orders live state across a restart.
  Chat shows the live state only while it is newer than the durable content's watermark, which removes both flicker back to an older edit and stale overlays without an end-of-stream frame.
  Progress edits never carry reasoning, so Chat keeps showing the live reasoning over a checkpoint that covers its frames.
- A frame too large to send (over 60000 bytes serialized) stops frames for that reply and forces a durable edit at once; durable edits then return to the ordinary cadence.
- Reasoning reaches only streamed agent replies, is never part of the answer or history, and the terminal edit keeps it as `io.mindroom.reasoning: {text, duration_ms}` (sidecar-only for large messages).
  Its duration runs from the start of each reasoning segment to the model's next output.
  OpenAI's Responses API streams a reasoning summary in parts, the first well after the model began reasoning, so its deltas carry when the reasoning item began; Anthropic, Gemini, and Ollama stream reasoning while the model produces it, so the first delta's arrival marks the start.
  Measured on Codex on 2026-11-01, the first part came about 5 s after reasoning began for GPT-6.1 Sol and GPT-6 Luna, but only at the end of 16 s of reasoning for GPT-6 Astra.
  The Responses API sends no summary unless the request asks for one, so Codex requests ask for `auto`, as the Codex CLI does; a reply on the OpenAI API shows reasoning only when its model sets `reasoning_summary` or `reasoning.summary`, and that route does not default it because an unverified organization's request then fails.
  Chat Completions carries no such start, so an OpenAI reasoning model reached that way, as through OpenRouter, still counts from the summary's arrival.
- A running tool call's latest output (what its tool reports as progress, such as a shell command's output tail) reaches only live frames, as an optional `progress` string on the call's tools entry, capped at its last 1000 characters and dropped when the call completes.
  Output changes many times a second and is noise once the result exists, so durable edits, the final message, and the stored tool trace never carry it; Element and other clients without frames see only the result.
  It is redacted like result previews, before it is cut to its latest characters, so a long credential keeps the label that identifies it and a script that prints a credential does not show it to the room; team replies do not forward members' or the leader's output.
  Redaction hides everything after a secret name that ends its line, such as `password:` with its value on the next line, as the call's result preview does, until newer output pushes that line out of the report.
  Masking only the shown part of a report was tried and rejected: each rule it needed to keep a value from showing without its label exposed another way to show one.
  A call keeps only its newest report and shows it at most ten times a second, the rate live frames go out, so a worker that floods reports costs the primary at most ten redactions a second per call.
  A call's output stays with that call: a delegated agent's tool output does not reach the parent's delegation entry, which shows the delegation's own activity, and only a job forwards its output to the call that started it and may be waiting for it.

## Rejected alternatives

- Direct SSE or WebSocket from the runtime: unreachable for local runtimes behind NAT, and native apps drop `localhost` origins; it would also need a second auth surface.
- Encrypted to-device frames: stored per device until delivered, with stale backlogs for offline devices.
- Cheaper edits (MSC4357 live-message markers, delta edits as timeline events): still stored events.
- MatrixRTC data channels: not deployed everywhere and too heavy per reply.
- HTML frames: see the rendering cost above.
- A separate live-only Megolm session: another sharing lifecycle for no gain over the count exemption.
- An end-of-live frame: the durable watermark already tells the client when the durable content caught up, after the client actually has it.
- Per-room aggregate rate limits and a global buffer byte cap on the homeserver: the per-sender rate limit, the per-room ring bounds, and the TTL sweep bound it; `/send` floods cost more and have no such limits.

## Wire contract

- Send: `PUT /_matrix/client/unstable/org.matrix.msc2477/rooms/{roomId}/ephemeral/io.mindroom.live/{sid}.{seq}`; the homeserver keeps at most 256 events, 1 MiB, and 30 s per room, allows 30 events per second per sender and room, and refuses `m.*` types, non-members, and bodies over 64 KiB.
- Sync: `{type, content, sender, origin_server_ts}` in `rooms.join.*.ephemeral.events`, never on initial sync, only after `since`; clients take the room from the enclosing sync room.
- Frame: `{"v": 1, "reply", "sid", "seq", "snapshot": {md, reasoning, tools, status, activity}}` or the same with `"delta"`, whose `md` and `reasoning` are splices `{at, del, ins, len}` in UTF-16 code units, `tools` is `{len, set: [[index, entry]]}`, and `status`/`activity` are new values.
  `tests/live_frames/fixtures/live-frames-v1.json` pins the format; mindroom-chat carries the same file.
- Cadence: a frame per 100 ms tick when something changed, one send in flight, a snapshot first, after a failed send, and every 5 s.

## Sub-agent frames

Each running sub-agent, a delegated child run (`run_subagent`, `continue_subagent`, `delegate`) or a Dynamic Workflow participant, is published as `io.mindroom.subagents` frames, so MindRoom Chat can show a live Sub-agents panel.
A run's frames have a reply frame's structure, so one delta format serves both: the run's transcript is projected into the live documents of a reply.
`tests/live_frames/fixtures/subagent-frames-v1.json` pins the format; mindroom-chat carries the same file.

- Send: the same route as reply frames, with `io.mindroom.subagents` as the event type and transaction ID `subagents.{sid}.{seq}`.
  Encrypted rooms send `{"v": 1, "encrypted": <Megolm content>}` with the event type bound to `io.mindroom.subagents`, in the room's current outbound session and outside its rotation count, as reply frames do.
- Sender: the account that answers the delegating turn in the room (the agent, or the team for a team turn), through its bot's current client at each send.
  A sub-agent has no account of its own, so clients should accept these frames only from a MindRoom agent on their homeserver, as for reply frames.
- Frame: `{"v": 1, "run", "sid", "seq", "meta", "snapshot"}` or the same with `"delta"` instead of `snapshot`.
  - `run` is the delegation ID, which names the run across its frames.
  - `sid` is a publisher ID like a reply's (zero-padded start milliseconds, a dash, 8 hex digits), `seq` starts at 0 within a `sid` and rises with each frame built, so a failed send leaves a gap; clients order frames by `(sid, seq)` and must not expect contiguous `seq`.
    A run keeps its `sid` through pauses and resumes; it gets a new one, with `seq` back at 0, only when its stream restarts after the room refused a frame.
  - `snapshot` and `delta` are exactly a reply's `LiveDocs` and live delta (`md` and `reasoning` as UTF-16 splices, `tools` as `{len, set}`, `status` and `activity` as new values).
    `reasoning` and `activity` are always empty.
- `meta` rides on every frame, so a client that missed the snapshot still knows the run's status.
  - `agent`: the sub-agent's name; `caller`: the agent or sub-agent that delegated to it; `root`: the agent in the room whose turn started the chain (equal to `caller` for a direct child); `parent`: the delegation ID of the sub-agent that delegated to it, or `null`.
  - `thread_id`: the Matrix thread the chain belongs to, or `null`; `source`: `"delegate"` or `"workflow"`.
  - `status`: one of the statuses below; `started_at`: Unix seconds; `ended_at`: Unix seconds once the run ended, else `null`.
  - A snapshot's `meta` also carries `task` (at most 1,000 characters) and, once the status is terminal, `result` (at most 1,000 characters).
    Deltas carry neither, so a client keeps them from the snapshot.
- Documents: text items are concatenated as the Markdown of `md`.
  Each tool call adds the marker line a reply shows for it (`🔧 \`name\` [n] ⏳`, with the hourglass replaced by the finished form once the call completes), numbered from 1 by its entry in `tools`, in order of appearance.
  So the n-th marker of `md` belongs to `tools[n-1]`, and a client renders the documents with the reply renderer.
  Text on both sides of a tool call is separated by the marker's blank lines; text items that meet because a cap removed what was between them are joined by a blank line.
  A completed call's entry has `type: "tool_call_completed"`, a running call's `tool_call_started`; a paused call that resumes keeps its row.
- `docs.status` is `"streaming"` while `meta.status` is `running` or `awaiting_approval`, and `"pending"` otherwise, so a client takes the run's state from `meta.status`.
- Statuses: `running` from the first frame, and again after an approval or a pause; `awaiting_approval` while the child waits for the person to decide its gated calls, as a background job child does through its job's cards; `paused` when the child stopped on an approval its parent's reply owns, until it resumes as `running`; `completed`, `failed`, `cancelled`, and `denied` end the run.
  An attempt that settled no outcome ends as `cancelled` when it was cancelled and as `failed` otherwise, with the reason as `result`.
  A run's last frame is a snapshot of its end, and `ended_at` is set from then on.
- Caps: the transcript's text is cut to its newest 5,000 characters once it passes 6,000, so the frames after a cut are appends again until the next cut; at most the 20 newest tool rows; `args_preview` and `result_preview` at 300 characters, with `truncated: true` on a row whose preview was cut.
  Text and previews are redacted before any cut, so a secret is masked whole.
  How many older items the caps removed is not part of the frame.
- Size: a frame must fit the 60,000-byte limit as nio sends it (compact JSON with ASCII escapes, so a character outside ASCII takes 6 bytes; in an encrypted room the Megolm ciphertext, padding, base64, and envelope are counted as well).
  A snapshot that does not fit drops the tool previews first, then cuts the oldest text from the front by the least that fits.
  A fitted frame is always a snapshot, and later deltas keep its fit, so they stay appends.
  A delta that does not fit is replaced by a fitted snapshot.
- A frame never carries an empty delta: when only `meta` changed, such as a status, the frame is a snapshot.
- 🔒 Previews: `args_preview` and `result_preview` appear only when the turn in the room that started the chain shows its tool calls (`show_tool_calls`); nested sub-agents follow their root's turn.
  Otherwise a row carries only `type` and `tool_name`, so the room sees for a sub-agent what it sees for the agent that delegated.
- Budget: one publisher per (sending account, room) sends all of that account's runs in that room, one send in flight and at least 125 ms between sends, so at most 8 frames a second, which leaves at least 12 of the homeserver's 30 events a second per sender and room for the account's own reply frames.
  Runs with something to send are served in turn.
  A run's changes are coalesced to at most one per 250 ms before they reach the publisher, and a status change goes out at once.
  A run sends a snapshot first, after a failed send, after the caps cut its transcript, when only `meta` changed, every 10 s while `running` or `awaiting_approval`, at its end, and when a delta would not fit; otherwise a delta from the last frame that landed.
  A `paused` run sends its snapshot and then nothing until it resumes, so a client must not time a paused run out by silence; a `running` or `awaiting_approval` run sends at least every 10 s while its runtime is alive.
  A run is forgotten after the frame of its end.
- A refused frame (`M_TOO_LARGE`, `M_UNRECOGNIZED`, `M_FORBIDDEN`, `M_UNKNOWN`, `M_NOT_FOUND`, or a bare 4xx) stops that run's frames until it resumes from a pause or ends, and its end then gets one more attempt.
  Other failures retry with a snapshot after a one second wait.
- When no frames are sent: the delegating turn has no Matrix room (the OpenAI-compatible API, scheduled or API runs) or runs outside a managed runtime; `defaults.streaming.live_frames` is off when the run starts (switching it off later does not stop a run already publishing); the homeserver does not list `io.mindroom.ephemeral_opt_in`; the sending account has no client for a whole retry.
  Frames are best effort: a failure building or sending one stops frames and never fails the run.
- Restart: frames are memory only and an interrupted child is not rerun.
  After a crash no end is sent, because recovery runs in a new process that has no feed for the run.
  On a graceful stop the old process may still publish the run as `failed` or `cancelled`, and that frame may or may not land.
  A client should drop a `running` or `awaiting_approval` run that goes silent, and keep a `paused` or ended run for as long as it is useful.

## Limitations

- Clients without frame support (Element, bridges, older Chat builds) see the reply in steps of `update_interval` instead of the former fast start.
- Federated users and sliding-sync clients get no frames.
- A crash loses what frames showed beyond the last checkpoint; Chat falls back to the checkpoint after 12 s without frames.
- A reply split into several messages (`large_message_strategy: split`) keeps no reasoning, which would otherwise repeat on every segment, and a final edit carries reasoning only in `m.new_content`, cut to the space the answer leaves, so reasoning never turns an ordinary reply into a sidecar or segments for Element and bridges.
- Teams stream body and tool calls but no reasoning, and a reply that paused for an approval shows reasoning only until the pause and keeps none on its final message: the continuation that finishes it streams through the caller-owned progress path, which carries no reasoning.
- Every opted-in device wakes once per frame while a reply streams in any of its rooms.

## Measurements

Measured on 2026-10-31 on the shared aarch64 development host (load average 25 to 50) with the fork's release build on loopback, three opted-in sync clients, and one client that did not opt in.

| Updates | Send to sync p50 | p95 | Syncs per second per opted-in device | Wakes of the other client | Homeserver CPU |
|---|---|---|---|---|---|
| Frames at 10 Hz | 2.4 ms | 5.6 ms | 9.1 | 0 | 2.5% |
| Frames at 20 Hz | 2.3 ms | 5.7 ms | 18.2 | 0 | 5.0% |
| Frames at 50 Hz (rate limit at 30) | 2.2 ms | 5.7 ms | 27.4 | 0 | 8.3% |
| Edits at 2 Hz | 6.4 ms | 12.2 ms | 1.8 | 1.8 per second | 1.0% |
| Edits at 10 Hz | 4.5 ms | 10.2 ms | 9.1 | 9.1 per second | 5.3% |

One synthetic-model reply of about 2,600 characters over 9 seconds in an encrypted room, end to end through the runtime:

| | Durable edits | Edit bytes | Frame bytes | Wakes of a client without opt-in |
|---|---|---|---|---|
| Frames on | 3 | 27 KB | 60 KB in 80 frames at 9.9 per second | 5 |
| Frames off | 23 | 202 KB | none | 26 |

Most of an encrypted delta frame's roughly 750 bytes is the Megolm envelope; a similar reply's 84 frames in an unencrypted room took 19 KB.

Frames used the reply's own Megolm session, and one session carried more than 170 frames across two replies without rotating.
MindRoom Chat in a browser showed 78 visible updates of such a reply in its thread view, decrypting every frame through its Rust crypto; the room timeline's thread preview still follows the durable edits.
Computing one frame's documents for a 40 KB reply took 0.34 ms in the runtime, against 38 ms for rendering its HTML.
