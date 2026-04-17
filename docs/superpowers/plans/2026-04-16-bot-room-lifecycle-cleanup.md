# Bot Room Lifecycle Cleanup Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Move room membership and invite persistence behavior out of `src/mindroom/bot.py` without changing runtime behavior.

**Architecture:** Add a focused room lifecycle collaborator that owns configured joins, cleanup leaves, invite handling, router post-join work, and welcome-message checks. Keep `AgentBot` as the runtime shell and make `invited_rooms_store.py` the single owner of invited-room persistence mechanics.

**Tech Stack:** Python, matrix-nio, pytest

---

### Task 1: Capture the extraction boundary

**Files:**
- Modify: `docs/superpowers/specs/2026-04-16-bot-room-lifecycle-cleanup-design.md`
- Modify: `docs/superpowers/plans/2026-04-16-bot-room-lifecycle-cleanup.md`

- [ ] **Step 1: Confirm the touched runtime surface**

Verify that only room and invite lifecycle methods move.
Keep response, turn, sync, and delivery code out of scope.

- [ ] **Step 2: Preserve the public bot surface**

Keep `AgentBot.join_configured_rooms(...)`, `AgentBot.leave_unconfigured_rooms(...)`, `AgentBot.ensure_rooms(...)`, and `AgentBot._on_invite(...)` callable so tests and orchestrator code do not need broader rewiring.

### Task 2: Extract room lifecycle ownership

**Files:**
- Create: `src/mindroom/bot_room_lifecycle.py`
- Modify: `src/mindroom/bot.py`
- Modify: `src/mindroom/matrix/invited_rooms_store.py`

- [ ] **Step 1: Move invited-room persistence into the store helper**

Add a `save_invited_rooms(path, room_ids)` helper in `src/mindroom/matrix/invited_rooms_store.py`.
Remove JSON and atomic write plumbing from `src/mindroom/bot.py`.

- [ ] **Step 2: Add the room lifecycle collaborator**

Implement a focused helper that owns:
- configured room joins
- router post-join setup callback execution
- empty-room welcome checks
- unconfigured room cleanup with persisted invited-room preservation
- invite acceptance, authorization, join, and persistence updates

- [ ] **Step 3: Delegate from AgentBot**

Construct the collaborator in `AgentBot.__init__(...)`.
Replace the moved methods in `AgentBot` with thin delegation wrappers.

### Task 3: Verify the PR behavior stays fixed

**Files:**
- Modify only the files above unless a focused test adjustment is required
- Test: `tests/test_room_invites.py`
- Test: `tests/test_team_invitations.py`
- Test: `tests/test_multi_agent_bot.py`
- Test: `tests/test_dm_functionality.py`

- [ ] **Step 1: Run focused invite and membership tests**

Run:
```bash
uv run pytest tests/test_room_invites.py tests/test_team_invitations.py tests/test_dm_functionality.py tests/test_multi_agent_bot.py -k 'invite or invited_rooms or join_configured_rooms or leave_unconfigured_rooms' -x -n 0 --no-cov -v
```

Expected:
- PASS

- [ ] **Step 2: Run targeted full-file verification if needed**

If the focused selector misses a touched path, run:
```bash
uv run pytest tests/test_room_invites.py tests/test_team_invitations.py tests/test_dm_functionality.py -x -n 0 --no-cov -v
```

Expected:
- PASS
