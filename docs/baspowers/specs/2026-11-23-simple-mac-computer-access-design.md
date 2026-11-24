# Simple Mac Computer Access Design

Date: 2026-11-23. Status: approved by Bas, M2 TCC spike verified, ready for planning.

## Intent

Make giving a MindRoom agent full control of a Mac as close to zero-friction as macOS allows: **2 clicks in `MindRoom Chat.app` to live Shell & Files**, **2–6 clicks total to live Full GUI + Screen + Shell** (4–6 only when macOS Accessibility/Screen Recording are not yet granted), and **2 clicks to add any additional agent**.

- **Primary use case:** a user talking to an agent wants to give it full and total access to their Mac (apps, screen, and unprompted shell commands, surviving restarts and reboots) with the single least amount of clicks.
- **Secondary use case:** fine-grained restrictions (per-app allowlist, observe-only default, timed control leases, read-only folders, ask-per-shell, Playwright browser extension) already exist and stay intact behind **Choose limits…**.
- **Explicit decisions approved by Bas (2026-11-23):**
  1. Move Computer Access into **`MindRoom Chat.app`** (Electron Mac app, `chat.mindroom.app`) on Apple Silicon by bundling `MindRoom Desktop Helper.app`; refocus `MindRoom.app` (`chat.mindroom.menubar`) on Local Agents.
  2. **Full Access** includes both GUI/screen control AND persistent unprompted shell commands across restarts/reboots (with clear disclosure on the consent sheet and 1-click menu-bar Revoke; never silently upgrades existing GUI-only grants).
  3. **Dynamic requester-scoped `desktop` tool activation:** pairing a Mac activates the `desktop` tool for `(requester_id, agent_name)` automatically with no `config.yaml` edit (zero prompt overhead when unpaired; configurable opt-out policy for strict deployments).

## Empirical M2 Verification (2026-11-23)

Verified on Bas's MacBook Pro M2 (`macOS 26.5.2`, `arm64`):

1. **macOS TCC responsible-process attribution:** launching a `.app` bundle via LaunchServices (`open`) and spawning `Contents/Helpers/MindRoom Desktop Helper.app/Contents/MacOS/MindRoom Desktop Helper` via Node `child_process.spawn` sets `responsibility_get_pid_responsible_for_pid(child.pid) == parent.pid` (`50324 == 50324`). macOS therefore attributes both Accessibility and Screen Recording to `MindRoom Chat.app` (`chat.mindroom.app`), not a second app entry, and any Screen Recording grant already held by `MindRoom Chat.app` (for Quick Capture / Screen Share) covers the spawned helper.
2. **Helper artifact & protocol:** the signed notarized `MindRoom Desktop Helper.app` (`chat.mindroom.desktophelper`, Team ID `DNA6966LGZ`, matching `chat.mindroom.app`) is **88 MB uncompressed / 43 MB `.tar.gz`**, starts in <1 s with `--config <path> --storage-path <path>`, and speaks the existing NDJSON stdio protocol (`type: "hello"`, `type: "status"`, requests keyed by `request_id`).
3. **MSC3882 silent login:** `https://mindroom.chat/_matrix/client/v3/login` advertises `{"type": "m.login.token", "get_login_token": true}`. `mindroom-tuwunel`'s `login_token_route` (`src/api/client/session/token.rs:38`) issues tokens through `auth_uiaa` (already handled by `mindroom-chat`'s `connectWatch.ts` via `useAuthenticatedAction`). `src/mindroom/desktop/native_host.py:821-840` already accepts `login_token` in `_login()` and skips browser SSO when provided.

## Architecture Overview

Three phases across two repositories (`mindroom` and `mindroom-chat`), each shipping a clean, self-contained PR:

