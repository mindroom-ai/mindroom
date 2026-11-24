# Simple Mac Computer Access (Phase 1: Backend & Desktop Helper) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: implement this plan task-by-task with the skill named in **Execution**. Steps use checkbox (`- [ ]`) syntax.

**Execution:** `baspowers:subagent-driven-development` — 6 substantial backend/security/protocol tasks with strict invariants across `mindroom/desktop`, `commands`, `agents`, and CI packaging; fresh `gemini4_neon@xhigh` implementer per task with targeted pytest + final `opus` + `astra` whole-branch review.

**Goal:** Deliver all `mindroom` backend and Desktop Helper foundations for 2-click Mac Computer Access: Olm to-device pairing, multi-controller config & journal with per-controller live grants, shell-first startup without GUI permissions, dynamic requester-scoped `desktop` tool activation, security hardening, and a signed helper release artifact.

**Architecture:** Preserve the existing Python `DesktopBridge` + NDJSON stdio `NativeDesktopHost` and `#4154`'s single-writer consent rule (`native_config.json` stores capabilities and pinned controllers, never live auto-grant flags). Add Olm to-device pairing events alongside legacy `/desktop` text commands, extend `NativeDesktopConfig` (`v: 2`, backwards-compatible with `v: 1`) and `DesktopCommandJournal` (`user_version = 2`) for multiple controllers, and inject the `desktop` tool per requester when paired under `defaults.desktop_pairing = "self_service"`.

**Tech Stack:** Python 3.13+, `matrix-nio` (Olm to-device), SQLite3, Pydantic v2, `pytest` / `pytest-asyncio`, `ruff`, `tach`, GitHub Actions macOS signing.

**Spec:** `docs/baspowers/specs/2026-11-23-simple-mac-computer-access-design.md`

## Global Constraints

- Never persist live `full_control` or `shell_auto_approve` flags in `native_config.json`; only the local host app stores consent and sends `grant_control` / `grant_shell` on bridge start.
- Never allow `MINDROOM_APP_IDS` (`chat.mindroom.menubar`, `chat.mindroom.desktophelper`, `chat.mindroom.app`, `chat.mindroom.app.helper`, `chat.mindroom.app.helper.Renderer`, `chat.mindroom.app.helper.GPU`, `chat.mindroom.app.helper.Plugin`) to be allowlisted or targeted under computer scope.
- Every new Olm to-device pairing event MUST verify `authenticated_sender_is_current(client, event)` before processing.
- Legacy `v: 1` `native_config.json`, `user_version = 1` `commands.sqlite3`, single-controller `MindRoom.app` `status()` fields, and legacy `/desktop setup` / `/desktop confirm` commands MUST stay backwards-compatible.
- All new internal imports must satisfy `uv run --with tach tach check --dependencies --interfaces`.
- Target ~1:1 test:prod line ratio testing real invariants and failure paths; keep edits compact and reuse `tests/desktop_helpers.py`.
- Never run `env`, `printenv`, `set`, or `uv pip install` without `--python .venv/bin/python` and `unset VIRTUAL_ENV`.
- Every commit ends with `--trailer "Assisted-by: MindRoom DevAgent:<model_id> <https://github.com/mindroom-ai/mindroom>"`.

## Review Focus

1. **Cross-account or cross-device pairing hijack:** a second Matrix account claiming a token, or a different device of the same account sending `pairing_confirm.v1` for a pairing initiated by another Chat device — both must be rejected without modifying saved credentials.
2. **Privilege bleed across paired controllers:** pairing Agent B in restricted mode while Agent A holds Full Access must NOT grant Agent B computer control or unprompted shell; revoking Agent A must leave Agent B's pending/active work intact (and vice versa).
3. **v1 → v2 live upgrade of existing Mac installs:** an existing `v: 1` `native_config.json` and `user_version = 1` `commands.sqlite3` (with in-flight/completed receipts) must load, migrate cleanly, preserve replay receipts under the original `controller_key`, and continue returning legacy top-level `controller_*` fields in `status()`.
4. **Partial-permission startup (Shell ready, GUI missing):** starting a bridge with `allowed_app_ids=["primary-screen"]` and `shell.enabled=True` when macOS Accessibility/Screen Recording are missing must succeed, serve `run_shell` immediately, accept `grant_control`/`grant_shell`, and return a structured permission error on GUI actions without crashing the bridge.
5. **Dual-host lock contention:** a second `NativeDesktopHost` starting a bridge on the same `<storage>/desktop_bridge` directory must fail closed with `already_running` instead of racing on `commands.sqlite3` or the Matrix Olm session, and releasing the lock on clean stop or crash must allow restart.

