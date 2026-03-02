# Refactor Report: Move command modules into `src/mindroom/commands/`

## What you changed
I moved four command modules with `git mv` to preserve history.
I moved `src/mindroom/commands.py` to `src/mindroom/commands/parsing.py`.
I moved `src/mindroom/command_handler.py` to `src/mindroom/commands/handler.py`.
I moved `src/mindroom/config_commands.py` to `src/mindroom/commands/config_commands.py`.
I moved `src/mindroom/config_confirmation.py` to `src/mindroom/commands/config_confirmation.py`.
I did not move `src/mindroom/interactive.py`.
I did not create `src/mindroom/commands/__init__.py`.
I updated imports across `src/` and `tests/` to use the new module paths, including `mindroom.commands.parsing`, `mindroom.commands.handler`, and `mindroom.commands.config_commands`.
I updated monkeypatch string targets from `mindroom.command_handler.*` to `mindroom.commands.handler.*` in affected tests.
I updated the README auto-generated command snippet import to `from mindroom.commands.parsing import _get_command_entries` so the pre-commit docs hook passes.

## Verification gate results
I ran all verification gate commands from the prompt exactly.
All stale-reference `rg` checks returned zero matches.
`test ! -f src/mindroom/commands/__init__.py` passed.

## Test results
`pre-commit run --all-files` passed.
`pytest -q --ignore=tests/test_browser_tool.py --ignore=tests/test_claude_agent_nightly_soak.py --ignore=tests/test_claude_agent_tool.py --ignore=tests/test_gmail_tools.py --ignore=tests/test_openclaw_compat_contract.py` passed.
The pytest run result was `1786 passed, 18 skipped`.

## Any issues encountered
Ruff auto-fix initially rewrote a few imports to `from mindroom.commands import ...`, which violated the stale-reference gate.
I adjusted those imports to explicit submodule imports and re-ran the gate until all checks were clean.
The README docs-generation pre-commit hook initially failed because it still imported `_get_command_entries` from `mindroom.commands`, and updating that snippet resolved the failure.
