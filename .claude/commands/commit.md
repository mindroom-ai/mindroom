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
   - **NEVER use `git add .` or `git add -A`**
   - Add files individually: `git add <filename>` or use `git commit -a`
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

4. **Commit Message Style Guide**
   - **Format**: `type(scope): description` (conventional commits)
   - **Types**: feat, fix, docs, style, refactor, test, chore, perf, ci, build, revert
   - **Scope**: Include in parentheses when clear module/area affected
   - **Description rules**:
     - Use lowercase after colon (unless proper nouns: Docker, Matrix, AI)
     - Keep first line under 72 characters
     - Use imperative mood ("Add" not "Added", "Fix" not "Fixed")
     - Be specific about what changed
     - For bugs: describe what was broken
     - For features: describe capability added
     - For refactors: explain what was restructured

   **Good examples**:
   - `feat: Add Direct Message (DM) support for private agent conversations`
   - `fix(profile): Handle null user data in profile loading`
   - `refactor: Extract team formation logic into private function`
   - `test: Add comprehensive tests for extra_kwargs functionality`
   - `docs: Add comprehensive deployment guide for instance manager`

   **For incomplete work**: `WIP: [description]` is acceptable

5. **Final Checks**
   - Run `pytest` to ensure tests pass
   - Review `git diff --staged`
   - Verify no unrelated files included
   - Check for sensitive information

Remember: The project frequently has debugging scripts and test files that should NOT be committed!