---

### Task 1: Security Hardening (`MINDROOM_APP_IDS`, Pairing Requester Binding, Host Bridge Lock)

**Files:**
- Modify: `src/mindroom/desktop/input.py`
- Modify: `src/mindroom/desktop/pairing.py`
- Modify: `src/mindroom/desktop/native_host.py`
- Test: `tests/test_desktop_input.py`, `tests/test_desktop_pairing.py`, `tests/test_desktop_native_host.py`

**Interfaces:**
- Consumes: `MINDROOM_APP_IDS` (`src/mindroom/desktop/input.py`), `claim_desktop_pairing` (`src/mindroom/desktop/pairing.py`), `NativeDesktopHost._dispatch` (`src/mindroom/desktop/native_host.py`).
- Produces: Expanded `MINDROOM_APP_IDS` (7 bundle IDs), requester-checked `claim_desktop_pairing(...)`, non-blocking advisory lock `<storage>/desktop_bridge/host.lock` held for the lifetime of `NativeBridgeRuntime`.

- [ ] **Step 1: Write failing tests**
  - In `tests/test_desktop_input.py`: assert `"chat.mindroom.app"` and all 4 Electron helper IDs (`chat.mindroom.app.helper`, `chat.mindroom.app.helper.Renderer`, `chat.mindroom.app.helper.GPU`, `chat.mindroom.app.helper.Plugin`) are in `MINDROOM_APP_IDS` and rejected by `NativeDesktopConfig` / `accessibility` computer scope.
  - In `tests/test_desktop_pairing.py`: `test_claim_rejects_different_matrix_account` — `create_desktop_pairing(..., requester_id="@alice:example.org", ...)` followed by `claim_desktop_pairing(..., device_user_id="@mallory:example.org", ...)` raises `DesktopPairingError`.
  - In `tests/test_desktop_native_host.py`: `test_start_refuses_when_host_lock_is_held` — holding an exclusive `flock` on `<storage>/desktop_bridge/host.lock` makes `start` fail with `NativeProtocolError.code == "already_running"`; releasing the lock lets `start` succeed and `stop` release the lock.
- [ ] **Step 2: Run tests to verify failure**
  - Run: `NIX_PATH=nixpkgs=flake:nixpkgs nix-shell --run 'uv run pytest tests/test_desktop_input.py tests/test_desktop_pairing.py tests/test_desktop_native_host.py -k "mindroom_app_ids or different_matrix_account or host_lock" -x -n 0 --no-cov'`
  - Expected: FAIL.
- [ ] **Step 3: Implement minimal code**
  - Add the 5 `chat.mindroom.app*` bundle IDs to `MINDROOM_APP_IDS` in `src/mindroom/desktop/input.py`.
  - In `src/mindroom/desktop/pairing.py` (`claim_desktop_pairing`): check `if device_user_id != pending.requester_id: raise DesktopPairingError("Desktop pairing code does not belong to this Matrix account.")` before checking `pending.claimed`.
  - In `src/mindroom/desktop/native_host.py`: acquire a non-blocking `fcntl.flock(LOCK_EX | LOCK_NB)` on `<storage>/desktop_bridge/host.lock` inside `start` (before spawning `NativeBridgeRuntime`) and release/close the fd on startup failure or in `stop` `finally:`.
- [ ] **Step 4: Run targeted tests**
  - Run: `NIX_PATH=nixpkgs=flake:nixpkgs nix-shell --run 'uv run pytest tests/test_desktop_input.py tests/test_desktop_pairing.py tests/test_desktop_native_host.py -x -n 0 --no-cov'`
  - Expected: PASS.
- [ ] **Step 5: Commit**
  - `git commit -m "fix(desktop): harden app exclusions, pairing requester check, and host bridge lock"` with `Assisted-by` trailer.

---

### Task 2: Multi-Controller Config (`v: 2`) & Per-Controller SQLite Journal (`user_version = 2`)

**Files:**
- Modify: `src/mindroom/desktop/native_config.py`
- Modify: `src/mindroom/desktop/command_journal.py`
- Test: `tests/test_desktop_native_config.py`, `tests/test_desktop_command_journal.py`

