# ISSUE-152 Plan

## Scope

Add `accept_invites: bool = Field(default=True)` to `AgentConfig` immediately after `rooms`.
Keep all invite persistence logic in `src/mindroom/bot.py`.
Do not change `_post_join_room_setup()`, `ensure_rooms()`, or `start()`.

## Bot Changes

Add `_should_accept_invite()` so the router and teams always accept invites, named agents accept unless `accept_invites` is `False`, and unknown entities reject invites.
Add `_invited_rooms_path()`, `_load_invited_rooms()`, and `_save_invited_rooms()` to `AgentBot`.
Store `self._invited_rooms: set[str]` in `AgentBot.__init__` and load it once from disk there.
Use `agent_state_root_path()` for the persisted path so entity names are normalized consistently.
Write invited room state atomically from `self._invited_rooms` instead of using load-add-save cycles.

## Invite Flow

Gate `_on_invite()` through `_should_accept_invite()`.
After a successful join, persist the room only for named non-router agents whose `accept_invites` is `True`.
Keep existing router and team post-join behavior unchanged.

## Cleanup Flow

In `leave_unconfigured_rooms()`, union `self._invited_rooms` into `configured_rooms` only for eligible named agents.
Do not protect teams or the router with invited-room persistence.

## Tests

In `tests/test_room_invites.py`, cover an opted-out agent refusing a non-DM invite, an opted-in agent persisting a non-DM invited room, and cleanup preserving a persisted non-DM invited room.
In `tests/test_multi_agent_bot.py`, patch `mindroom.bot.join_room` and assert `_on_invite()` follows the new acceptance gate.
In `tests/test_team_invitations.py`, add a regression test that `TeamBot` still accepts invites without an `AgentConfig`.
