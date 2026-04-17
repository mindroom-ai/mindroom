# ISSUE-152 Report

## Changes

Added `accept_invites` to `AgentConfig` with a default of `True`.
Added invite acceptance and invited-room persistence helpers in `src/mindroom/bot.py`.
Persisted invited rooms only for named non-router agents whose `accept_invites` setting remains enabled.
Preserved router and team invite acceptance behavior.
Restored the default invite-acceptance path for entities without an explicit `AgentConfig`.
Switched invited-room persistence to `safe_replace()` so cross-device Docker and NixOS storage keeps the file durable.
Extended configured-room resolution to include persisted invited rooms for opted-in agents so orphan cleanup does not kick them after restart.
Updated invite and cleanup tests to cover opt-out, persistence, cleanup preservation, and team invite regression behavior.
Round 3 reverted the earlier persisted-room lookup from `src/mindroom/config/main.py`.
Round 3 now preloads all persisted invited rooms once inside `src/mindroom/matrix/room_cleanup.py` with `_load_all_persisted_invited_rooms()` before orphan cleanup decisions are made.
That keeps configured-room resolution limited to static config while still preserving ad-hoc invited rooms across service restarts.
Updated `tests/test_room_invites.py`, `tests/test_multi_agent_bot.py`, and `tests/test_dm_room_preservation.py` to cover the preload path.

## Validation

Ran `export NIX_PATH=nixpkgs=/nix/var/nix/profiles/per-user/root/channels/nixos && nix-shell --run 'uv run pytest tests/test_room_invites.py tests/test_multi_agent_bot.py -x -n 0 --no-cov -v'`.
That run passed with `188 passed, 4 skipped, 1 warning in 36.13s`.
Ran `export NIX_PATH=nixpkgs=/nix/var/nix/profiles/per-user/root/channels/nixos && nix-shell --run 'uv run pytest tests/test_team_invitations.py -x -n 0 --no-cov -v'`.
That run passed with `3 passed, 1 warning in 0.49s`.
Ran `export NIX_PATH=nixpkgs=/nix/var/nix/profiles/per-user/root/channels/nixos && nix-shell --run 'uv run pytest tests/test_room_invites.py tests/test_multi_agent_bot.py tests/test_team_invitations.py tests/test_dm_functionality.py -x -n 0 --no-cov -v'`.
That run passed with `200 passed, 4 skipped, 1 warning in 36.43s`, including `tests/test_dm_functionality.py::TestDMIntegration::test_agent_accepts_dm_invites`.
Ran `export NIX_PATH=nixpkgs=/nix/var/nix/profiles/per-user/root/channels/nixos && nix-shell --run 'uv run pytest tests/test_room_invites.py tests/test_dm_functionality.py tests/test_team_invitations.py tests/test_multi_agent_bot.py tests/test_dm_room_preservation.py -x -n 0 --no-cov 2>&1'`.
That run passed with `210 passed, 4 skipped, 1 warning in 36.97s`.
