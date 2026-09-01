# Minimal Live Invite Bootstrap Implementation Plan

> Execute this plan task by task with test-first changes and verification checkpoints.
> This plan and `invite-acceptance-contract.md` are temporary development artifacts and must be deleted in the final commit before merge.

**Goal:** Allow only the router to use the exact sender of a fresh authenticated self-invite as a narrow pre-join `current_room_members` bootstrap, while preserving existing ordinary invite recovery and avoiding new lifecycle machinery.

**Architecture:** Start from `origin/main`.
Keep the existing durable pending-invite store unchanged for ordinary authorization and recovery.
Add one immutable process-local token per live invite callback.
Let `BotRoomLifecycle` validate the token, perform one join, re-read current authorization after the join, and persist acceptance only after final validation.
Never compensate by leaving a room.

**Tech stack:** Python 3.13, asyncio, matrix-nio, pytest, pytest-xdist, pre-commit.

**Authoritative contract:** `docs/dev/invite-acceptance-contract.md`

## Progress

- [x] Task 1 completed from current `origin/main`; the initial focused surface passed with 60 tests.
- [x] Task 2 completed with a router-only pure authorization rule.
- [x] Task 3 completed with authenticated self-invite admission and one process-local token.
- [x] Task 4 completed with final authorization, replacement safety, and no compensating leave.
- [x] Task 5 completed with production edits limited to the three scoped files and no new durable state or retry owner.
- [ ] Task 6 is in progress; the implementation commit was created after 73 focused tests, all repository hooks, and the full suite with 15,446 passed and 23 skipped, while push and independent reviews remain.

## Global constraints

- Restrict production changes to `src/mindroom/authorization.py`, `src/mindroom/bot_room_lifecycle.py`, and `src/mindroom/bot.py`.
- Do not change persistence schemas, room cleanup, configuration publication, the orchestrator, or sync continuity.
- Keep the runtime change to one callback token flowing through the existing room lock and join owner.
- Use the existing per-room invite lock.
- Run every pytest command with `-n auto`.
- Preserve unrelated work and never stage with `git add .` or `git add -A`.
- Update the contract before implementation if a required guarantee or accepted failure changes.

## Task 1: Establish the clean baseline

**Files:**
- Add: `docs/dev/invite-acceptance-contract.md`
- Add: `docs/dev/invite-acceptance-implementation-plan.md`

1. Create a persistent worktree and branch from `origin/main`.
2. Run `uv sync --all-extras`.
3. Copy this contract and plan into the new worktree with `apply_patch`.
4. Run the baseline invite and authorization suites.

Command:

```bash
uv run pytest -n auto --no-cov -q tests/test_authorization.py tests/test_room_invites.py
```

5. Record the baseline result before changing runtime code.

## Task 2: Define the narrow authorization rule

**Files:**
- Modify: `tests/test_authorization.py`
- Modify: `src/mindroom/authorization.py`

1. Add failing tests for a pure helper named `allows_live_inviter_bootstrap`.
2. Prove that it returns true only for the router when the current configuration enables `current_room_members`.
3. Prove that it returns false for agents, teams, and the router when that policy is disabled.
4. Run the focused authorization tests and confirm the new tests fail for the missing helper.
5. Implement the helper using `resolve_responder_access` and `ROUTER_AGENT_NAME`.
6. Run the focused authorization tests again.

Command:

```bash
uv run pytest -n auto --no-cov -q tests/test_authorization.py
```

## Task 3: Admit only authenticated live invite callbacks

**Files:**
- Modify: `tests/test_room_invites.py`
- Modify: `src/mindroom/bot_room_lifecycle.py`
- Modify: `src/mindroom/bot.py`

1. Add failing tests proving that stripped room metadata, an invite for another state key, and a non-invite membership event create no pending work and no token.
2. Add a frozen `LiveRoomInvite` dataclass whose unique process-local identity represents one room and sender callback generation.
3. Add `record_live_room_invite(room_id, sender_id) -> LiveRoomInvite`.
4. Persist the existing pending record first, then replace the room's process-local token synchronously.
5. Change the authenticated self-invite callback to create the live token and pass it to `handle_recorded_invite`.
6. Keep reconciliation calls tokenless so durable or cache-only work cannot bootstrap.
7. Add sender-aware pending deletion so old work cannot remove a newer sender's durable record.
8. Run the focused callback and invite tests.

Command:

```bash
uv run pytest -n auto --no-cov -q tests/test_room_invites.py -k "invite and (callback or metadata or state_key or membership or pending)"
```

## Task 4: Join and validate without compensating leave

**Files:**
- Modify: `tests/test_room_invites.py`
- Modify: `src/mindroom/bot_room_lifecycle.py`

1. Add failing tests for the exact fresh router inviter using `current_room_members`.
2. Add failing tests proving that agents, teams, durable recovered records, and cache-only records cannot use the bootstrap.
3. Add a failing test proving that normal nio invite-cache removal after a successful join does not revoke the process-local token.
4. Add a failing test proving that a same-sender replacement callback creates a newer generation and prevents old work from persisting or deleting the newer pending record.
5. Add failing tests proving that configuration revocation and inviter departure during `join` prevent accepted-room persistence.
6. Add assertions that final rejection never calls Matrix leave.
7. Under the existing room lock, evaluate ordinary authorization first and use the bootstrap helper only for the current live token.
8. After a successful join, re-read `self._config()` and re-evaluate ordinary authorization.
9. If current policy still permits the live router bootstrap, call the existing authoritative `get_room_members` helper and require the inviter to be joined.
10. Confirm the token is still current before setup and persistence.
11. On final rejection, delete only pending work owned by the current token and return without calling leave.
12. On success, retain the existing setup, accepted-room persistence, handled marker, welcome, and pending deletion ordering.
13. Run all invite and authorization tests.

Command:

```bash
uv run pytest -n auto --no-cov -q tests/test_authorization.py tests/test_room_invites.py
```

## Task 5: Check scope and documentation

**Files:**
- Modify only invite-related documentation already changed by the replacement patch, if necessary.

1. Review every changed line against the authoritative contract.
2. Remove tests or code that promise accepted failure recovery.
3. Confirm no production file outside the three-file allowlist changed.
4. Measure runtime source size.

Commands:

```bash
git diff --name-only origin/main
git diff --numstat origin/main -- src
git diff --check
```

5. Use source size as a diagnostic and stop if the change introduces another owner, durable state, retry path, cleanup branch, or production file.

## Task 6: Verify and prepare the exact review head

1. Run the complete affected test surface.

Command:

```bash
uv run pytest -n auto --no-cov -q tests/test_authorization.py tests/test_room_invites.py tests/test_bot_ready_hook.py tests/test_matrix_sync_continuity.py tests/test_multi_agent_bot.py tests/test_team_invitations.py
```

2. Run repository hooks.

Command:

```bash
uv run pre-commit run --all-files
```

3. Run the full repository test suite if the focused surface and hooks pass.

Command:

```bash
uv run pytest -n auto
```

4. Run `git status --short` before staging.
5. Scan proposed public content and generated references for forbidden private identifiers.
6. Stage only the intended files by explicit path and verify the staged diff.
7. Create a new commit without amending.
8. Push only after the exact local head passes verification.
9. Request two fresh independent full reviews against the contract on the same exact head.
10. Treat findings about explicitly accepted failures as scope proposals, not implementation defects.
