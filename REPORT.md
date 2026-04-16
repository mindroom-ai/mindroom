# ISSUE-152 Report

## Changes

Added `accept_invites` to `AgentConfig` with a default of `True`.
Added invite acceptance and invited-room persistence helpers in `src/mindroom/bot.py`.
Persisted invited rooms only for named non-router agents whose `accept_invites` setting remains enabled.
Preserved router and team invite acceptance behavior.
Updated invite and cleanup tests to cover opt-out, persistence, cleanup preservation, and team invite regression behavior.

## Validation

Ran `export NIX_PATH=nixpkgs=/nix/var/nix/profiles/per-user/root/channels/nixos && nix-shell --run 'uv run pytest tests/test_room_invites.py tests/test_multi_agent_bot.py -x -n 0 --no-cov -v'`.
That run passed with `188 passed, 4 skipped, 1 warning in 36.13s`.
Ran `export NIX_PATH=nixpkgs=/nix/var/nix/profiles/per-user/root/channels/nixos && nix-shell --run 'uv run pytest tests/test_team_invitations.py -x -n 0 --no-cov -v'`.
That run passed with `3 passed, 1 warning in 0.49s`.
