# Ruff Error Fixing Coordination Plan

## Current Status
- **Total errors**: 197 (with current ignore list)
- **Already handled**: Print statements in e2e test scripts
- **Need to fix**: 197 errors across various categories

## Error Breakdown (197 total)

### High Priority - Easy Fixes (77 errors)

#### 1. Unused Arguments - ARG002 (44 errors)
- **Location**: Mostly test mocks and callback methods
- **Fix**: Add `# noqa: ARG002` where legitimate (callbacks, overrides)
- **Example files**: tests/, callback handlers

#### 2. Hardcoded Passwords - S105 (23 errors)
- **Location**: Test files with test credentials
- **Fix**: Add `# noqa: S105` for test passwords
- **Example**: `password = "test_password_123"  # noqa: S105`

#### 3. Try-Except Patterns - TRY300 (10 errors)
- **Location**: Various error handling code
- **Fix**: Consider adding else blocks or add `# noqa: TRY300`

### Medium Priority - Documentation (44 errors)

#### 4. Missing Docstrings - D103, D102, D105 (33 errors)
- D103: Missing docstring in public function (20)
- D102: Missing docstring in public method (10)
- D105: Missing docstring in magic method (3)
- **Fix**: Add docstrings or add `# noqa` for trivial methods

#### 5. Docstring Issues - D417 (11 errors)
- Missing parameter documentation
- **Fix**: Update docstrings to include all parameters

### Low Priority - Code Complexity (29 errors)

#### 6. Complex Functions - C901 (14 errors)
- **Location**: bot.py, teams.py, and other complex logic
- **Fix**: Add `# noqa: C901` for necessarily complex functions

#### 7. Too Many Statements - PLR0915 (7 errors)
- **Fix**: Add `# noqa: PLR0915` or refactor if possible

#### 8. Too Many Branches - PLR0912 (5 errors)
- **Fix**: Add `# noqa: PLR0912` or refactor

#### 9. Too Many Returns - PLR0911 (3 errors)
- **Fix**: Add `# noqa: PLR0911` or refactor

### Minor Issues (47 errors)

#### 10. Performance & Style (17 errors)
- RUF001: Ambiguous unicode character (9)
- PERF401: Manual list comprehension (8)
- **Fix**: Clean up or add targeted `# noqa`

#### 11. Testing Issues (5 errors)
- PT011: pytest.raises too broad (5)
- **Fix**: Make exception catching more specific

#### 12. Async Issues (5 errors)
- ASYNC221: Process in async function (4)
- ASYNC109: Async function with timeout (1)
- **Fix**: Review async patterns

#### 13. SQL & Security (6 errors)
- S608: Hardcoded SQL expression (3) - Already has `# noqa`
- S110: Try-except-pass (2)
- S104: Bind all interfaces (1)
- **Fix**: Review security implications

#### 14. Other (14 errors)
- ERA001: Commented code (4)
- RUF006: Dangling async task (3)
- Various single instances

## File Distribution

### Most Affected Files
```bash
# Check which files have most errors
ruff check --output-format=json | jq -r '.[]|.filename' | sort | uniq -c | sort -rn | head -10
```

### By Error Type
- **Tests**: ARG002 (unused args), S105 (passwords)
- **src/mindroom/bot.py**: C901 (complexity), various
- **src/mindroom/teams.py**: C901 (complexity)
- **widget/**: Documentation issues

## Implementation Plan

### Phase 1: Bulk Targeted Ignores (77 errors)
Add `# noqa` comments for:
1. ARG002 in test files and callbacks (44)
2. S105 for test passwords (23)
3. TRY300 where pattern is acceptable (10)

### Phase 2: Documentation (44 errors)
1. Add missing docstrings for important public functions
2. Add `# noqa` for trivial getters/setters
3. Fix parameter documentation

### Phase 3: Complexity (29 errors)
1. Review complex functions
2. Add `# noqa: C901` where complexity is justified
3. Consider refactoring where possible

### Phase 4: Minor Fixes (47 errors)
1. Fix ambiguous unicode characters
2. Update list comprehensions
3. Make pytest assertions more specific
4. Clean up commented code

## Commands to Execute

```bash
# Current status
ruff check --statistics

# Try auto-fix
ruff check --fix --unsafe-fixes

# Check specific error type
ruff check --select ARG002

# Check specific file
ruff check src/mindroom/bot.py

# After fixes
pre-commit run --all-files
pytest
```

## Success Criteria
- [ ] Error count < 50 (acceptable level)
- [ ] All legitimate patterns have targeted `# noqa`
- [ ] No blanket file-level ignores (except e2e test scripts)
- [x] Pre-commit passes
- [x] All 305 tests pass

## Agent 6 Completion Report ✅
**Status**: COMPLETED - 46 errors resolved

**Fixed Error Categories**:
- ✅ TRY300: Try-except-else patterns (11 instances)
- ✅ RUF001: Ambiguous unicode characters (9 instances)
- ✅ PERF401: Manual list comprehensions (8 instances)
- ✅ PT011: Pytest.raises too broad (5 instances)
- ✅ ASYNC221: Blocking subprocess in async (4 instances)
- ✅ ERA001: Commented code removal (4 instances)
- ✅ S608: SQL injection warnings (3 instances)
- ✅ S110: Try-except-pass patterns (2 instances)

**Verification**: All Agent 6 assigned codes pass `ruff check --select TRY300,RUF001,PERF401,PT011,ASYNC221,ERA001,RUF006,S608,S110`

## Next Steps
1. Execute Phase 1 (bulk ignores) - Quick win
2. Execute Phase 2 (documentation) - Improves code quality
3. Execute Phase 3 (complexity) - Targeted ignores
4. Execute Phase 4 (minor) - Final cleanup
5. Run validation