**Interfaces:**
- Consumes: `PinnedMatrixDevice` (`src/mindroom/matrix/device_identity.py`), `controller_journal_key` (`src/mindroom/desktop/command_journal.py`).
- Produces:
  - `NativeControllerEntry(controller: PinnedMatrixDevice, allowed_requester_ids: tuple[str, ...], allowed_agent_names: tuple[str, ...])` in `src/mindroom/desktop/native_config.py`.
  - `NativeDesktopConfig.controllers: tuple[NativeControllerEntry, ...]` (kept in sync with backwards-compatible properties `.controller`, `.allowed_requester_ids`, `.allowed_agent_names` returning the primary entry / union as appropriate) plus `.with_upserted_controller(entry: NativeControllerEntry) -> NativeDesktopConfig` and `.with_removed_controller(controller_key: str) -> NativeDesktopConfig`.
  - `DesktopCommandJournal` (`user_version = 2`) accepting `controller_key: str` on `admit`, `get`, `sequence_error`, `remember_response`, `queued`, `started`, `pending_responses` (with default fallback to single-controller `default_controller_key` for backwards-compatible callers).

- [ ] **Step 1: Write failing tests**
  - In `tests/test_desktop_native_config.py`:
    - `test_v1_payload_loads_into_single_controller_entry`: loading an existing `v: 1` JSON file yields `len(cfg.controllers) == 1` and matches `cfg.controller`.
    - `test_v2_roundtrip_and_upsert_remove_controller`: upserting a 2nd controller preserves the 1st; upserting the same `(user_id, device_id, ed25519)` updates its requesters/agents without duplication; removing a controller drops only that entry and refuses removing the last controller when `enabled=True` (or disables cleanly when last controller is removed).
    - `test_rejects_duplicate_controller_keys` and rejects unknown fields/versions.
  - In `tests/test_desktop_command_journal.py`:
    - `test_migrates_v1_journal_to_v2_and_preserves_receipts`: open a real `user_version = 1` DB containing a command + response + sequence under controller A, reopen under `v2`, verify controller A's receipt replays AND controller B can admit the same `request_id` and `session_id` without conflict.
    - `test_two_controllers_isolated_replay_and_sequences`: same `request_id` / `session_id` admitted for controller A and controller B stay isolated in `get`, `sequence_error`, and `pending_responses` (returning `controller_key` with each pending response).
- [ ] **Step 2: Run tests to verify failure**
  - Run: `NIX_PATH=nixpkgs=flake:nixpkgs nix-shell --run 'uv run pytest tests/test_desktop_native_config.py tests/test_desktop_command_journal.py -x -n 0 --no-cov'`
  - Expected: FAIL.
