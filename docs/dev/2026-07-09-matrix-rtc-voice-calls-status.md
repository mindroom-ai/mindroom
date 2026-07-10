# MatrixRTC Voice Calls — Living Status Doc

Last updated: 2026-07-10.
This is the single source of truth for the voice-call effort; update it with every meaningful change.

## Goal

MindRoom agents join Element Call (MatrixRTC) voice calls in their rooms and hold real spoken conversations through either OpenAI realtime speech-to-speech or a provider-independent cascaded STT, normal-agent, and TTS pipeline.
Both backends use the same agent as text chat, with the same resolved prompt and effective tools, including knowledge, skills, and hooks, while incrementally persisting a recallable transcript.
Primary target deployment: production `mindroom.chat` (Cinny at `chat.mindroom.chat`).
The lab (`mindroom.lab.mindroom.chat`, incus container on the `mindroom` host) is a separate deployment and gets its own RTC backend later — prod first.

## Related PRs

| Repo | PR | State | Contents |
|------|----|-------|----------|
| mindroom-ai/mindroom | [#1452](https://github.com/mindroom-ai/mindroom/pull/1452) | **merged** 2026-07-10 | `src/mindroom/matrix_rtc/` package: call manager/session, Element Call wire formats, lk-jwt exchange, frame-key rotation, livekit-agents media bridge (`matrix_calls` extra), in-call tools, transcripts + memory references, PL0 power-level fix, `calls:` config, docs, tests + live smoke harness |
| mindroom-ai/mindroom | [#1485](https://github.com/mindroom-ai/mindroom/pull/1485) | **merged** 2026-07-10 | Complete the LiveKit `AudioInput` startup contract, add media-plane diagnostics and regression coverage, and extend the consolidated live speaking harness with an existing-account mode |
| mindroom-ai/mindroom | [#1484](https://github.com/mindroom-ai/mindroom/pull/1484) | **open** | Add provider-independent cascaded calls while preserving the existing OpenAI Realtime backend |
| mindroom-ai/mindroom-nio | [#5](https://github.com/mindroom-ai/mindroom-nio/pull/5) | **merged**, released as [0.27.0](https://github.com/mindroom-ai/mindroom-nio/releases/tag/0.27.0) on PyPI; pin bumped in #1452 | Surface unknown decrypted olm to-device events as `UnknownToDeviceEvent` (required to receive Element Call frame keys in encrypted rooms) |
| basnijholt/dotfiles | [#69](https://github.com/basnijholt/dotfiles/pull/69) | **merged**, deployed to prod + mindroom LXC | LiveKit SFU + lk-jwt-service on `hetzner-matrix`, Caddy path routing under `mindroom.chat/livekit/*`, `rtc_foci` in well-known, agenix `livekit-keys.age`, `--extra matrix_calls` in the LXC uv wrapper |
| mindroom-ai/mindroom-tuwunel | none needed | — | Fork already implements the OpenID `request_token` + federation userinfo routes the stack needs; MSC4140 delayed events are missing (upstream [tuwunel#178](https://github.com/matrix-construct/tuwunel/issues/178)) but that only causes stale rosters after client crashes, not join failures |
| mindroom-ai/mindroom-cinny | [#96](https://github.com/mindroom-ai/mindroom-cinny/pull/96) | **merged** (deployed to prod earlier from the branch) | Service worker fix: the Workbox app-shell navigation fallback hijacked the Element Call widget iframe (`/public/element-call/index.html`), which was the entire "stuck on Joining" bug; adds `/public/` to the denylist |

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
| SFU signaling + WebRTC media relay | ✅ live | one Matrix-authenticated caller plus the bot, with the bot subscribed to the caller's audio track |
| Real `CallManager` joins a call, publishes membership | ✅ live | job-tmp `live_botcall_test.py`; found + fixed the PL0 bug (below) |
| E2EE frame-key exchange | ✅ real olm crypto | `tests/test_matrix_rtc_e2ee_roundtrip.py` — active since the nio 0.27 pin bump (no longer skipped), passes in the normal suite |
| In-call tools, same-agent prompt, transcripts, memory references | ✅ unit tests | The sole caller is the real Matrix requester; effective Agno tools include knowledge and skills; approval/interactive/external flows stay hidden; covered by `tests/test_matrix_rtc_call_tools.py`, `..._transcript.py`, `..._call_manager.py` |
| Cascaded STT → normal agent → TTS | ✅ live | `tests/manual/scratch/live_agent_speaking_test.py` against production MatrixRTC on 2026-07-10: agent greeted aloud, kept a natural sentence pause within one user turn, ran the real `multiply` tool, spoke "15 times 23 is 345", and wrote the transcript plus memory reference (all 6 legs PASS) |
| gpt-realtime endpoint/model wiring | ✅ live probe | WS to `wss://api.openai.com/v1/realtime?model=gpt-realtime-2.1` reached session layer; previously failed only on the placeholder key |
| gpt-realtime agent actually speaking in a call | ✅ live | `tests/manual/scratch/live_agent_speaking_test.py`: real CallManager + real key; agent greeted aloud, understood a spoken question, ran the real `multiply` calculator tool, spoke "The result is 345", transcript + memory reference written (all 6 legs PASS) |
| Roster-filtered audio input satisfies the current LiveKit AgentSession contract | ✅ regression test in #1485; live redeploy not recorded here | `AgentSession.start()` can traverse the wrapper's terminal `source = None` property instead of aborting after the SFU connection |
| Human joins call from prod Cinny | ✅ live | Root cause was the fork's service worker hijacking the widget iframe (see below); fix deployed to prod (release `20260709-080409`, cinny#96) and validated headless with the SW active: full join to the SFU websocket, in-call UI rendered. Cloudflare's cached `/sw.js` refreshed the same hour, so real browsers have the fix |
| **Real agent (Mind/openclaw) speaks with the user in a prod call** | ✅ user-validated | 2026-07-09: user created a Cinny Voice Room, invited Mind, Mind joined and held a spoken conversation (after fixing the placeholder `OPENAI_API_KEY` in `~/.mindroom-chat/.env` — the join succeeded but the realtime session got `invalid_api_key` until the real key was installed and the service restarted) |

## Bugs found by live testing (fixed)

1. **PL0 members could not publish call membership** — `org.matrix.msc3401.call.member` defaults to state PL 50; Element pins it to 0 in call rooms.
   Fixed in #1452: room creation and the existing managed-room power-level reconciliation share the same required event overrides.
2. **Stock nio drops unknown decrypted olm events**, so encrypted frame keys never reach callbacks — fixed by nio#5 (validated via the real-olm round-trip test).
3. **In-call tools were exposed to the realtime model with empty parameter schemas** — agno toolkit functions only build their JSON schema in `process_entrypoint()`, which nothing on the call path invoked, so the model called tools with no arguments and had to guess from error strings.
   Fixed in `call_tools._wrap_agno_function` (now processes the entrypoint before reading `function.parameters`), pinned by a unit test.
   Calls use the sole caller's real Matrix requester identity and configured room-scoped context; tools requiring approval or interactive execution are omitted because voice has no approval UI.
4. **Enabling calls without the `matrix_calls` extra crashed the whole agent at startup** — `find_spec("livekit.rtc")` raises `ModuleNotFoundError` (instead of returning `None`) when the parent `livekit` package is missing, so `matrix_calls_dependencies_available()` blew up `AgentBot.start()` instead of logging the dependencies-missing warning. Found during the first real deployment; fixed in `voice_agent.py` (commit `33b92df46`), pinned by a unit test.
5. **PR review found lifecycle and credential-transport gaps** — the follow-up publishes membership before the first E2EE key, tears down partial SFU connects, accepts only the operator-configured or locally discovered focus, and sends manual-harness Matrix access tokens only in authorization headers.
6. **PR review found requester, E2EE provenance, pending-key, and realtime-close gaps** — the follow-up limits managed calls to one distinct human user and uses that user's real requester identity, derives the effective Agno prompt/tool surface, accepts frame keys only from device-authenticated Olm events, retains early keys until membership catches up, and cleans up or retries unexpected realtime-session termination.
7. **The merged voice agent joined the SFU and immediately left before starting the realtime session** — `livekit-agents` 1.6.4 walks `AgentSession.input.audio.source` while logging its IO chain, but `_AuthorizedParticipantAudioInput` duck-typed the interface without a `source` property.
   Fixed in #1485 by exposing the terminal `source = None` property and comparing the wrapper with every public `AudioInput` attribute in a regression test.
   The same PR adds structured `call_output_permissions_applied`, `call_audio_stream_added`, `call_audio_first_frame`, and `call_media_snapshot` events so permissions, subscription, actual inbound media, publications, mute state, and the authorized roster can be distinguished during diagnosis.
8. **Fresh-head review and the cascaded live test found lifecycle and turn-fidelity gaps** — the follow-up closes owned STT/TTS clients, validates component endpoints, preserves call tool restrictions through delegation and realtime mode, synchronizes played text internally without publishing unencrypted SFU transcripts, keeps error and paused statuses in canonical replay, records completed tools even when generation is cancelled, uses safer endpointing for sentence pauses, and sends only the newest finalized user turn to the agent.

## Deployed agent backend (mindroom-chat on the `mindroom` LXC)

- `/srv/mindroom` main = host-local main + merge of `matrix-rtc-voice-calls` (merge `33f2ddcc6` + crash fix `33b92df46`; rollback branch `backup-pre-rtc-20260709`, deploy branch `rtc-deploy`).
- `~/.mindroom-chat/config.yaml`: `calls: {enabled: true, agents: [openclaw]}` (backup `.bak-20260709-voice-calls`); Mind ("openclaw") is the calls agent.
- Real `OPENAI_API_KEY` in `~/.mindroom-chat/.env` (was the old placeholder; backup `.env.bak-20260709-openai-key`).
- `--extra matrix_calls` in the shared uv wrapper (dotfiles, merged via #69): the service's own `uv run` sync prunes manually-synced extras, so the extra must be part of the requested set.
- Call-membership PL0 reconciled across all 26 managed rooms at boot.
- **Lag at the last documented deployment check**: the host snapshot predates the nio 0.27 pin bump merged in #1452, so it still runs mindroom-nio 0.25.2 — encrypted-room hearing is NOT active there until the next deploy (unencrypted rooms, including Cinny Voice Rooms created with encryption off, work fully).
- Prompt parity finding from the historical deployed snapshot: the voice agent got Mind's real chat system prompt (51k chars, rendered identically) plus its then-visible 23 tools, but `defaults.max_preload_chars` (50k) truncated ~37k chars of Mind's context (USER.md among the casualties) — this affects chat too; chat masks it with thread history, a cold call session doesn't.
  Merged #1452 derives the effective Agno tool surface and filters functions that require text-only interaction.
  Open decision: raise the cap and/or strengthen the voice addendum against gpt-realtime's privacy-caginess.

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
- Committed: `tests/manual/scratch/live_agent_speaking_test.py` (the full loop for either backend: a synthetic caller speaks a TTS question into the SFU, the real CallManager agent greets, answers, and runs a real tool; checks audio energy, transcript, and memory reference).
- The harness accepts exactly one account mode: set `MINDROOM_REG_TOKEN` to register throwaway users, or set all six of `CALLER_USER_ID`, `CALLER_DEVICE_ID`, `CALLER_TOKEN`, `BOT_USER_ID`, `BOT_DEVICE_ID`, and `BOT_TOKEN` to reuse existing accounts; `OPENAI_API_KEY` is required in both modes.
- Existing-account mode uses a private test room, clears the caller's call membership, and makes both accounts leave and forget the room during cleanup.
- Job scratch (`~/.claude/jobs/147b583c/tmp/`): `live_botcall_test.py` (real CallManager in a live call), `live_bridge_test.py` (RealtimeVoiceBridge + optional realtime agent), `e2ee_framekey_test.py` (real-olm round trip), `cinny_join_repro.py` (headless Playwright browser-join repro against prod Cinny: seeds the session via localStorage, creates an `org.matrix.msc3417.call` room, clicks Join, taps console/network/postMessage; `BLOCK_SW=1` and `HOST_RULES` env knobs).
- Sandbox quirk: aiohttp/nio needs `SSL_CERT_FILE=$(python -c 'import certifi;print(certifi.where())')`; use `MATRIX_SSL_VERIFY=false` for a local bot run.

## Remaining work

- [x] Fix the prod Cinny browser join — root-caused (service worker app-shell fallback hijacked the widget iframe), fixed in cinny#96, deployed to prod, live-validated 2026-07-09.
- [x] Merge cinny#96 into `dev` — merged 2026-07-09 (with the review follow-up: `?` accepted as a boundary in the denylist regex). Prod still serves the earlier branch build, which is behaviorally identical for the real bug; the next regular Cinny deploy picks up `dev`.
- [x] Run the full agent-speaking test with the real OpenAI key (bot joins, greets, answers, calls a tool; transcript + memory reference written) — PASS 2026-07-09, see validation matrix.
- [x] Deploy the mindroom branch to the backend serving `mindroom.chat` agents — done 2026-07-09: merged `matrix-rtc-voice-calls` into the `mindroom` LXC host's `/srv/mindroom` main (merge commit `33f2ddcc6`, rollback branch `backup-pre-rtc-20260709`), added `calls: {enabled: true, agents: [openclaw]}` to `~/.mindroom-chat/config.yaml` (backup `.bak-20260709-voice-calls`), and added `--extra matrix_calls` to the shared uv wrapper in the host's dotfiles (`nixos-rebuild switch` applied; change also pushed to dotfiles branch `matrix-rtc-backend`, PR #69). The service's own `uv run` sync prunes manually-synced extras, so the wrapper flag is required. Deploy also caught a real bug (fixed on the PR branch, commit `33b92df46`): `matrix_calls_dependencies_available` crashed agent startup via `find_spec("livekit.rtc")` raising when livekit was absent. Verified: `mindroom-chat` healthy, openclaw bot up, livekit importable in the service venv, call-membership PL0 reconciled across 26 managed rooms.
- [x] Merge + release mindroom-nio#5, bump the pin in mindroom — done 2026-07-09: mindroom-nio 0.27.0 on PyPI, pin `>=0.27.0` in #1452, the real-olm E2EE round-trip test now runs (40/40 matrix_rtc tests pass).
- [ ] Redeploy `mindroom-chat` from current main to pick up mindroom-nio 0.27.0 and the AgentSession startup fix, then live-validate encrypted-room hearing and voice startup.
- [ ] Decide on the Mind prompt-truncation follow-ups (raise `defaults.max_preload_chars` beyond 50k so USER.md/memory survive; optionally harden the voice-call addendum against gpt-realtime's "can't reveal memories" reflexes).
- [ ] Lab deployment (separate RTC backend on the `mindroom` incus host) once prod is done.
- [ ] Merge the provider-independent cascaded voice follow-up after its fresh-head review loop approves it.
