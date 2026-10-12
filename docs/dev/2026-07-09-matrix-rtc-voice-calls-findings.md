# MatrixRTC Voice Calls: Findings

This record keeps what the July 2026 effort to let agents join Element Call (MatrixRTC) voice calls learned about Matrix, Element Call, LiveKit, the homeserver, the client, and providers, as of 2026-07-14.
Setup, configuration, and call behavior are on [Voice Calls](../voice-calls.md).

## Why

Agents should hold real spoken conversations in Element Call calls in their rooms as the same agent the user chats with in text, whichever speech backend runs the call.
The first target was production `mindroom.chat` with Cinny at `chat.mindroom.chat`.
The lab (`mindroom.lab.mindroom.chat`, an incus container on the `mindroom` host) is a separate deployment that needs its own RTC backend, planned after production.

## External constraints

- `org.matrix.msc3401.call.member` is a state event, so it defaults to power level 50 and power-level-0 members cannot publish call membership; Element pins it to 0 in call rooms, and MindRoom-managed rooms need the same override.
- Stock matrix-nio drops unknown decrypted Olm to-device events, so Element Call frame keys never reach callbacks in encrypted rooms; [mindroom-nio#5](https://github.com/mindroom-ai/mindroom-nio/pull/5), released in mindroom-nio 0.27.0, surfaces them as `UnknownToDeviceEvent`.
- The stack needs the homeserver's OpenID `request_token` and federation userinfo routes, which the Tuwunel fork already implements.
  Tuwunel's missing MSC4140 delayed events ([tuwunel#178](https://github.com/matrix-construct/tuwunel/issues/178)) only leave stale rosters after client crashes and do not cause join failures.
- `livekit-agents` 1.6.4 walks `AgentSession.input.audio.source` while logging its IO chain, so an audio input that duck-types `AudioInput` without a `source` property makes the agent join the SFU and leave immediately, before the realtime session starts ([#1485](https://github.com/mindroom-ai/mindroom/pull/1485)).
- A deployment whose service runs its own `uv run` sync prunes extras installed by a separate sync, so `--extra matrix_calls` must be part of the extras the service itself requests.

## Decisions

- Played text is synchronized with audio inside the agent instead of being published as LiveKit transcripts, because transcripts published through the SFU are unencrypted.

## Cinny "stuck on Joining" (resolved 2026-07-09)

Symptoms: the join hangs, the console shows only `/sync` traffic plus "No membership changes detected", no request ever reaches lk-jwt-service, and stray `_tuwunel/oidc/jwks` CORS errors appear.
Root cause: the mindroom-chat service worker's app-shell navigation fallback answered the Element Call widget iframe's navigation to `/public/element-call/index.html?widgetId=...` with the Cinny `index.html`, so the iframe booted a nested Cinny shell instead of Element Call ([mindroom-chat#96](https://github.com/mindroom-ai/mindroom-chat/pull/96) adds `/public/` to the fallback denylist).
The widget therefore never sent `contentLoaded` (Cinny logs "Widget specified waitForIframeLoad=false but timed out waiting for contentLoaded event!"), and the jwks CORS errors came from the nested shell's OIDC discovery and were a red herring.
LiveKit DTLS-timeout warnings in the server logs were teardown noise from short-lived test clients, not part of the bug.
Proof: a headless Playwright A/B against prod build `898d0685` with the identical room, URL, and build but service workers blocked completed the entire join: widget handshake, `get_openid`, `org.matrix.msc3401.call.member` publish, `POST /livekit/jwt/sfu/get`, and the SFU websocket `wss://mindroom.chat/livekit/sfu/rtc/v1`.
With the fix deployed (prod release `20260709-080409`), the same headless check with the service worker active completed the full join to the SFU websocket and rendered the in-call UI.
Real browsers got the fix only after Cloudflare's cached `/sw.js` refreshed, which happened within the hour.
Repro tricks: seed the session through the localStorage key `mindroom_multi_account_store` (password login is disabled in the UI for mindroom.chat), create the room with `creation_content.type = org.matrix.msc3417.call` so the CallView prescreen renders, tap widget postMessage traffic with an init script, and A/B with Playwright `service_workers="block"`.

## Evidence

- Against production MatrixRTC on mindroom.chat, `tests/manual/scratch/live_agent_speaking_test.py` passed all six legs with the realtime backend on 2026-07-09 and the cascaded backend on 2026-07-10: the agent greeted aloud, understood a spoken question, ran the real `multiply` tool, spoke the answer 345, and wrote the transcript and memory reference.
  The cascaded run also kept a natural sentence pause within one user turn.
  At the last documented deployment check, production still ran mindroom-nio 0.25.2, so encrypted-room hearing was not active there; unencrypted rooms worked fully.
- On 2026-07-09 a user created a Cinny Voice Room, invited Mind, and held a spoken conversation with it.
- `tests/manual/matrix_rtc_live_smoke.py` passed every stage against production: well-known discovery, OpenID, LiveKit JWT, and SFU signaling.
- In the deployed snapshot, the voice agent received Mind's real chat system prompt (51k characters, rendered identically to chat) and its 23 then-visible tools.

## Open questions

- `defaults.max_preload_chars` (50k) truncated about 37k characters of Mind's context files, USER.md among them.
  This affects chat too, but chat masks it with thread history, while a cold call session has none.
  Decide whether to raise the cap, harden the voice-call guidance against gpt-realtime's "can't reveal memories" reflexes, or both.
- As of 2026-07-14, hearing in encrypted rooms was proven only by `tests/test_matrix_rtc_e2ee_roundtrip.py` and had not been validated in a live call.
