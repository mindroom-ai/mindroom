---
description: Remove cruft and enforce MindRoom's no-compromise simplicity principles
allowed-tools: Bash(git diff:*), Bash(git status:*)
argument-hint: [file_or_directory]
---

# Anti-Cruft Review

## CRITICAL: Scope Analysis
**Current feature diff from main:**
!`git diff origin/main`

## ⚠️ STRICT SCOPE LIMITATION ⚠️
**ONLY modify code that is part of the current feature shown in the diff above!**
- If a file doesn't appear in the diff, DO NOT TOUCH IT
- Only remove cruft from files that are already being modified
- Focus exclusively on the current feature's code
- Leave all other code untouched, even if it has cruft

## Review Target
Review the code at @$ARGUMENTS and ensure it follows MindRoom's core philosophy from CLAUDE.md:

## MANDATORY Principles to Enforce:

### 1. NO Backward Compatibility
- **This project has ZERO users** - break anything that needs breaking
- Remove ALL fallback code paths
- Delete compatibility shims, version checks, deprecated methods
- One way to do things, not multiple

### 2. Radical Simplicity
- **Functional over classes**: Simple functions, not inheritance hierarchies
- **Prefer dataclasses**: Typed dataclasses over dicts
- **No over-engineering**: Solve TODAY's problem, not tomorrow's
- **No defensive programming**: Assume correct usage - no redundant checks

### 3. Code Hygiene
- **Imports at the top**: NEVER in functions (except circular import fixes)
- **No unnecessary try-except**: Only catch what can actually fail
- **Remove unused code**: Functions, imports, variables - delete ruthlessly
- **No premature abstraction**: Concrete implementations first
- **NO DUCKTYPING**: Explicit is better than implicit, so no `hasattr` or `getattr`, just use proper types with `isinstance` checks if needed

## Check for Common Cruft:

1. **Fallback patterns to DELETE**:
   - `if x else default_fallback` when x should always exist
   - `try/except: pass` hiding real issues
   - Multiple ways to configure the same thing
   - "Just in case" code paths

2. **Over-engineering to REMOVE**:
   - Abstract base classes
   - Complex inheritance chains
   - Factory patterns for simple objects
   - Unnecessary interfaces/protocols

3. **Defensive code to ELIMINATE**:
   - Checking for conditions that can't happen if code is correct
   - Validating internal state that should be guaranteed
   - Redundant error handling for programmer errors

## Action Items (ONLY for files in the current diff):

1. **Check scope first** - Is this file in `git diff origin/main`? If NO, STOP.
2. Read and apply ALL principles from CLAUDE.md
3. Identify cruft ONLY in the current feature's code
4. Propose deletions, not additions
5. Simplify complex patterns to basic functions
6. Replace class hierarchies with simple dataclasses
7. Remove ALL backward compatibility code IN THE FEATURE
8. Delete unused imports, functions, variables IN THE FEATURE
9. Ensure imports are at file top (not in functions)

Remember:
- **This codebase has NO users yet**. Be ruthless with NEW code.
- **BUT ONLY TOUCH FILES IN THE CURRENT DIFF!**
- Do not go on a cleanup spree outside the current feature
- Every line of NEW code is a liability - delete first, ask questions later
