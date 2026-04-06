# Round 1 Triage

- F1 fixed in `src/mindroom/message_target.py` by resolving scheduled delivery strictly from persisted workflow state and not consulting live router thread mode.
- F2 fixed in `src/mindroom/custom_tools/subagents.py` by defaulting `sessions_send()` to the canonical runtime target session instead of raw `context.thread_id`.
- F3 fixed in `src/mindroom/commands/handler.py` by deriving skill-command execution identity from canonical runtime target state.
- M1 fixed in `src/mindroom/streaming.py` by using `resolved_target.room_id` for the pre-send thread lookup.
- M2 deferred except for the overlapping F2 and F3 callsites, per review scope.
- M3 deferred as follow-up scope, per review scope.

# Regression Coverage

- Added a scheduled-workflow regression proving a persisted `thread_id` is honored even if the live router would now be room-mode.
- Added a subagent regression proving first-turn follow-ups default to the resolved thread session when raw `thread_id` is absent.
- Added a skill-command regression proving runtime-context execution identity uses the canonical resolved thread root and session.

# Verification

- `./.venv/bin/pytest tests/test_subagents.py tests/test_skills.py tests/test_workflow_scheduling.py tests/test_thread_mode.py tests/test_streaming_behavior.py` passed.
- `./.venv/bin/pytest` passed with `3657 passed, 19 skipped`.
- `./.venv/bin/pre-commit run --all-files` did not complete cleanly because repo-wide `ty` failures remain outside this change set and this environment does not provide `uv` or `uvx` for the `sync-config-models` and `generate-skill-references` hooks.
- `./.venv/bin/ruff check` and `./.venv/bin/ruff format --check` passed for the touched files.
