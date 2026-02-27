# Public Matrix + `uvx mindroom` Onboarding Plan

## Context

We want users to run MindRoom locally with very low setup overhead:

- MindRoom backend runs on the user's machine (`uvx mindroom`).
- Shared public Matrix homeserver: `https://mindroom.chat`.
- Shared public chat UI: `https://chat.mindroom.chat`.

### Important constraint (anti-spam policy)

The public homeserver is intentionally configured to allow **human account registration only via GitHub, Apple, or Google**. This is good for spam prevention, but it introduces a bootstrapping problem:

- MindRoom currently auto-creates agent accounts programmatically.
- Agents cannot complete interactive OAuth signup flows.
- Current account creation path expects password-style registration and can fail on OAuth-only/public setups.

So the design has to preserve anti-spam protections for humans while still allowing trusted machine-created agent accounts.

## Problem Statement

`uvx mindroom` should support a first-class "public Matrix" mode that:

1. Works without running local Synapse/Cinny.
2. Keeps human anti-spam protections intact.
3. Securely provisions and persists agent identities.
4. Avoids collisions between many independent local MindRoom installs.

## Goals

- One-command public bootstrap (`mindroom config init --profile public`).
- Secure account lifecycle (no predictable usernames/passwords).
- Stable persisted mapping from internal entity name (`agent`, `team`, `router`, `user`) to Matrix localpart.
- Compatibility with homeservers that require `m.login.registration_token`.
- Clear diagnostics in `mindroom doctor`.

## Non-Goals (for this change)

- Reworking homeserver auth policy for human users.
- Replacing Matrix-nio client stack.
- Building a full managed multi-tenant control plane in this PR.

## Current Gaps

1. `config init` has no public profile.
2. Usernames/passwords are deterministic in account bootstrap paths.
3. Startup assumes simple registration behavior.
4. Managed room aliases are global (`lobby`, etc.) and can collide on shared homeserver.
5. Some identity logic assumes `@mindroom_<agent>:domain` instead of reading persisted mapping.
6. Widget defaults still point to local URLs.

## Proposed Approach

### 1) Add profile-based config init

Add profile selection:

- `full` (current rich default behavior)
- `minimal` (existing minimal behavior)
- `public` (new)

Public profile writes `.env` defaults such as:

- `MATRIX_HOMESERVER=https://mindroom.chat`
- `MATRIX_SERVER_NAME=mindroom.chat`
- `MATRIX_REGISTRATION_TOKEN=` (required placeholder)
- default widget URL based on hosted UI

### 2) Keep anti-spam policy, add machine bootstrap path

Humans remain OAuth-only (GitHub/Apple/Google).
Agents use a **separate trusted registration channel** via Matrix registration token flow:

- Use `m.login.registration_token` during account creation.
- Read token from env (`MATRIX_REGISTRATION_TOKEN`).
- Fail with clear error if homeserver demands token and none is configured.

This keeps public signups restricted while allowing controlled bot provisioning.

### 3) Fix credentials and identity persistence

- Generate strong random passwords via `secrets`.
- Keep (and rely on) persisted mapping in `matrix_state.yaml`:
  - account key (`agent_<name>`) -> `{username, password}`
- Never regenerate usernames for already-provisioned entities.

### 4) Add per-install namespace

For public profile, generate a stable instance namespace (for example, `mr_x7k2`) and use it in:

- agent/team/router localparts
- managed room aliases

This prevents collisions between separate user installs on the same public server.

### 5) Reduce deterministic ID assumptions

Audit code paths that currently use `config.ids` or `MatrixID.from_agent(...)` as source of truth for runtime identities.

Where runtime identity matters (invites, mention matching, room policy), resolve IDs from persisted account mapping instead of deterministic derivation.

### 6) Public UX and doctor checks

- `!widget` default URL should target hosted UI in public mode.
- `mindroom doctor` should detect:
  - homeserver reachable
  - registration flow requirements
  - missing/invalid registration token

## Implementation Plan

## Phase 1: Public profile scaffolding

- Update `mindroom config init` with profile option.
- Keep backward compatibility for `--minimal`.
- Add public-profile `.env` template values.
- Update tests for init/profile behavior.

## Phase 2: Secure account generation + token registration

- Refactor registration call to support `auth_dict` for token flow.
- Add env-driven registration token handling.
- Replace deterministic password generation with random secrets.
- Update account creation tests.

## Phase 3: Namespace and collision handling

- Add stable per-install namespace storage/config.
- Prefix generated localparts and room aliases in public mode.
- Update room creation/invite resolution tests.

## Phase 4: Runtime identity correctness

- Replace deterministic runtime ID assumptions with persisted mapping where required.
- Update mentions/invitations/authorization checks as needed.
- Add regression tests around mixed/domain/public scenarios.

## Phase 5: Doctor + UX polish

- Add doctor checks for registration requirements and token presence.
- Set public widget defaults.
- Update CLI help and relevant docs.

## Key Files (expected)

- `src/mindroom/cli_config.py`
- `src/mindroom/cli.py`
- `src/mindroom/constants.py`
- `src/mindroom/matrix/client.py`
- `src/mindroom/matrix/users.py`
- `src/mindroom/matrix/identity.py`
- `src/mindroom/matrix/rooms.py`
- `src/mindroom/config.py`
- `src/mindroom/commands.py`
- tests in `tests/test_cli_config.py`, `tests/test_cli.py`, `tests/test_matrix_agent_manager.py`, plus room/authorization tests

## Risks and Mitigations

- Risk: breaking existing self-hosted deterministic assumptions.
  - Mitigation: profile-gated behavior for public mode, plus migration-safe fallback to existing stored accounts.

- Risk: token leaks in logs/config.
  - Mitigation: never print token values; only print presence/absence.

- Risk: partial identity migration causing invite/mention mismatches.
  - Mitigation: prioritize runtime identity source-of-truth audit before final merge.

## Definition of Done

- `mindroom config init --profile public` yields runnable public config.
- Agents bootstrap on `mindroom.chat` using registration-token flow.
- No deterministic passwords remain in account provisioning.
- No account/room alias collisions across independent installs in public mode.
- `mindroom doctor` gives actionable token/registration diagnostics.
- `pytest` passes with new and updated coverage.
