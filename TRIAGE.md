# Triage

- A-1: FIX.
- `_rewrite_working_session_for_compaction()` now keeps compacting the originally selected visible run set until that set is exhausted, even if an intermediate pass already drops the remaining history under the replay budget.
- B-1: FIX.
- Replaced the stale single-pass regression with a true multi-pass `prepare_history_for_run()` test that constrains `summary_input_budget`, proves the post-first-pass history is already within replay budget, and still requires a second summary call to remove the remaining raw run.
- B-2: FIX.
- Added a forced-compaction zero-visible-runs test that confirms the force flag is cleared, no summary is generated, and `prepared.compaction_outcomes` stays empty.
- C-1: FIX.
- The old dead `side_effect` setup is gone because the updated multi-pass test now legitimately exercises two summary generations.
- C-2: FIX.
- Renamed the auto-compaction regression test so the name matches the multi-pass behavior it now verifies.

# Verification

- `export NIX_PATH=nixpkgs=/nix/var/nix/profiles/per-user/root/channels/nixos && nix-shell --run 'uv run pytest tests/test_agno_history.py tests/test_compact_context.py -x -n 0 --no-cov -v'`
- Result: `75 passed, 1 warning`.
