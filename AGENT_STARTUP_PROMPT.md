# Ruff Migration Agent Startup Prompt

## Your Mission
You are one of 6 parallel agents working to enable ALL ruff rules in the mindroom-2 codebase. Your goal is to fix specific error categories to make the code compliant with all ruff rules.

## First Steps (MANDATORY)
1. Identify yourself as Agent [1-6] based on when you were started
2. Read the coordination file: `RUFF_MIGRATION_COORDINATION.md`
3. Find your pre-assigned work in the Agent Registry section
4. Mark your checkbox to indicate you've started working

## Your Workflow
1. **Focus on your assigned error codes only** - do not fix errors outside your category
2. **Remove your error codes from the ignore list** in `pyproject.toml` when starting
3. **Fix all instances** of your assigned errors in the codebase
4. **Test your changes** with `ruff check --select [YOUR_CODES] src/ tests/`
5. **Run tests** with `pytest` to ensure nothing breaks
6. **Update the coordination file** with your progress
7. **Commit your changes** with clear messages

## Important Rules
- **DO NOT** work on error codes assigned to other agents
- **DO NOT** remove error codes from ignore list that aren't yours
- **DO** communicate blockers in the coordination file
- **DO** run `pre-commit run --all-files` before final commit
- **DO** ensure all tests pass

## Commands You'll Need
```bash
# Check your specific errors
ruff check --select [YOUR_CODE] src/ tests/

# Fix automatically where possible
ruff check --select [YOUR_CODE] --fix src/ tests/

# See detailed error info
ruff rule [YOUR_CODE]

# Run tests
pytest

# Check everything still works
pre-commit run --all-files
```

## Example Agent Workflow
If you're Agent 3 and select "D103" errors:
1. Edit `RUFF_MIGRATION_COORDINATION.md` to claim Agent 3 and D103
2. Remove "D103" from `pyproject.toml` ignore list
3. Run `ruff check --select D103 src/ tests/` to see all instances
4. Fix all D103 errors (missing docstrings in public functions)
5. Test with `pytest`
6. Update coordination file with completion
7. Commit: "Fix D103: Add missing docstrings to public functions"

## Start Now
Begin by reading `RUFF_MIGRATION_COORDINATION.md` and claiming your agent number!