- **Phase 1 (`mindroom`):** Backend & Desktop Helper foundations — Olm to-device pairing protocol, multi-controller config/journal, shell-first bridge startup without GUI permissions, dynamic requester-scoped `desktop` tool activation, `MINDROOM_APP_IDS` + pairing hardening, and signed helper release artifact.
- **Phase 2 (`mindroom-chat`):** Native Computer Access in `MindRoom Chat.app` — bundle `MindRoom Desktop Helper.app` in Apple Silicon builds, Electron main `DesktopBridgeProcess` + Tray kill switch + Login Item + native shell-approval window, 2-click **Allow Full Access** + **Choose limits…** UI, silent MSC3882 login over private IPC, live Accessibility polling, and Screen Recording reuse / 1-click relaunch.
- **Phase 3 (`mindroom` + `mindroom-chat`):** Web Browser Chat Olm relay (`pair_invite.v1` + secretless `mindroom://connect` wake-up) and `MindRoom.app` handoff/bridge-lock cleanup.

---

## Phase 1: Backend & Desktop Helper (`mindroom`)

### 1.1 Security Hardening
- **`MINDROOM_APP_IDS` (`src/mindroom/desktop/input.py`):** expand from `{"chat.mindroom.menubar", "chat.mindroom.desktophelper"}` to also exclude Electron `MindRoom Chat.app` and its helpers:
  - `"chat.mindroom.app"`
  - `"chat.mindroom.app.helper"`
  - `"chat.mindroom.app.helper.Renderer"`
  - `"chat.mindroom.app.helper.GPU"`
  - `"chat.mindroom.app.helper.Plugin"`
- **Pairing requester binding (`src/mindroom/desktop/pairing.py`):** in `claim_desktop_pairing`, verify `device_user_id == pending.requester_id` before recording a claim (rejecting claims from any other Matrix account with `DesktopPairingError("Desktop pairing code does not belong to this Matrix account.")`).
- **One bridge per storage path (`src/mindroom/desktop/native_host.py`):** acquire a non-blocking advisory lock file (`<storage>/desktop_bridge/host.lock`) when starting the bridge so `MindRoom Chat.app` and `MindRoom.app` can never run two bridges against the same storage path simultaneously (failing with a clear `already_running` error naming the active owner).

### 1.2 Olm To-Device Pairing Protocol (Zero Copy-Paste, Matrix-Only)

All new pairing events use Olm-encrypted Matrix to-device delivery (verified with `authenticated_sender_is_current`) — no secrets ever enter a room timeline, no public MindRoom backend HTTP URL is required, and pairing works from any room (DM or multi-agent).

New event types in `src/mindroom/desktop/protocol.py` (all carry `"v": 1`):

| Event type | Direction | Fields |
|---|---|---|
| `io.mindroom.desktop.setup_request.v1` | User's Chat device → Agent | `v`, `nonce` (16–128 chars), `agent_name` |
| `io.mindroom.desktop.setup_offer.v1` | Agent → User's Chat device | `v`, `nonce`, `homeserver`, `user_id`, `code`, `agent_name`, `cloudflare_access`, `eligible` (bool), `error` (optional str) |
| `io.mindroom.desktop.pairing_claim.v1` *(existing)* | Helper device → Agent | `v`, `token` |
| `io.mindroom.desktop.pairing_accepted.v1` *(existing)* | Agent → Helper device | `v`, `verification` |
| `io.mindroom.desktop.pairing_confirm.v1` | User's Chat device → Agent | `v`, `nonce`, `token`, `verification`, `device_id`, `device_ed25519` |
| `io.mindroom.desktop.pairing_confirmed.v1` | Agent → User's Chat device | `v`, `nonce`, `ok` (bool), `agent_name`, `error` (optional str) |
| `io.mindroom.desktop.disconnect_request.v1` | User's Chat device → Agent | `v`, `nonce`, `agent_name` |
| `io.mindroom.desktop.disconnect_confirmed.v1` | Agent → User's Chat device | `v`, `nonce`, `ok` (bool), `agent_name`, `error` (optional str) |

