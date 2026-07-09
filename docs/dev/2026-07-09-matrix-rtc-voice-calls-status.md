# MatrixRTC Voice Calls — Living Status Doc

Last updated: 2026-07-09 (morning).
This is the single source of truth for the voice-call effort; update it with every meaningful change.

## Goal

MindRoom agents join Element Call (MatrixRTC) voice calls in their rooms and hold real spoken conversations through an OpenAI realtime speech-to-speech model (`gpt-realtime-2.1`), as the *same agent* as in text chat: same system prompt, same tools (approval rules enforced), with a transcript saved to the agent workspace and a daily-memory entry per call.
Primary target deployment: production `mindroom.chat` (Cinny at `chat.mindroom.chat`).
The lab (`mindroom.lab.mindroom.chat`, incus container on the `mindroom` host) is a separate deployment and gets its own RTC backend later — prod first.

## PRs (one per repo)

| Repo | PR | State | Contents |
|------|----|-------|----------|
| mindroom-ai/mindroom | [#1452](https://github.com/mindroom-ai/mindroom/pull/1452) | draft, branch `matrix-rtc-voice-calls` | `src/mindroom/matrix_rtc/` package: call manager/session, Element Call wire formats, lk-jwt exchange, frame-key rotation, livekit-agents media bridge (`matrix_calls` extra), in-call tools, transcripts + daily memory, PL0 power-level fix, `calls:` config, docs, ~50 tests + live smoke harness |
| mindroom-ai/mindroom-nio | [#5](https://github.com/mindroom-ai/mindroom-nio/pull/5) | draft, branch `surface-unknown-olm-events` | Surface unknown decrypted olm to-device events as `UnknownToDeviceEvent` (required to receive Element Call frame keys in encrypted rooms) |
| basnijholt/dotfiles | [#69](https://github.com/basnijholt/dotfiles/pull/69) | draft, branch `matrix-rtc-backend`, **already deployed to prod** | LiveKit SFU + lk-jwt-service on `hetzner-matrix`, Caddy path routing under `mindroom.chat/livekit/*`, `rtc_foci` in well-known, agenix `livekit-keys.age` |
| mindroom-ai/mindroom-tuwunel | none needed | — | Fork already implements the OpenID `request_token` + federation userinfo routes the stack needs; MSC4140 delayed events are missing (upstream [tuwunel#178](https://github.com/matrix-construct/tuwunel/issues/178)) but that only causes stale rosters after client crashes, not join failures |
| mindroom-ai/mindroom-cinny | none needed so far | — | Upstream Cinny ships Element Call (embedded widget); call UI activates via the homeserver `rtc_foci` well-known, which prod now advertises |

## Deployed production backend (mindroom.chat)

- `services.livekit` on `:7880`, ICE/TCP `:7881`, media UDP `50100-50200` (iptables verified open).
- `services.lk-jwt-service` on `:8090`.
- Caddy: `https://mindroom.chat/livekit/jwt/*` → lk-jwt, `wss://mindroom.chat/livekit/sfu/*` → LiveKit; `org.matrix.msc4143.rtc_foci` in `/.well-known/matrix/client`.
- LiveKit API secret: agenix `livekit-keys.age` (key name `mindroom`); Tuwunel registration token at `/run/agenix/registration-token` on the host (sudo).
- Deployed via `nixos-rebuild switch` from the dotfiles branch flake ref; all services active as of last check.

## Validation matrix

| Leg | Status | How |
|-----|--------|-----|
| Well-known discovery, OpenID → LiveKit JWT | ✅ live | `tests/manual/matrix_rtc_live_smoke.py` against prod (all stages PASS) |
| SFU signaling + WebRTC media relay | ✅ live | two Matrix-authed participants, bot subscribed to caller's audio track |
| Real `CallManager` joins a call, publishes membership | ✅ live | job-tmp `live_botcall_test.py`; found + fixed the PL0 bug (below) |
| E2EE frame-key exchange | ✅ real olm crypto | `tests/test_matrix_rtc_e2ee_roundtrip.py` (skips until nio#5 is pinned; PASS against patched nio) |
| In-call tools, same-agent prompt, transcripts, daily memory | ✅ unit tests | `tests/test_matrix_rtc_call_tools.py`, `..._transcript.py`, `..._call_manager.py` |
| gpt-realtime endpoint/model wiring | ✅ live probe | WS to `wss://api.openai.com/v1/realtime?model=gpt-realtime-2.1` reached session layer; previously failed only on the placeholder key |
| gpt-realtime agent actually speaking in a call | ⬜ unblocked, not yet run | real OpenAI key provided 2026-07-09 and saved to `~/.mindroom/.env` |
| Human joins call from prod Cinny | 🔶 in progress | see “Current investigation” |

## Bugs found by live testing (fixed)

1. **PL0 members could not publish call membership** — `org.matrix.msc3401.call.member` defaults to state PL 50; Element pins it to 0 in call rooms.
   Fixed in #1452: `create_room` seeds the override and `ensure_call_member_power_level` reconciles managed rooms when `calls.enabled`.
2. **Stock nio drops unknown decrypted olm events**, so encrypted frame keys never reach callbacks — fixed by nio#5 (validated via the real-olm round-trip test).

## Current investigation: prod Cinny “stuck on Joining” (2026-07-09)

Symptoms: voice room shows the ready state, but joining hangs; console shows only `/sync` traffic and “No membership changes detected”; **no request reaches lk-jwt-service** from the browser.
Ruled out so far:

- `rtc_foci` well-known: correct, and the deployed Cinny build (`898d0685`) contains the support.
- Firewall: 7881/tcp + 50100-50200/udp open (iptables).
- MSC4140 delayed events: not advertised by Tuwunel, but per the tuwunel#178 thread Element Call works on Tuwunel regardless (matrix-js-sdk falls back); it only causes ghost participants after crashes.

Next suspects (active): Cinny's embedded-widget path — `CallWidgetDriver` capability/approval handling and the element-call widget handshake (join dies before the SFU credential request, i.e. inside widget ↔ client signaling).
LiveKit DTLS-timeout warnings observed in server logs during earlier Python tests are probably teardown noise from short-lived test clients, but re-check once a browser join succeeds.

## Test assets

- Committed: `tests/manual/matrix_rtc_live_smoke.py` (register → well-known → OpenID → JWT → SFU connect, needs `REG_TOKEN`).
- Job scratch (`~/.claude/jobs/147b583c/tmp/`): `live_botcall_test.py` (real CallManager in a live call), `live_bridge_test.py` (RealtimeVoiceBridge + optional realtime agent), `e2ee_framekey_test.py` (real-olm round trip).
- Sandbox quirk: aiohttp/nio needs `SSL_CERT_FILE=$(python -c 'import certifi;print(certifi.where())')`; use `MATRIX_SSL_VERIFY=false` for a local bot run.

## Remaining work

- [ ] Fix the prod Cinny browser join (current investigation above).
- [ ] Run the full agent-speaking test with the real OpenAI key (bot joins, greets, answers, calls a tool; transcript + daily memory written).
- [ ] Deploy the mindroom branch to the backend serving `mindroom.chat` agents (`mindroom-chat` service on the LXC host) or merge + release, so real agents (not test harnesses) join calls.
- [ ] Merge + release mindroom-nio#5, bump the pin in mindroom (activates the E2EE round-trip test and encrypted-room hearing).
- [ ] Lab deployment (separate RTC backend on the `mindroom` incus host) once prod is done.
- [ ] Un-draft and merge the three PRs.
