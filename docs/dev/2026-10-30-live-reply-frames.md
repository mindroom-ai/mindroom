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
  Redaction fails closed on a line it cannot delimit, such as `password:` with its value on the next line, so the output shows `[redaction failed]`, as the call's result preview does, until newer output pushes that line out of the report.
  Masking only the shown part when that happens was tried and rejected: each rule it needed to keep a value from showing without its label exposed another way to show one.
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

## Limitations

- Clients without frame support (Element, bridges, older Chat builds) see the reply in steps of `update_interval` instead of the former fast start.
- Federated users and sliding-sync clients get no frames.
- A crash loses what frames showed beyond the last checkpoint; Chat falls back to the checkpoint after 12 s without frames.
- A Stop or a stream error ends the reply below its last checkpoint, so up to one `update_interval` of text Chat showed live disappears above the note; writing a checkpoint after a committed Stop would break the reply lifecycle, and shorter intervals bring back the edits frames remove.
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