- [ ] **Step 3: Implement `NativeControllerEntry` / `v: 2` config and `user_version = 2` journal**
  - In `src/mindroom/desktop/native_config.py`: accept both `v: 1` and `v: 2` payloads; serialize as `v: 2` when `len(controllers) > 1` (or always `v: 2` while keeping `controller`/`allowed_requester_ids`/`allowed_agent_names` properties for Python callers); reject duplicate `(user_id, device_id, ed25519)` or duplicate `(user_id, device_id)` with mismatched `ed25519`.
  - In `src/mindroom/desktop/command_journal.py`: migrate `user_version = 1` → `2` inside a single SQLite transaction adding `controller_key TEXT NOT NULL` to `commands`, `sequences`, and `responses` with composite unique constraints `(controller_key, request_id)` and `(controller_key, session_id)`. Retire the single-controller refusal in `check_controller_binding` (keep the function as a validated no-op or schema check so existing callers don't break).
- [ ] **Step 4: Run targeted tests**
  - Run: `NIX_PATH=nixpkgs=flake:nixpkgs nix-shell --run 'uv run pytest tests/test_desktop_native_config.py tests/test_desktop_command_journal.py -x -n 0 --no-cov'`
  - Expected: PASS.
- [ ] **Step 5: Commit**
  - `git commit -m "feat(desktop): support multiple controllers in native config v2 and SQLite journal v2"` with `Assisted-by` trailer.

---

### Task 3: Multi-Controller Bridge, Per-Controller Live Grants, and Shell-First Startup

**Files:**
- Modify: `src/mindroom/desktop/bridge.py`
- Modify: `src/mindroom/desktop/bridge_session.py`
- Modify: `src/mindroom/desktop/shell.py`
- Modify: `src/mindroom/desktop/native_host.py`
- Test: `tests/test_desktop_bridge.py`, `tests/test_desktop_bridge_session.py`, `tests/test_desktop_shell.py`, `tests/test_desktop_native_host.py`

**Interfaces:**
- Consumes: `NativeControllerEntry`, `NativeDesktopConfig.controllers` (Task 2), `DesktopCommandJournal` `v2` (Task 2).
- Produces:
  - `DesktopControllerPolicy(controller: PinnedMatrixDevice, allowed_requester_ids: frozenset[str], allowed_agent_names: frozenset[str])` in `src/mindroom/desktop/bridge.py`.
  - `DesktopBridge.grant_control(duration_seconds=None, *, computer=False, controller_key: str | None = None)` and `revoke_control(controller_key: str | None = None)`.
  - `DesktopShell.grant(duration_seconds=None, *, until_revoked=False, controller_key: str | None = None)` and `revoke(controller_key: str | None = None)`.
  - `DesktopBridge.update_controllers(controllers: tuple[DesktopControllerPolicy, ...])` for live hot-reload on `upsert_controller` / `remove_controller`.
  - Native host actions: `upsert_controller`, `remove_controller`, optional `controller_key` on `grant_control`/`revoke_control`/`grant_shell`/`revoke_shell`, plus `controllers`, `gui_permissions_ready`, `shell_ready` in `status()`.

- [ ] **Step 1: Write failing tests**
  - In `tests/test_desktop_bridge.py`:
    - `test_two_controllers_per_controller_full_access_isolation`: grant computer control + shell auto-approve to controller A only; controller A executes `launch_app` and unprompted `run_shell`; controller B is refused GUI control and still requires shell approval. Revoking controller A revokes only A.
    - `test_responses_route_back_to_issuing_controller`: commands from controller A and controller B each send `DesktopResponse` back to their respective `PinnedMatrixDevice`.
  - In `tests/test_desktop_bridge_session.py`:
    - `test_shell_first_start_succeeds_without_gui_permissions`: config with `allowed_app_ids=("primary-screen",)` and `shell.enabled=True` starts cleanly when macOS Accessibility/Screen Recording report `"missing"`, executes `run_shell`, and returns a clear permission error on `get_app_state` / `screenshot`.
  - In `tests/test_desktop_native_host.py`:
    - `test_upsert_and_remove_controller_actions`: `upsert_controller` adds a 2nd controller (including while running, without dropping existing shell handles); `remove_controller` removes it and revokes its grants; `status()` exposes both legacy primary fields and `controllers[]`, `gui_permissions_ready`, `shell_ready`.
    - `test_grant_control_and_shell_accept_optional_controller_key`: passing `controller_key` scopes the grant; omitting it preserves existing single-controller behavior.
- [ ] **Step 2: Run tests to verify failure**
  - Run: `NIX_PATH=nixpkgs=flake:nixpkgs nix-shell --run 'uv run pytest tests/test_desktop_bridge.py tests/test_desktop_bridge_session.py tests/test_desktop_shell.py tests/test_desktop_native_host.py -k "isolation or issuing_controller or shell_first or upsert_and_remove or optional_controller_key" -x -n 0 --no-cov'`
  - Expected: FAIL.
- [ ] **Step 3: Implement multi-controller bridge, per-controller grants, and shell-first startup**
  - In `src/mindroom/desktop/bridge.py`: match incoming Olm `AuthenticatedToDeviceEvent` against all configured `controllers`, record `controller_key` in the journal, deliver responses to the matching `PinnedMatrixDevice`, track live control deadlines and computer-scope flags per `controller_key` (with `controller_key=None` applying to all configured controllers for backwards compatibility).
  - In `src/mindroom/desktop/shell.py`: track auto-approval deadlines per `controller_key` (plus global fallback when `controller_key=None`); `revoke(controller_key=...)` revokes only that controller's auto-approval, pending request, and handles (or all when `None`).
  - In `src/mindroom/desktop/bridge_session.py` and `native_host.py`: ensure `start` resolves all pinned controllers and does not fail on missing Accessibility/Screen Recording when building the bridge; surface `gui_permissions_ready` and `shell_ready` in `status()`; add `upsert_controller` and `remove_controller` actions.
- [ ] **Step 4: Run targeted tests**
  - Run: `NIX_PATH=nixpkgs=flake:nixpkgs nix-shell --run 'uv run pytest tests/test_desktop_bridge.py tests/test_desktop_bridge_session.py tests/test_desktop_shell.py tests/test_desktop_native_host.py -x -n 0 --no-cov'`
  - Expected: PASS.
- [ ] **Step 5: Commit**
  - `git commit -m "feat(desktop): add multi-controller bridge, per-controller grants, and shell-first startup"` with `Assisted-by` trailer.

---

### Task 4: Olm To-Device Pairing Protocol & Multi-Agent `/desktop setup <agent>`

**Files:**
- Modify: `src/mindroom/desktop/protocol.py`
- Modify: `src/mindroom/desktop/pairing.py`
- Modify: `src/mindroom/desktop/pairing_receiver.py`
- Modify: `src/mindroom/commands/desktop_commands.py`
- Modify: `src/mindroom/commands/handler.py`
- Modify: `src/mindroom/turn_policy.py`
- Test: `tests/test_desktop_protocol.py`, `tests/test_desktop_pairing.py`

**Interfaces:**
- Consumes: `claim_desktop_pairing`, `confirm_desktop_pairing`, `complete_desktop_pairing` (`src/mindroom/desktop/pairing.py`), `save_desktop_credentials` / `delete_desktop_credentials` (`src/mindroom/desktop/credentials.py`).
- Produces:
  - Protocol constants & strict dataclasses in `src/mindroom/desktop/protocol.py`:
    - `DESKTOP_SETUP_REQUEST_EVENT_TYPE = "io.mindroom.desktop.setup_request.v1"` (`DesktopSetupRequest`)
    - `DESKTOP_SETUP_OFFER_EVENT_TYPE = "io.mindroom.desktop.setup_offer.v1"` (`DesktopSetupOffer`)
    - `DESKTOP_PAIRING_CONFIRM_EVENT_TYPE = "io.mindroom.desktop.pairing_confirm.v1"` (`DesktopPairingConfirm`)
    - `DESKTOP_PAIRING_CONFIRMED_EVENT_TYPE = "io.mindroom.desktop.pairing_confirmed.v1"` (`DesktopPairingConfirmed`)
    - `DESKTOP_DISCONNECT_REQUEST_EVENT_TYPE = "io.mindroom.desktop.disconnect_request.v1"` (`DesktopDisconnectRequest`)
    - `DESKTOP_DISCONNECT_CONFIRMED_EVENT_TYPE = "io.mindroom.desktop.disconnect_confirmed.v1"` (`DesktopDisconnectConfirmed`)
  - Initiator-device binding on `desktop_pairings` (`initiator_device_id`, `initiator_ed25519` columns, migrated safely via `ALTER TABLE` if missing).
  - Full Olm pairing lifecycle in `DesktopPairingReceiver.on_event`.
  - `/desktop setup <agent_name>` support in rooms with multiple MindRoom agents (still rejecting rooms with another human).

- [ ] **Step 1: Write failing tests**
  - In `tests/test_desktop_protocol.py`: round-trip & strict validation tests for all 6 new event dataclasses (rejecting wrong version, blank nonce, oversized fields, invalid types).
  - In `tests/test_desktop_pairing.py`:
    - `test_olm_pairing_happy_path_setup_claim_confirm`: Chat device sends `setup_request.v1` → receives `setup_offer.v1` → helper sends `pairing_claim.v1` → receives `pairing_accepted.v1` → initiating Chat device sends `pairing_confirm.v1` → receives `pairing_confirmed.v1(ok=True)` and credentials are saved.
    - `test_olm_pairing_confirm_rejects_different_chat_device`: a second device of the same user sending `pairing_confirm.v1` gets `pairing_confirmed.v1(ok=False)` and credentials are NOT saved.
    - `test_olm_pairing_confirm_rejects_mismatched_device_key_or_verification`: mismatched `device_id`, `device_ed25519`, or `verification` is rejected.
    - `test_olm_disconnect_deletes_credentials`: `disconnect_request.v1` removes saved credentials for `(requester_id, agent_name)` and replies `disconnect_confirmed.v1(ok=True)`.
    - `test_slash_desktop_setup_named_agent_in_multi_agent_room_and_rejects_other_human`: `/desktop setup dev` works in a room with 2 agents and 1 human; still refuses when a 2nd human is in the room.
- [ ] **Step 2: Run tests to verify failure**
  - Run: `NIX_PATH=nixpkgs=flake:nixpkgs nix-shell --run 'uv run pytest tests/test_desktop_protocol.py tests/test_desktop_pairing.py -x -n 0 --no-cov'`
  - Expected: FAIL.
- [ ] **Step 3: Implement Olm pairing events, receiver handlers, and `/desktop setup <agent>`**
  - Add the 6 event constants and dataclasses to `src/mindroom/desktop/protocol.py` and `__all__`.
  - Extend `desktop_pairings` schema in `src/mindroom/desktop/pairing.py` with optional `initiator_device_id` and `initiator_ed25519` columns (`ALTER TABLE` migration if absent), and enforce initiator match in `confirm_desktop_pairing` when `initiator_device_id` is set.
  - Handle `setup_request.v1`, `pairing_confirm.v1`, and `disconnect_request.v1` in `DesktopPairingReceiver.on_event` (`src/mindroom/desktop/pairing_receiver.py`), verifying `authenticated_sender_is_current` on every event and replying via `send_encrypted_to_device`.
  - Update `desktop_room_agent` / `handle_desktop_command` (`src/mindroom/commands/desktop_commands.py`, `handler.py`, `turn_policy.py`) to accept an explicit `<agent_name>` target in rooms containing only the requester and MindRoom accounts (zero other humans).
- [ ] **Step 4: Run targeted tests**
  - Run: `NIX_PATH=nixpkgs=flake:nixpkgs nix-shell --run 'uv run pytest tests/test_desktop_protocol.py tests/test_desktop_pairing.py -x -n 0 --no-cov'`
  - Expected: PASS.
- [ ] **Step 5: Commit**
  - `git commit -m "feat(desktop): add Olm to-device pairing protocol and multi-agent /desktop targeting"` with `Assisted-by` trailer.

---

### Task 5: Dynamic Requester-Scoped `desktop` Tool Activation & `defaults.desktop_pairing` Policy

**Files:**
- Modify: `src/mindroom/config/models.py`
- Modify: `src/mindroom/desktop/pairing_receiver.py`
- Modify: `src/mindroom/commands/desktop_commands.py`
- Modify: `src/mindroom/bot.py`
- Modify: `src/mindroom/agents.py`
- Test: `tests/test_desktop_tool.py`, `tests/test_desktop_pairing.py`

**Interfaces:**
- Consumes: `DesktopPairingReceiver` (Task 4), `load_desktop_credentials` / `desktop_configuration_state` (`src/mindroom/desktop/credentials.py`, `configuration.py`).
- Produces:
  - `DefaultsConfig.desktop_pairing: Literal["self_service", "explicit_only", "disabled"] = "self_service"` (`src/mindroom/config/models.py`).
  - Helper `desktop_pairing_eligible(config: Config, agent_name: str) -> str | None` used consistently by `pairing_receiver.py`, `desktop_commands.py`, and `agents.py`.
  - Dynamic injection of `desktop` toolkit in `src/mindroom/agents.py` (`_assemble_agent_tools`) when `self_service` is enabled and the current `requester_id` has READY saved desktop credentials for `agent_name`.

- [ ] **Step 1: Write failing tests**
  - In `tests/test_desktop_tool.py`:
    - `test_dynamic_desktop_tool_absent_when_unpaired_and_injected_when_paired`: an agent without `"desktop"` in `tools:` does NOT include `desktop` in assembled tools when unpaired, DOES include `desktop` for a requester who has READY saved credentials, and still omits it for a different unpaired requester.
    - `test_explicit_only_and_disabled_policies_block_dynamic_injection`: with `defaults.desktop_pairing = "explicit_only"` or `"disabled"`, an agent without `"desktop"` in `tools:` never receives dynamic injection; an authored `persona` with narrowed tools excluding `desktop` is never bypassed.
  - In `tests/test_desktop_pairing.py`:
    - `test_self_service_registers_pairing_receiver_without_static_desktop_tool` and `test_setup_request_reports_ineligible_when_policy_disabled_or_explicit_only`.
- [ ] **Step 2: Run tests to verify failure**
  - Run: `NIX_PATH=nixpkgs=flake:nixpkgs nix-shell --run 'uv run pytest tests/test_desktop_tool.py tests/test_desktop_pairing.py -k "dynamic_desktop or explicit_only or self_service or ineligible" -x -n 0 --no-cov'`
  - Expected: FAIL.
- [ ] **Step 3: Implement `desktop_pairing` policy and dynamic tool activation**
  - Add `desktop_pairing: Literal["self_service", "explicit_only", "disabled"] = "self_service"` to `DefaultsConfig` in `src/mindroom/config/models.py`.
  - Implement `desktop_pairing_eligible(config, agent_name)` in `src/mindroom/commands/desktop_commands.py` (or a shared desktop eligibility helper) and reuse it in `chat_pairing_desktop_error`, `register_desktop_pairing_receiver`, and `DesktopPairingReceiver`.
  - In `src/mindroom/agents.py` (`_assemble_agent_tools`): when `config.defaults.desktop_pairing == "self_service"`, `"desktop"` not already resolved, not in `disabled_tool_names`, and `narrowed_tools` is `None` (so narrowed personas stay narrowed), check `execution_identity.requester_id` credentials via `load_desktop_credentials` + `desktop_configuration_state`; if `READY`, attach `desktop` toolkit.
- [ ] **Step 4: Run targeted tests**
  - Run: `NIX_PATH=nixpkgs=flake:nixpkgs nix-shell --run 'uv run pytest tests/test_desktop_tool.py tests/test_desktop_pairing.py -x -n 0 --no-cov'`
  - Expected: PASS.
- [ ] **Step 5: Commit**
  - `git commit -m "feat(desktop): activate desktop tool dynamically per paired requester"` with `Assisted-by` trailer.

---

### Task 6: Signed Helper Release Artifact & Full Verification

**Files:**
- Modify: `.github/workflows/release.yml`
- Modify: `tach.toml` (only if `tach check` requires a new edge)
- Test: `tests/test_desktop_native_packaging.py`

**Interfaces:**
- Produces: GitHub Release assets `MindRoom-Desktop-Helper-arm64.tar.gz` and `MindRoom-Desktop-Helper-arm64.tar.gz.sha256` produced from the signed, verified `MindRoom Desktop Helper.app` inside `build_macos_app`.

- [ ] **Step 1: Write failing test**
  - In `tests/test_desktop_native_packaging.py`: `test_release_workflow_publishes_signed_desktop_helper_tarball_and_sha256` — parse `.github/workflows/release.yml` and verify `build_macos_app` archives the signed `MindRoom Desktop Helper.app` into `MindRoom-Desktop-Helper-arm64.tar.gz`, generates `MindRoom-Desktop-Helper-arm64.tar.gz.sha256`, and uploads both to the GitHub release.
- [ ] **Step 2: Run test to verify failure**
  - Run: `NIX_PATH=nixpkgs=flake:nixpkgs nix-shell --run 'uv run pytest tests/test_desktop_native_packaging.py -x -n 0 --no-cov'`
  - Expected: FAIL.
- [ ] **Step 3: Update `.github/workflows/release.yml`**
  - After building and verifying `MindRoom.app` (which signs and verifies `Contents/Helpers/MindRoom Desktop Helper.app` with Developer ID + notarizes via the DMG/zip flow), tar-gzip the signed & stapled `MindRoom Desktop Helper.app` to `dist/macos/MindRoom-Desktop-Helper-arm64.tar.gz`, write `shasum -a 256` to `.sha256`, and include both in the `gh release upload` asset list.
- [ ] **Step 4: Full Phase 1 verification**
  - Run all desktop tests (default addopts): `NIX_PATH=nixpkgs=flake:nixpkgs nix-shell --run 'uv run pytest tests/test_desktop*.py -x --no-cov'`
  - Run architecture & lint checks:
    - `NIX_PATH=nixpkgs=flake:nixpkgs nix-shell --run 'uv run --with tach tach check --dependencies --interfaces'`
    - `NIX_PATH=nixpkgs=flake:nixpkgs nix-shell --run 'uv run ruff check src/mindroom/desktop src/mindroom/commands/desktop_commands.py src/mindroom/agents.py src/mindroom/config/models.py tests/test_desktop*.py'`
    - `NIX_PATH=nixpkgs=flake:nixpkgs nix-shell --run 'uv run ty check'`
- [ ] **Step 5: Commit**
  - `git commit -m "ci(macos): publish signed MindRoom-Desktop-Helper-arm64 tarball and checksum on releases"` with `Assisted-by` trailer.
