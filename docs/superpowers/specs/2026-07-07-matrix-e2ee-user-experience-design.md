# Matrix E2EE Full-Flow User Experience Design

Status: draft for review.
Date: 2026-07-07.
Related: PR #1423 (encrypted delivery fix + decrypt-failure visibility), live E2EE test session of 2026-07-07.

## Context

Live testing against a local Synapse showed that the encrypted transport already works end to end: agents decrypt inbound text and media, reply encrypted in threads with streaming edits, survive restarts with the same device, and re-fetch and decrypt thread history from the homeserver.
PR #1423 fixed the two blockers found (sends failed on device trust by default; undecryptable events were silently dropped) and removed the unusable `matrix_delivery.ignore_unverified_devices` knob.
What does not exist yet is a designed product experience: how encryption gets enabled, what users see in their Matrix client, how trust is established, and how failures surface and recover.

## The client stack: mindroom-cinny first, and E2EE is already the default path

The primary MindRoom client is the `mindroom-cinny` fork (matrix-js-sdk 41.7 with `initRustCrypto`, the same Rust crypto engine Element uses), served at chat.mindroom.chat and chat.lab.mindroom.chat.
Its `CreateChat` flow defaults new DMs to encrypted (`createRoom.defaultEncryption ?? true`), and neither shipped config (`config.json`, `config.mindroom.json`) overrides that — so every new chat a user starts with an agent from the hosted client is an encrypted room today.
E2EE is therefore not a future opt-in; it is the live default path for the flagship "chat with your agent" flow, which is why the transport fixes in PR #1423 mattered immediately.
Owning the client is also a design lever Element never gives us: the fork can ship agent-aware crypto UX, and `createRoom.defaultEncryption: false` in the deployed config exists as an emergency valve (config-only, no rebuild) if an E2EE regression ever needs to be routed around in production.

## Why cross-signing is still the backbone: MSC4153

MSC4153 ("exclude non-cross-signed devices", formerly "invisible cryptography") was merged into the Matrix spec in October 2025, with the spec PR merged in February 2026.
It says clients SHOULD stop sharing room keys with devices that are not cross-signed; Element Web ships it behind lab flags, and the implementation lives in the same matrix-js-sdk Rust crypto stack that mindroom-cinny uses.
When the js-sdk defaults tighten (or the fork bumps the dependency), non-cross-signed MindRoom bot devices stop receiving room keys and every encrypted room breaks on the sender's side, where the backend cannot fix it.
Owning the client softens the timeline (the fork controls when it adopts stricter defaults) but does not remove the pressure: users can reach agents from Element and other clients too, and pinning the fork to old crypto defaults indefinitely is not a strategy.
Cross-signing bootstrap for bot devices is therefore the backbone of this design; it is also the prerequisite for any "verify this agent" UX in the fork, because without bot cross-signing keys there is nothing for the user's client to sign.

## Goals

1. A user who starts a chat with an agent from mindroom-cinny (encrypted by default today) gets a working, warning-free conversation; the same conversation works from Element and other clients.
2. An operator can turn on encryption for managed rooms through config, chat command, or dashboard, with the irreversibility of Matrix encryption made explicit.
3. When decryption fails, the user gets actionable feedback in the room and the operator gets diagnostics, instead of silence.
4. Bot devices remain functional as the js-sdk Rust crypto stack adopts MSC4153 defaults (cross-signed device identity).
5. Store loss and restarts degrade gracefully and recover automatically where the protocol allows it.
6. Users keep access to their encrypted agent history across their own device changes (user-side key backup guidance in the fork).

## Non-goals

- Interactive SAS/emoji verification of bots by users: once bots are cross-signed, per-user verification adds ceremony without meaningful trust (the bot would auto-accept), and MSC4153-era clients key off cross-signing, not per-user SAS.
- Cross-user key forwarding (honoring key requests from other users): refusing these is correct client behavior and a deliberate security boundary.
- Encrypting MindRoom's local state at rest: the event cache and session DBs intentionally store plaintext locally so agents keep context; E2EE here protects transport and homeserver storage.

## UX flows being designed

### Flow A: user starts an encrypted chat with an agent (mindroom-cinny default path)

Today: works after PR #1423.
mindroom-cinny does not paint per-message shields for other users' devices, so the fork experience is already visually quiet; Element users see the bot's device as unverified, and MSC4153-flag users cannot talk to the bot at all.
Target: the bot account presents a cross-signed device; the fork can then show a verified-agent affordance, Element shows no warnings, and exclude-insecure-devices clients keep working.
The user's own key continuity is part of this flow: mindroom-cinny already has key-backup UI (`BackupRestore`, `useKeyBackup`) and an own-device verification nag (`UnverifiedTab`); hosted onboarding should steer users into key backup so a new login does not orphan their encrypted agent history.