**Security invariants of the Olm pairing flow:**
1. **`setup_request.v1`:** receiver checks `authenticated_sender_is_current`, verifies `agent_name == self.agent_name`, checks pairing eligibility (`desktop_pairing_policy`), creates a pending pairing bound to `(requester_id=event.sender, agent_name=self.agent_name, initiator_device_id=identity.device_id, initiator_ed25519=identity.ed25519)`, and replies via Olm to that exact initiating Chat device with `setup_offer.v1`. The Chat client reads `(controller_user_id, controller_device_id, controller_ed25519)` directly from the Olm-authenticated sender of `setup_offer.v1`.
2. **`pairing_claim.v1`:** helper claims `token` via Olm. Receiver verifies `authenticated_sender_is_current` AND `event.sender == pending.requester_id`.
3. **`pairing_confirm.v1`:** receiver checks `authenticated_sender_is_current`, verifies the sender is the **exact initiating Chat device** (`(event.sender, identity.device_id, identity.ed25519) == (pending.requester_id, pending.initiator_device_id, pending.initiator_ed25519)`), verifies `pending.device_id == confirm.device_id` and `pending.device_ed25519 == confirm.device_ed25519` and `verification` matches `desktop_pairing_verification(token, pending.device_ed25519)`, saves credentials, deletes the pending pairing row, and replies `pairing_confirmed.v1`.
4. **Backwards compatibility:** legacy `/desktop setup`, `/desktop confirm`, `/desktop status`, `/desktop rotate`, `/desktop disconnect` text commands remain functional. When a pending pairing was created via `/desktop setup`, `initiator_device_id` is `None` and confirmation uses the existing `/desktop confirm` path. `/desktop setup <agent_name>` also works in rooms with multiple MindRoom agents (refusing only when another **human** account is in the room, since text `/desktop setup` prints a bearer code to the timeline).

### 1.3 Multi-Controller Config (`v: 2`) & Per-Controller Journal

Remove the single-controller cliff so one Mac can pair with multiple agents without wiping storage:

