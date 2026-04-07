# Implement Report

## Fixed

- Updated mention parsing in `src/mindroom/matrix/mentions.py` to treat the optional `@mindroom_` prefix case-insensitively.
- Reworked `_mention_candidate_names()` to try exact and namespace-stripped names before prefix-reconstructed variants.
- Added the missing `prefix + stripped_name` candidate so namespaced mentions like `@mindroom_dev_a1b2c3d4` resolve to config key `mindroom_dev`.
- Tightened the new mention tests to use exact processed-text assertions instead of substring checks.
- Added `_bind_config()` in `tests/test_mentions.py` to remove repeated config-binding boilerplate in the custom-agent tests.
- Added coverage for namespaced `mindroom_`-prefixed agents and uppercase prefixed mentions.

## False Positive

- Reviewer B-1's stated mechanism was not correct because `_find_matching_agent_name()` already compares candidates and config keys case-insensitively.
- The real gap was earlier in parsing because the regex treated `mindroom_` case-sensitively, so `@MINDROOM_calculator` did not follow the same candidate path as `@mindroom_calculator`.

## Validation

- Ran `export NIX_PATH=nixpkgs=/nix/var/nix/profiles/per-user/root/channels/nixos && nix-shell --run 'uv run pytest tests/test_mentions.py -x -n 0 --no-cov -v'`.
- Result: `21 passed, 1 warning in 4.62s`.
- Ran `export NIX_PATH=nixpkgs=/nix/var/nix/profiles/per-user/root/channels/nixos && nix-shell --run 'uv run pre-commit run --files src/mindroom/matrix/mentions.py tests/test_mentions.py IMPLEMENT-REPORT.md'`.
- Result: passed.
