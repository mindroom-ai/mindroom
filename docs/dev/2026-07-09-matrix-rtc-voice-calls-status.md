# MatrixRTC Voice Calls — Living Status Doc

Last updated: 2026-07-09 (afternoon).
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
| mindroom-ai/mindroom-cinny | [#96](https://github.com/mindroom-ai/mindroom-cinny/pull/96) | draft, branch `fix/sw-element-call-navigation-fallback` | Service worker fix: the Workbox app-shell navigation fallback hijacked the Element Call widget iframe (`/public/element-call/index.html`), which was the entire "stuck on Joining" bug; adds `/public/` to the denylist |

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
| gpt-realtime agent actually speaking in a call | ✅ live | `tests/manual/scratch/live_agent_speaking_test.py`: real CallManager + real key; agent greeted aloud, understood a spoken question, ran the real `multiply` calculator tool, spoke "The result is 345", transcript + daily memory written (all 6 legs PASS) |
| Human joins call from prod Cinny | ✅ live | Root cause was the fork's service worker hijacking the widget iframe (see below); fix deployed to prod (release `20260709-080409`, cinny#96) and validated headless with the SW active: full join to the SFU websocket, in-call UI rendered. Caveat: Cloudflare caches `/sw.js` (max-age 14400), so clients get the fixed SW when that TTL lapses (~17:25 UTC on 2026-07-09) |

## Bugs found by live testing (fixed)

1. **PL0 members could not publish call membership** — `org.matrix.msc3401.call.member` defaults to state PL 50; Element pins it to 0 in call rooms.
   Fixed in #1452: `create_room` seeds the override and `ensure_call_member_power_level` reconciles managed rooms when `calls.enabled`.
2. **Stock nio drops unknown decrypted olm events**, so encrypted frame keys never reach callbacks — fixed by nio#5 (validated via the real-olm round-trip test).
3. **In-call tools were exposed to the realtime model with empty parameter schemas** — agno toolkit functions only build their JSON schema in `process_entrypoint()`, which nothing on the call path invoked, so the model called tools with no arguments and had to guess from error strings.
   Fixed in `call_tools._wrap_agno_function` (now processes the entrypoint before reading `function.parameters`), pinned by a unit test.

## RESOLVED: prod Cinny “stuck on Joining” (2026-07-09)

Symptoms were: joining hangs, console shows only `/sync` traffic plus “No membership changes detected”, no request ever reaches lk-jwt-service, and stray `_tuwunel/oidc/jwks` CORS errors.
**Root cause: the fork's service worker.**
The Workbox `NavigationRoute` app-shell fallback in `src/sw.ts` answers every same-origin navigation not on its denylist with the precached Cinny `index.html`, and an iframe document load is a navigation.
The Element Call widget iframe navigates to `/public/element-call/index.html?widgetId=...`; the widget query params keep the precache route from matching and `/public/` was not on the denylist, so the iframe booted a nested Cinny app shell instead of Element Call.
The widget therefore never sent `contentLoaded` (Cinny: “Widget specified waitForIframeLoad=false but timed out waiting for contentLoaded event!”), and the nested shell's OIDC discovery produced the jwks CORS red herring.
Proof by A/B with headless Playwright against prod build `898d0685`: identical room/URL/build with service workers blocked completes the ENTIRE join — widget handshake, `get_openid`, `org.matrix.msc3401.call.member` publish, `POST /livekit/jwt/sfu/get`, and the SFU websocket `wss://mindroom.chat/livekit/sfu/rtc/v1`.
Fix: cinny#96 adds a base-path-aware `/public/` entry to the navigation fallback denylist.
Repro tricks worth keeping: seed the session via localStorage key `mindroom_multi_account_store` (password login is UI-disabled for mindroom.chat), create the room with `creation_content.type = org.matrix.msc3417.call` so the CallView prescreen renders, tap widget postMessage traffic with an init script, and A/B with Playwright `service_workers="block"`.
LiveKit DTLS-timeout warnings in earlier server logs were teardown noise from short-lived test clients, as suspected.

## Test assets

- Committed: `tests/manual/matrix_rtc_live_smoke.py` (register → well-known → OpenID → JWT → SFU connect, needs `REG_TOKEN`).
- Committed: `tests/manual/scratch/live_agent_speaking_test.py` (the full loop: synthetic caller speaks a TTS question into the SFU, real CallManager agent greets, answers, runs a real tool; checks audio energy, transcript, and daily memory; needs `MINDROOM_REG_TOKEN` + `OPENAI_API_KEY`).
- Job scratch (`~/.claude/jobs/147b583c/tmp/`): `live_botcall_test.py` (real CallManager in a live call), `live_bridge_test.py` (RealtimeVoiceBridge + optional realtime agent), `e2ee_framekey_test.py` (real-olm round trip), `cinny_join_repro.py` (headless Playwright browser-join repro against prod Cinny: seeds the session via localStorage, creates an `org.matrix.msc3417.call` room, clicks Join, taps console/network/postMessage; `BLOCK_SW=1` and `HOST_RULES` env knobs).
- Sandbox quirk: aiohttp/nio needs `SSL_CERT_FILE=$(python -c 'import certifi;print(certifi.where())')`; use `MATRIX_SSL_VERIFY=false` for a local bot run.

## Remaining work

- [x] Fix the prod Cinny browser join — root-caused (service worker app-shell fallback hijacked the widget iframe), fixed in cinny#96, deployed to prod, live-validated 2026-07-09.
- [ ] Merge cinny#96 into `dev` (prod currently runs the fix branch build, which is `dev` + that one commit).
- [x] Run the full agent-speaking test with the real OpenAI key (bot joins, greets, answers, calls a tool; transcript + daily memory written) — PASS 2026-07-09, see validation matrix.
- [x] Deploy the mindroom branch to the backend serving `mindroom.chat` agents — done 2026-07-09: merged `matrix-rtc-voice-calls` into the `mindroom` LXC host's `/srv/mindroom` main (merge commit `33f2ddcc6`, rollback branch `backup-pre-rtc-20260709`), added `calls: {enabled: true, agents: [openclaw]}` to `~/.mindroom-chat/config.yaml` (backup `.bak-20260709-voice-calls`), and added `--extra matrix_calls` to the shared uv wrapper in the host's dotfiles (`nixos-rebuild switch` applied; change also pushed to dotfiles branch `matrix-rtc-backend`, PR #69). The service's own `uv run` sync prunes manually-synced extras, so the wrapper flag is required. Deploy also caught a real bug (fixed on the PR branch, commit `33b92df46`): `matrix_calls_dependencies_available` crashed agent startup via `find_spec("livekit.rtc")` raising when livekit was absent. Verified: `mindroom-chat` healthy, openclaw bot up, livekit importable in the service venv, call-membership PL0 reconciled across 26 managed rooms.
- [ ] Merge + release mindroom-nio#5, bump the pin in mindroom (activates the E2EE round-trip test and encrypted-room hearing).
- [ ] Lab deployment (separate RTC backend on the `mindroom` incus host) once prod is done.
- [ ] Un-draft and merge the three PRs.