- **`NativeDesktopConfig` (`src/mindroom/desktop/native_config.py`):**
  - Support schema `v: 2` while seamlessly reading `v: 1` (normalizing `v: 1`'s single `controller` + `allowed_requester_ids` + `allowed_agent_names` into a 1-element `controllers` list).
  - Each `NativeControllerEntry` holds:
    - `controller: PinnedMatrixDevice` (`user_id`, `device_id`, `ed25519`)
    - `allowed_requester_ids: tuple[str, ...]`
    - `allowed_agent_names: tuple[str, ...]`
  - Preserve `controller` / `allowed_requester_ids` / `allowed_agent_names` convenience fields in `status()` (mirroring the primary/first controller for backwards compatibility with existing `MindRoom.app`) plus a `controllers: [...]` array listing all paired controllers.
  - Add native host actions `upsert_controller` and `remove_controller` (working while stopped or safely reloading the running bridge controller map without dropping the Matrix session or existing shell handles for other controllers).
  - **Single-writer rule preserved (`#4154`):** `native_config.json` NEVER stores live `full_control` or `shell_auto_approve` grants. Only `allowed_app_ids`, `files.roots`, and `shell.enabled` live in `native_config.json`. The host app owns the local consent preference (keyed to exact `controller_key`) and issues `grant_control` / `grant_shell` with that `controller_key` on each bridge start.
  - **Per-controller live grant scoping:** `grant_control`, `revoke_control`, `grant_shell`, and `revoke_shell` accept an optional `controller_key: str | None` parameter (omitted = all paired controllers for backwards compatibility with existing `MindRoom.app` and CLI callers). When provided, live unexpiring computer control and unprompted shell auto-approval apply **only** to that exact controller, so pairing a 2nd agent in restricted mode never inherits another agent's Full Access.
- **`DesktopCommandJournal` (`src/mindroom/desktop/command_journal.py`):**
  - Bump SQLite `PRAGMA user_version` from `1` to `2`.
  - Migrate existing `v1` rows (tagging existing `commands` and `sequences` rows with `metadata['controller']` as their `controller_key`).
  - Scope uniqueness and queries by `(controller_key, request_id)` and `(controller_key, session_id)`.
  - Remove the single-controller refusal (`"Desktop journal belongs to a different controller."`); each controller gets its own isolated replay and sequence namespace in the same SQLite database.
- **`DesktopBridge` (`src/mindroom/desktop/bridge.py` & `bridge_session.py`):**
  - Accept `controllers: tuple[DesktopControllerPolicy, ...]` (lookup incoming Olm `AuthenticatedToDeviceEvent` by matching pinned controller `(user_id, device_id, ed25519)`).
  - Verify all pinned controllers' device keys on startup (`prepare_client` resolves each pinned controller).
  - Execution remains serialized across controllers so two agents cannot interleave mouse/keyboard actions simultaneously.
  - Response delivery targets the exact controller that issued the command.

### 1.4 Shell-First Startup (Unblock Bridge on Missing GUI Permissions)

- Today `bridge_session.py` starts a `DesktopShell` and `MacOSDesktopProvider` independently, and `provider.py` already checks macOS Accessibility/Screen Recording when GUI actions execute — only the UI's `startBlocker` blocked starting when `allowed_app_ids` was non-empty and permissions were missing.
- Ensure `native_host.py`'s `start` action succeeds whenever `config.has_local_capability` is true, even if `accessibility` or `screen_recording` is `"missing"`.
- Report granular readiness in `bridge.status()` and `native_host.status()`:
  - `gui_permissions_ready: bool` (both Accessibility and Screen Recording granted)
  - `shell_ready: bool` (`shell.enabled` and bridge running)
  - When a GUI action runs while Accessibility or Screen Recording is missing, return a clear structured error (`"macOS Accessibility/Screen Recording permission is not granted yet; shell commands remain available."`).
- Ensure `grant_control(until_revoked=True, scope="computer")` and `grant_shell(until_revoked=True)` both succeed immediately when the bridge starts, even while GUI permissions are pending, so the instant Accessibility/Screen Recording flip green, GUI control works with zero extra grant step.

### 1.5 Dynamic Requester-Scoped `desktop` Tool Activation

- Add `desktop_pairing: Literal["self_service", "explicit_only", "disabled"] = "self_service"` to `DefaultsConfig` (`src/mindroom/config/models.py`).
- **Receiver registration (`src/mindroom/desktop/pairing_receiver.py` & `src/mindroom/bot.py`):**
  - When `config.defaults.desktop_pairing == "self_service"`, register `DesktopPairingReceiver` on every concrete agent (unless `"desktop"` is explicitly excluded or policy is `"disabled"`).
  - When `"explicit_only"`, register only on agents whose resolved `tools` include `"desktop"` (current behavior).
  - When `"disabled"`, register on none.
- **Runtime tool assembly (`src/mindroom/agents.py` `_assemble_agent_tools`):**
  - If `config.defaults.desktop_pairing == "self_service"`, `"desktop"` is not already in `resolved_tool_configs`, not in `disabled_tool_names`, not excluded by an authored `persona` with narrowed tools, and `execution_identity` has a `requester_id` whose `load_desktop_credentials(cred_manager, requester_id=..., agent_name=agent_name)` is `DesktopConfigurationStatus.READY`, inject `EffectiveToolConfig(name="desktop", ...)` for that turn.
  - Result: zero prompt tokens when unpaired; instant `desktop` tool availability on the very next message after pairing, with no `config.yaml` edit or restart.
- **Eligibility discovery:** `setup_request.v1` returns `eligible: false` with a clear reason if `desktop_pairing == "disabled"` or (`"explicit_only"` and agent lacks `desktop`).

### 1.6 Helper Release Artifact (`.github/workflows/release.yml`)

- In `build_macos_app` (after `sign_app "$HELPER_APP"` and `"$HELPER_VERIFY_SCRIPT" "$HELPER_APP"`), also archive and upload the signed, notarized `MindRoom Desktop Helper.app` as `MindRoom-Desktop-Helper-arm64.tar.gz` plus `MindRoom-Desktop-Helper-arm64.tar.gz.sha256` on every GitHub release, so `mindroom-chat` CI can fetch and SHA-256-verify an exact pinned version.

---

## Phase 2: Native Computer Access in `MindRoom Chat.app` (`mindroom-chat`)

### 2.1 Packaging & Signing (`electron/builder.config.cjs`, `.github/workflows/desktop.yml`)
- Split macOS DMG targets into `arm64` (`MindRoom-Chat-arm64.dmg` + `MindRoom-Chat-arm64-mac.zip`, bundling `Contents/Helpers/MindRoom Desktop Helper.app`) and `x64` (`MindRoom-Chat-x64.dmg` + `MindRoom-Chat-x64-mac.zip`, without the arm64-only helper).
- Pin helper release tag + SHA-256 in `scripts/desktop-helper-pin.json`; download, verify SHA-256 + `codesign --verify --deep --strict` (Team ID `DNA6966LGZ`, identifier `chat.mindroom.desktophelper`) during build.

### 2.2 Electron Main Host (`electron/main/desktopAccess.ts`, `tray.ts`, `shellApprovalWindow.ts`)
- **`DesktopBridgeProcess` (~250 lines):** spawns `MindRoom Desktop Helper.app/Contents/MacOS/MindRoom Desktop Helper --config ~/.mindroom/config.yaml --storage-path ~/.mindroom/mindroom_data`, speaks NDJSON stdio (`hello`, `status`, `request_id` responses), manages clean shutdown on Quit.
- **Local Full Access Consent Store (`userData/desktop-access-consent.json`, mode `0600`):**
  - Stores per-controller local consent `{controllerKey, mode: "full", grantedAt}` — never writable by the renderer without an active main-process `noteGesture()` + native confirmation.
  - On app launch / bridge online: automatically re-sends `grant_control({until_revoked: true, scope: "computer"})` and `grant_shell({until_revoked: true})` for consented controllers, and enables `app.setLoginItemSettings({openAtLogin: true})` (tracking whether Full Access added the login item so revoking restores it).
- **Menu Bar Tray & Kill Switch:**
  - Persistent macOS menu bar icon whenever Computer Access is active (`MindRoom · Full Access (<agents>)` or `MindRoom · Active`).
  - Closing the main window hides it instead of quitting while Computer Access is active.
  - Top menu item: **Revoke Full Access** (immediately clears consent, calls `revoke_control` + `revoke_shell`, stops bridge).
- **Native Shell Approval Window:** non-focus-stealing `BrowserWindow` for restricted (`Choose limits…`) mode when `shell.pending` arrives, preserving the 1 s arming delay and escaped-character inspection.
- **Narrow IPC (`electron/main/ipc.ts`):** only exposed to the bundled main frame; sensitive actions (`grantFullAccess`, `loginWithToken`, `pairController`, `revokeAll`) require main-process `noteGesture()`.

### 2.3 Two-Click UX (`src/app/desktop-access/`)
- **Entry points (any room, DM or multi-agent):**
  - Room header / agent profile action: **Give Mac Access** (shows green status dot when active; lists room agents if >1).
  - Settings → **This Mac**: full status, paired agents list, **Full Access** / **Choose limits…** editor, permissions, diagnostics, and **Revoke All**.
  - `/desktop` slash command in Electron opens the native Mac Access sheet directly instead of printing JSON.
- **Consent Sheet (the 2nd click):**
  > **Allow `<agent>` full access to this Mac?**  
  > `<agent>` can see your screen, control every app, and run shell commands as your macOS user without asking, including after restart. Stop anytime from the menu bar.  
  > **[Allow Full Access]** · **Choose limits…** · **Cancel**
- **Automatic background flow after clicking Allow Full Access:**
  1. Send `setup_request.v1` over Olm (`callKeyProvider.ts`'s `encryptToDeviceMessages` pattern) → receive `setup_offer.v1`.
  2. If helper lacks a matching Matrix session: mint one-time `login_token` via `connectWatch.ts`'s MSC3882 flow (`POST /_matrix/client/v1/login/get_token` with `useAuthenticatedAction`), pass over Electron IPC → helper `login({method: "sso", login_token})`. (Fallback: existing helper browser SSO if homeserver lacks MSC3882 or uses Cloudflare Access.)
  3. Helper runs `upsert_controller` (with `primary-screen` + `shell.enabled: true`) + `pair(code)` → returns `{device_id, device_ed25519, verification}`.
  4. Chat sends `pairing_confirm.v1` over Olm → receives `pairing_confirmed.v1` → helper runs `finish_setup` + `start` → main sends `grant_control(until_revoked, computer)` + `grant_shell(until_revoked)`.
  5. **Shell & Files are live in ~2 seconds.** Status banner reads *"Shell ready · Waiting for macOS screen & input permissions"* if TCC is not yet granted.
- **Zero-Nag TCC Permissions Row (inline, only shown if missing):**
  - **Accessibility:** 1-click **Allow Accessibility** opens System Settings > Accessibility; live-polled every 1 s — flips green automatically with **zero restart** and **zero Check Again button**.
  - **Screen Recording:** already green (0 clicks, 0 restart) if previously granted for Quick Capture. Otherwise 1-click **Allow Screen Recording** requests & opens System Settings; once enabled, shows a single **Relaunch to activate screen control** button (`app.relaunch(); app.exit(0)`), which automatically resumes Full Access on relaunch.
- **Choose limits…:** expands the existing granular controls (app picker, observe-only + timed control lease, read-only folders, shell off / ask-each-time / timed auto-approve, browser extension) without cluttering the primary path.

---

## Phase 3: Browser Chat Relay & `MindRoom.app` Consolidation

- **Browser Chat (Safari/Chrome/Firefox) → signed-in Mac app (3 clicks, zero localhost/mixed-content):**
  - Browser Chat sends `setup_request.v1` to the agent, discovers the user's Mac helper device via `/keys/query`, and sends `io.mindroom.desktop.pair_invite.v1` `{controller, agent_name, code, sas_code}` to the Mac helper over Olm to-device.
  - `MindRoom Chat.app` pops a native local consent sheet showing the requester browser + agent + 6-digit SAS code; 1 click approves, claims, and notifies Browser Chat to send `pairing_confirm.v1`.
  - First-time browser users without `MindRoom Chat.app` get a 1-click **Download MindRoom Chat for Mac** button (or secretless `mindroom://connect?hs=<hs>&agent=<agent>&req=<id>` wake-up link carrying zero tokens/codes).
- **`MindRoom.app` (`macos/MindRoom`) cleanup:**
  - Detect active `MindRoom Chat.app` bridge lock and show a clean handoff banner ("Computer Access is now built into MindRoom Chat" + **Open MindRoom Chat**), keeping `MindRoom.app` focused on Local Agents and local inference.

---

## Click Budget Summary

| Flow | Clicks |
|---|---:|
| Electron Chat → Shell & Files live | **2** *(+1 UIA once-ever per Mac if homeserver requires UIA)* |
| Electron Chat → Full GUI + Screen + Shell *(TCC already granted)* | **2** |
| Electron Chat → Full GUI + Screen + Shell *(brand-new Mac)* | **6** *(2 app + 4 macOS System Settings toggles; auto-relaunch)* |
| Add a 2nd agent to same Mac | **2** *(instant; no re-login, no TCC)* |
| Web Browser Chat → Mac app already signed in | **3** *(2 browser + 1 local Approve on Mac)* |
| Emergency Revoke Full Access | **2** *(Menu bar Tray → Revoke Full Access)* |

## Testing & Verification Strategy

- **Phase 1 (`mindroom`):** unit & integration tests for every new Olm pairing event, spoofed-sender / mismatched-requester / mismatched-initiator-device rejections, `MINDROOM_APP_IDS` exclusion of `chat.mindroom.app`, `v1 -> v2` config migration, multi-controller journal isolation and replay, shell-first startup without GUI permissions, host lock conflict, and dynamic requester-scoped `desktop` tool activation (plus `explicit_only` and `disabled` policies).
- **Phase 2 (`mindroom-chat`):** Vitest tests for Olm pairing state machine, MSC3882 fallback, Electron main `DesktopBridgeProcess` / consent store / `noteGesture()` guards, and macOS Playwright Electron e2e specs on M2/CI (plus a live signed-build acceptance check on M2).
- **Phase 3:** Browser-to-Mac Olm `pair_invite.v1` tests + `MindRoom.app` lock handoff tests.
