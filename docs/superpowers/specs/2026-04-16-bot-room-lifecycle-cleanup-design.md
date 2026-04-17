# Bot Room Lifecycle Cleanup Design

**Goal**

Reduce `src/mindroom/bot.py` to the Matrix runtime shell for startup, sync, and high-level delegation.
Move the PR-specific room membership and invite persistence behavior into one focused collaborator.

**Scope**

This cleanup is limited to the room and invite lifecycle added or expanded by the current PR.
It does not change response generation, turn handling, delivery, or orchestrator responsibilities.
It does not change invite policy semantics or room membership behavior.

**Current Problem**

`AgentBot` currently owns configured room joins, unconfigured room cleanup, invite acceptance policy, invited-room persistence, router-only post-join setup, and welcome message handling.
That logic is orthogonal to the rest of the runtime shell and makes `bot.py` grow even after earlier extractions.
The new invited-room persistence path also forces `bot.py` to own JSON and atomic file-write details that belong closer to the persistence helper.

**Proposed Design**

Introduce a focused room lifecycle collaborator under `src/mindroom/` that owns room membership and invite handling for one bot runtime.
Keep `AgentBot` responsible for startup, sync callbacks, shutdown, and delegating room lifecycle work to the collaborator.
Move invited-room save mechanics into `src/mindroom/matrix/invited_rooms_store.py` so the runtime shell no longer owns file-format details.

**Responsibilities of the New Collaborator**

The collaborator owns configured room joins.
The collaborator owns unconfigured room leaves while preserving persisted invited rooms and the router root Space exception.
The collaborator owns invite acceptance checks, sender authorization checks, room joins for accepted invites, and invited-room persistence updates.
The collaborator owns router-only post-join actions through explicit callbacks supplied by `AgentBot`.
The collaborator owns the welcome-message-on-empty-room helper because it is part of room lifecycle behavior.

**Responsibilities Remaining in AgentBot**

`AgentBot` still creates the Matrix client, registers event callbacks, tracks sync health, owns background tasks, and exposes stable public methods used by tests and orchestrator code.
`AgentBot` keeps thin delegation methods for `join_configured_rooms`, `leave_unconfigured_rooms`, `ensure_rooms`, and `_on_invite` so existing call sites do not need to change.

**Files**

Modify `src/mindroom/bot.py` to delegate room and invite lifecycle behavior.
Add `src/mindroom/bot_room_lifecycle.py` for the extracted collaborator.
Modify `src/mindroom/matrix/invited_rooms_store.py` to add save support.
Update invite and room membership tests only as needed for the new helper boundaries.

**Testing**

Keep the existing invite and room-membership tests passing.
Run focused pytest coverage for `tests/test_room_invites.py`, `tests/test_team_invitations.py`, `tests/test_multi_agent_bot.py`, and `tests/test_dm_functionality.py` for the affected cases.
