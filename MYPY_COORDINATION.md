# MyPy Error Fixing Coordination Document

## Current Status
- **Total Original Errors**: ~450 MyPy errors
- **Current Status**: 29 errors remaining in 2 files
- **Progress**: 94% complete

## Currently Working On (Active Tasks)

### Developer (Current Session)
- **Status**: Working on tests/test_threading_error.py (27 errors)
- **Last Completed**: tests/test_streaming_e2e.py (3 errors) - Fixed by Claude Code Session 6
- **Started**: tests/test_threading_error.py at session start

## Completed Files ✅

### Round 1 (138 errors fixed)
1. ✅ tests/test_config_reload.py (50 errors) - Fixed by Claude Code
2. ✅ tests/test_bot_scheduling.py (31 errors) - Fixed by Claude Code
3. ✅ tests/test_router_rooms.py (22 errors) - Fixed by Claude Code
4. ✅ tests/test_memory_functions.py (18 errors) - Fixed by Claude Code
5. ✅ tests/test_routing_regression.py (17 errors) - Fixed by Claude Code

### Round 2 (44 errors fixed)
6. ✅ tests/test_team_collaboration.py (15 errors) - Fixed by Claude Code
7. ✅ tests/test_team_extraction.py (14 errors) - Fixed by Claude Code
8. ✅ tests/test_agent_response_logic.py (16 errors) - Fixed by Claude Code

### Round 3 (76 errors fixed)
9. ✅ tests/test_multi_agent_e2e.py (11 errors) - Fixed by Claude Code
10. ✅ tests/test_matrix_identity.py (15 errors) - Fixed by Claude Code Session 2
11. ✅ tests/test_streaming_edits.py (15 errors) - Fixed by Claude Code Session 1
12. ✅ tests/test_multi_agent_bot.py (8 errors) - Fixed by Claude Code Session 1
13. ✅ tests/test_team_coordination.py (4 errors) - Fixed by Claude Code Session 2
14. ✅ tests/test_thread_invites.py (12 errors) - Fixed by Claude Code Session 1
15. ✅ tests/test_streaming_e2e.py (11 errors) - Fixed by Claude Code Session
16. ✅ tests/test_mentions.py (11 errors) - Fixed by Claude Code Session 1
17. ✅ tests/test_team_room_updates.py (3 errors) - Fixed by Claude Code Session 5
18. ✅ tests/test_streaming_e2e.py (3 errors) - Fixed by Claude Code Session 6

## Remaining Files (TODO List)

### High Priority (>10 errors)
- [🔄] tests/test_threading_error.py (27 errors) - **Currently being worked on**

### Medium Priority (5-10 errors)
[No files remaining]

### Low Priority (1-4 errors)
- [🔄] tests/test_streaming_behavior.py (2 errors) - **Currently being worked on**
- [ ] tests/test_team_collaboration.py (2 errors) - **Available**
- [ ] tests/test_bot_scheduling.py (1 error) - **Available**

## Coordination Rules

### Before Starting Work:
1. Update this document with your status
2. Mark the file as "Currently being worked on"
3. Add your estimated completion time

### After Completing Work:
1. Move the file to the "Completed Files" section
2. Update your status to "Available"
3. Commit your changes with a descriptive message
4. Update the "Current Status" numbers at the top

### Communication:
- Use this document to avoid conflicts
- If you see someone already working on a file, pick a different one
- Always check this document before starting new work

## Common Error Types & Fixes

### Most Common Issues:
1. **Missing return type annotations**: Add `-> None` to test functions
2. **Missing type annotations on variables**: Add type hints like `dict[str, Any]`
3. **Union attribute errors**: Add None checks or `# type: ignore[union-attr]`
4. **Method assignment errors**: Use monkeypatch or `# type: ignore[method-assign]`

### Quick Reference Commands:
```bash
# Check errors in specific file
.venv/bin/mypy tests/test_filename.py

# Check total remaining errors
.venv/bin/mypy tests 2>&1 | wc -l

# Find files with most errors
.venv/bin/mypy tests 2>&1 | grep "error:" | cut -d':' -f1 | sort | uniq -c | sort -nr
```

## Notes
- Source code (src/mindroom) already passes MyPy with 0 errors
- Focus is on test files only
- Maintain test functionality - all tests should still pass
- Use pre-commit hooks (git commit will auto-format)
