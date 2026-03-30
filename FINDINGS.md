# Findings

- Added per-`(room_id, thread_id)` response lifecycle locks in [`src/mindroom/bot.py`](/srv/mindroom-worktrees/issue-045-on-pr429/src/mindroom/bot.py) and wrapped agent responses, skill command responses, and the shared team response helper so a same-thread turn cannot start prompt preparation until the previous turn fully finishes.
- Updated [`src/mindroom/custom_tools/compact_context.py`](/srv/mindroom-worktrees/issue-045-on-pr429/src/mindroom/custom_tools/compact_context.py) so `compact_context` reports queued compaction honestly instead of claiming the session is already compacted.
- Added regression coverage in [`tests/test_compact_context.py`](/srv/mindroom-worktrees/issue-045-on-pr429/tests/test_compact_context.py) that queues compaction through the tool during a first turn, starts a second turn before apply, and asserts the second turn only sees the compacted session after the first turn completes.

# Verification

- `export NIX_PATH=nixpkgs=/nix/var/nix/profiles/per-user/root/channels/nixos && nix-shell --run 'uv run pytest tests/test_compact_context.py -x -n 0 --no-cov -v'`
- `export NIX_PATH=nixpkgs=/nix/var/nix/profiles/per-user/root/channels/nixos && nix-shell --run 'uv run ruff check src/'`
- `export NIX_PATH=nixpkgs=/nix/var/nix/profiles/per-user/root/channels/nixos && nix-shell --run 'uv run pytest tests/ -x -n 0 --no-cov -v'`
- `export NIX_PATH=nixpkgs=/nix/var/nix/profiles/per-user/root/channels/nixos && nix-shell --run 'uv run pre-commit run --all-files'`
