# Agent Instructions for Fixing Ruff Errors

## Your Task
You are tasked with fixing ruff linting errors in the mindroom codebase. There are currently 197 errors that need to be addressed. You should work systematically through your assigned category of errors.

## Current Situation
- The codebase has already had a major ruff migration where many errors were fixed
- Some error types are already ignored in `pyproject.toml` (S101, SLF001, PLR2004, FBT001/002/003, G004, T20, TD002/003, E501, D107, D401, D402, PLR0913, PLW0603, S603, S607)
- There are 197 remaining errors that need targeted fixes or `# noqa` comments
- All 305 tests currently pass - DO NOT break them

## Important Rules
1. **DO NOT** add any more blanket ignores to `pyproject.toml`
2. **DO NOT** modify the existing ignore list in `pyproject.toml`
3. **USE** targeted `# noqa: ERROR_CODE` comments for legitimate violations
4. **PREFER** fixing the issue over ignoring it when reasonable
5. **ENSURE** all tests still pass after your changes
6. **COMMIT** your work frequently with clear messages

## Error Categories and Assignments

### Agent 1: Unused Arguments (44 errors)
**Error Code**: ARG002 - Unused method argument
**Your Task**: 
1. Run `ruff check --select ARG002` to find all instances
2. For each error, determine if it's:
   - A callback/override method → Add `# noqa: ARG002` on the line
   - A test mock → Add `# noqa: ARG002` on the line
   - Actually unused → Remove the parameter if safe
3. Example fix:
   ```python
   def callback_handler(self, event, context):  # noqa: ARG002
       # context is required by the interface but not used here
       return self.process(event)
   ```

### Agent 2: Test Passwords (23 errors)
**Error Code**: S105 - Hardcoded password string
**Your Task**:
1. Run `ruff check --select S105` to find all instances
2. These are all in test files with test credentials
3. Add `# noqa: S105` to each line with a test password
4. Example fix:
   ```python
   TEST_PASSWORD = "test_password_123"  # noqa: S105
   ```

### Agent 3: Documentation - Missing Docstrings (33 errors)
**Error Codes**: D103 (20), D102 (10), D105 (3)
**Your Task**:
1. Run `ruff check --select D103,D102,D105` to find all instances
2. For each error:
   - If it's an important public function → Add a proper docstring
   - If it's a trivial getter/setter → Add `# noqa: D103` (or appropriate code)
   - If it's a test → Add a brief docstring
3. Example fixes:
   ```python
   def get_name(self):
       """Return the name of the object."""
       return self.name
   
   def _internal_helper(self):  # noqa: D103
       return self._value * 2
   ```

### Agent 4: Documentation - Parameter Docs (11 errors)
**Error Code**: D417 - Missing argument descriptions in docstring
**Your Task**:
1. Run `ruff check --select D417` to find all instances
2. Update docstrings to include all parameters
3. Example fix:
   ```python
   def process(self, data, validate=True):
       """Process the given data.
       
       Args:
           data: The data to process
           validate: Whether to validate the data first
       """
   ```

### Agent 5: Code Complexity (29 errors)
**Error Codes**: C901 (14), PLR0915 (7), PLR0912 (5), PLR0911 (3)
**Your Task**:
1. Run `ruff check --select C901,PLR0915,PLR0912,PLR0911` to find all instances
2. For each complex function:
   - Review if it can be reasonably refactored → Refactor it
   - If complexity is justified → Add `# noqa: C901` (or appropriate code)
3. Focus on files: `src/mindroom/bot.py`, `src/mindroom/teams.py`
4. Example:
   ```python
   def complex_routing_logic(self, message):  # noqa: C901
       # This function handles all message routing and needs to check many conditions
       ...
   ```

### Agent 6: Minor Issues and Cleanup (87 errors)
**Error Codes**: TRY300 (10), RUF001 (9), PERF401 (8), PT011 (5), ASYNC221 (4), ERA001 (4), RUF006 (3), S608 (3), S110 (2), and others
**Your Task**:
1. Fix TRY300 - Add else blocks to try-except or add `# noqa: TRY300`
2. Fix RUF001 - Replace ambiguous unicode characters
3. Fix PERF401 - Convert manual list comprehensions
4. Fix PT011 - Make pytest.raises more specific
5. Review and fix/ignore the rest appropriately

## How to Work

1. **Start by checking your assigned errors**:
   ```bash
   source .venv/bin/activate
   ruff check --select YOUR_ERROR_CODES
   ```

2. **Fix systematically**:
   - Start with the files that have the most errors
   - Make targeted fixes
   - Test after each significant change

3. **Verify your work**:
   ```bash
   # Check your specific errors are fixed
   ruff check --select YOUR_ERROR_CODES
   
   # Run tests to ensure nothing broke
   pytest tests/
   
   # Check overall error count
   ruff check --statistics
   ```

4. **Commit frequently**:
   ```bash
   git add -p  # Add changes selectively
   git commit -m "fix: resolve ARG002 errors in test files"
   ```

## Coordination
- Check the file `RUFF_FINAL_COORDINATION.md` for overall status
- Don't modify files another agent is working on
- Focus on your assigned error codes
- If you find an error that needs discussion, note it in your commits

## Success Criteria
- Your assigned error codes show 0 errors when running `ruff check --select YOUR_CODES`
- All tests still pass
- No blanket ignores added to pyproject.toml
- Only targeted `# noqa` comments where justified

## Example Working Session
```bash
# Activate environment
source .venv/bin/activate

# Check current status of your errors
ruff check --select ARG002 --statistics

# See the actual errors
ruff check --select ARG002

# Fix errors in a specific file
vim src/mindroom/bot.py
# Add # noqa: ARG002 to legitimate unused arguments

# Test the file still works
pytest tests/test_bot.py -xvs

# Commit the changes
git add src/mindroom/bot.py
git commit -m "fix: add noqa for legitimate unused arguments in bot.py callbacks"

# Continue with next file...
```

Good luck! Remember: targeted fixes only, preserve test compatibility, and commit frequently.