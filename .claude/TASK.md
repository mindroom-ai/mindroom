# Fix 3 bugs found in Round 3 live test validation

These bugs were found by running the exhaustive live test checklist (`docs/dev/exhaustive-live-test-checklist.md`) against a live MindRoom instance with Tuwunel homeserver.

## Bug 1: CONF-003 — Removed agent's Matrix member not cleaned from room

When an agent is removed from `config.yaml` and hot-reloaded, the agent stops responding (correct), but its stale Matrix user (e.g., `@mindroom_helper_*`) remains in the room membership list. The member should be kicked/removed during reconciliation.

**Where to look:** `src/mindroom/orchestration/` — the hot-reload/reconciliation path that handles agent removal. When an agent is removed from config, it should also kick the agent's Matrix user from all rooms it was in.

**Evidence:** `live-test-results/section-02/` (from Round 3 worktree at `/srv/mindroom-worktrees/val3-s2/evidence/`)

## Bug 2: ROOM-006 — Orphan cleanup misidentifies router as orphaned

The room cleanup logic (`src/mindroom/matrix/room_cleanup.py` or `rooms.py`) incorrectly classifies the configured router bot in the managed root space as an "orphan" and attempts to kick it. The kick only fails because Matrix rejects self-kicking (`M_FORBIDDEN: You cannot kick yourself`).

This was also found in Round 1 and partially fixed, but the fix is incomplete — the router in the **root space** is still misidentified.

**Where to look:** `src/mindroom/matrix/room_cleanup.py` and `src/mindroom/matrix/rooms.py` — the orphan detection logic needs to exclude the router bot from orphan candidates, especially in the root space.

**Evidence:** `/srv/mindroom-worktrees/val3-s3/evidence/val3_s3/`

## Bug 3: CMD-009 — Interactive menu/reaction prompt completely broken

The interactive command prompt path (where MindRoom posts a menu and waits for emoji reactions to select options) does not work. The menu agent never establishes a working interactive question — no `Menu choice:` follow-up is produced even after sending the correct reaction emoji.

**Where to look:** The interactive prompt/reaction handling code. Check how reaction events are processed and matched to pending interactive prompts.

**Evidence:** `/srv/mindroom-worktrees/val3-s7/evidence/`

## Instructions

1. Fix all 3 bugs
2. Add regression tests for each fix
3. Each fix should be a separate commit with a clear message
4. Write results to `.claude/REPORT.md` with:
   - Summary of each fix
   - Files changed
   - Test results

## Environment

- Python deps: use `uv run` (no pip)
- Run tests: `uv run pytest tests/ -x -q`
- Homeserver: Tuwunel on localhost:8008
- Do NOT push (no git push)