### Flow B: operator encrypts a managed room

Today: impossible — managed rooms are always created unencrypted, no config exists, and members lack the power level to enable encryption themselves.
Target: three equivalent entry points that all route through the same room-provisioning seam:

```yaml
rooms:
  private_planning:
    encrypted: true          # per-room opt-in

matrix_room_access:
  encrypt_managed_rooms: true  # global default for newly created managed rooms
```

- Config: `encrypted: true` on a room entry (and a global default) causes room creation with `m.room.encryption` (`m.megolm.v1.aes-sha2`) in `initial_state`, and hot-reload reconciliation enables it on existing rooms.
- Chat command: `!encrypt` in a managed room (authorized users only) asks for confirmation via the existing config-confirmation reaction flow, then the bot sends the state event.
- Dashboard: room settings show an encryption badge and an enable toggle with the same confirmation copy.

Enabling encryption on a Matrix room is irreversible; every entry point states this before acting, and reconciliation never attempts to remove encryption.

### Flow C: decryption failure feedback

Today: warning log plus an automatic room-key request (shipped in PR #1423), but the user still sees an agent that ignores them.
Target, layered on the existing `MegolmEvent` handler:

- In rooms where exactly one MindRoom bot is present, or in any room for the router only, the affected bot posts one rate-limited notice per (room, megolm session): "I could not decrypt your last message. I have requested the key; if this persists, send a new message."
  Single-responder election (router when present, otherwise the lone bot) prevents notice storms in multi-agent rooms.
- A `!e2ee` command reports the bot's device ID, cross-signing status, session counts, and recent decrypt-failure/key-request stats for the room, giving operators a diagnostic surface inside chat.
- The API health endpoint exposes counters (`e2ee_decrypt_failures`, `e2ee_key_requests_sent`) so dashboards and alerts can see systemic problems.

### Flow D: restarts, migrations, store loss

Today: restarts restore the same device and olm store (verified live).
But if `encryption_keys/` is lost while `matrix_state.yaml` survives, the bot restores the same device ID with fresh olm keys — the homeserver rejects or clients distrust the identity-key change, and the account is wedged.
Target: at startup, if the olm store for a persisted device is missing or fails integrity checks, the bot logs a prominent warning, performs a fresh password login (new device), bootstraps or re-signs with its cross-signing identity, and continues.
Old messages that only lived in the lost store stay undecryptable (protocol reality), but the durable event cache preserves the agent's conversational context, and the new device is immediately trusted via cross-signing.

## Design decisions

### D1: bots always encrypt to all devices (shipped)

Bots have no interactive verification, so outgoing sends always pass `ignore_unverified_devices=True`; there is no configuration.
A trust policy only returns if a real bot-side verification mechanism ever exists, which this design does not require.

### D2: cross-signing bootstrap in mindroom-nio (the critical path)

Upstream matrix-nio has no cross-signing support; this is a fork feature in `mindroom-nio`, consistent with prior fork additions (vodozemac backend, MSC4186).
Mechanism, all standard spec endpoints:

1. On first login for an account, generate master and self-signing ed25519 keys (a user-signing key is unnecessary for bots, which never verify other users).
   Cross-signing keys are plain ed25519 over canonical JSON, independent of olm state, so implementation does not depend on vodozemac exposing new primitives.
2. Upload them via `POST /keys/device_signing/upload`, completing user-interactive auth with the bot's stored password (bots have persisted passwords, so bootstrap is non-interactive).
3. Sign the device's ed25519 key with the self-signing key and upload via `POST /keys/signatures/upload`.
4. Persist the private cross-signing keys in the per-user store under `encryption_keys/`, alongside the olm store.
5. On any new device for the same account (Flow D recovery), load the persisted cross-signing keys and sign the new device before the first sync completes.

Acceptance: mindroom-cinny's device-verification hooks report the agent device as cross-signed, and an Element instance with the exclude-insecure-devices lab flag enabled can hold an encrypted conversation with the agent (this is the strictest client behavior available and the canary for future js-sdk defaults).

### D3: managed-room encryption enablement (Flow B)

Schema: `encrypted: bool` on managed room config entries plus `matrix_room_access.encrypt_managed_rooms` as the default for new rooms; both default to false initially.
The default can flip to true once D2 has shipped and soaked, because encrypting before bots are cross-signed would walk users straight into MSC4153 breakage.
Members are never granted power to set `m.room.encryption` themselves; the bot performs the state change so the confirmation and audit path is uniform.

### D4: failure feedback and diagnostics (Flow C)

The visible notice reuses the delivery gateway (it sends fine — outbound uses the bot's own outbound session) and is keyed on (room_id, session_id) with a persistent seen-set so restarts do not re-notify.
The notice is `m.notice`, threaded off nothing (room-level), and suppressed entirely in rooms with more than one bot unless the bot is the router.

### D5: store lifecycle and recovery (Flow D)

Startup ordering: restore login, then verify the olm store opens and contains the account keys for the persisted device; on failure, fall back to fresh-device login plus D2 re-signing rather than continuing with a wedged identity.
`mindroom doctor` gains an E2EE section: store presence and integrity per agent, cross-signing status, and a warning when `matrix_state.yaml` and `encryption_keys/` are out of sync.

### D6: server-side key backup (deferred)

Megolm key backup (`m.megolm_backup.v1.curve25519-aes-sha2`) would let a rebuilt store recover old room keys, but the durable event cache already preserves agent context across store loss, so the user-visible benefit is small relative to the fork work.
Revisit after D2–D5 ship if history recovery in Matrix clients (not just agent context) proves important.

### D7: mindroom-cinny fork work (owning the other end)

The fork already gives a quiet baseline (no per-message shields for other users' devices, encrypted DMs by default), so fork changes are polish and safety rails rather than prerequisites:

1. Emergency valve: document `createRoom.defaultEncryption: false` in the hosted config runbook as the config-only rollback if an E2EE regression ever hits production chat.
2. After D2 ships: a verified-agent affordance in room and member views, driven by the existing `useDeviceVerificationStatus` hook against the agent's cross-signed device; no new trust machinery, since cross-signing is the substrate.
3. Key-continuity onboarding: surface the existing key-backup setup (`BackupRestore`) prominently in the hosted first-run flow so a user's new login can still read their encrypted agent history; today the only nudge is the own-device `UnverifiedTab`.
4. Optional UTD polish: friendlier client-side copy for undecryptable events in agent rooms, complementing (not replacing) the server-side D4 notice, which must work for users on any client.
5. Dependency policy: treat matrix-js-sdk bumps that change key-sharing defaults (MSC4153 adoption) as gated on D2 being deployed, and say so in the fork's FORK_CHANGES maintenance notes.

## Phasing

1. Phase 1 (shipped, PR #1423): unconditional encrypted delivery, decrypt-failure logging and key requests.
2. Phase 2 (implemented on the `e2ee-phase2-ux` branch, live-verified 2026-07-07): D3 room enablement (config + `!encrypt`; dashboard toggle deferred), D4 notices and `!e2ee`, D5 doctor checks and fresh-device fallback without cross-signing.
3. Phase 3 (mindroom-nio fork): D2 cross-signing bootstrap and re-signing; then wire the mindroom startup path to it and add the strict-client live test (Element lab flag) to smoke docs.
4. Phase 4 (mindroom-cinny fork, after D2): D7 verified-agent affordance and key-backup onboarding; lift the js-sdk bump gate.
5. Phase 5 (optional): D6 key backup; flip `encrypt_managed_rooms` default after D2 soak.

Phases 2 and 3 are independent and can proceed in parallel; Phase 4 depends on D2; only the default flip in Phase 5 depends on everything before it.

## Test strategy

Unit tests target the owning seams: room provisioning (initial_state and reconciliation), the notice rate limiter, doctor checks, and in the nio fork, canonical-JSON signing vectors against known cross-signing fixtures.
Live verification reuses the nio-based encrypted driver from the 2026-07-07 session (register, create encrypted room, send, watch), extended with: the built mindroom-cinny client against a local stack as the primary end-to-end surface (create chat with an agent, encrypted by default), an Element Web instance with the MSC4153 lab flag as the strict-client canary for D2, a store-deletion restart for D5, and `!encrypt` on a live managed room for D3.

## Open questions

1. Should hosted-provisioned personal rooms (mindroom.chat pairing flow) default to encrypted once D2 ships, ahead of the general managed-room default flip?
2. Is the decrypt-failure notice wanted in the hosted product voice, or should hosted deployments disable it and rely on dashboard surfacing plus fork-side UTD copy only?
3. Does the nio fork adopt matrix-rust-sdk's cross-signing store format for future portability, or keep a minimal fork-native serialization?
4. Should key-backup onboarding in mindroom-cinny be a blocking first-run step for hosted users, or a dismissible prompt?
