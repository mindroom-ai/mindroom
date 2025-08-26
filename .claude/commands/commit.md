---
description: Safe git commit practices for MindRoom
allowed-tools: Bash(git status:*), Bash(git diff:*), Bash(git add:*), Bash(pre-commit run:*)
---

# Safe Git Commit Practices

## Current Status
- Git status: !`git status`
- Staged changes: !`git diff --staged`
- Unstaged changes: !`git diff`

## CRITICAL Reminders

1. **Selective Staging**
   - **NEVER use `git add .` or `git commit -a`**
   - Add files individually: `git add <filename>`
   - Review with `git status` before staging

2. **Why This Matters**
   - Project has unstaged debugging scripts
   - Temporary test files must not be committed
   - Configuration files with credentials need protection
   - Only commit relevant changes from current work

3. **Pre-commit Validation**
   - **ALWAYS run**: `pre-commit run --all-files`
   - Ensures code style compliance
   - Fixes linting issues automatically
   - Validates Python formatting with ruff

4. **Commit Messages**
   - Use descriptive messages
   - For incomplete work: "WIP: [description]"
   - Follow conventional commits:
     - `feat:` - new features
     - `fix:` - bug fixes
     - `docs:` - documentation
     - `refactor:` - code refactoring
     - `test:` - test changes

5. **Final Checks**
   - Run `pytest` to ensure tests pass
   - Review `git diff --staged`
   - Verify no unrelated files included
   - Check for sensitive information

Remember: The project frequently has debugging scripts and test files that should NOT be committed!
