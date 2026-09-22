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

### 1. Remove Obsolete Compatibility
- Remove unjustified fallback paths, compatibility shims, version checks, and deprecated methods
- Preserve required boundaries under the [Legacy Compatibility Policy](../../CLAUDE.md#legacy-compatibility-policy) and [Agno Compatibility Policy](../../CLAUDE.md#agno-compatibility-policy)
- Check documented provenance, removal criteria, and regression coverage before deleting a boundary; remove Agno workarounds only when the relevant behavioral tests pass without them
- One way to do things, not multiple

### 2. Radical Simplicity
- **Functional over classes**: Simple functions, not inheritance hierarchies
- **Prefer dataclasses**: Typed dataclasses over dicts
- **No over-engineering**: Solve TODAY's problem, not tomorrow's
- **No defensive programming**: Assume correct usage - no redundant checks

### 3. Code Hygiene
- **Imports at the top**: Function imports may avoid cycles or defer heavy/optional dependencies until first use, as required by [CLAUDE.md](../../CLAUDE.md#1-core-philosophy)
- Keep deferred imports explicit (`from x import Y`) with `# noqa: PLC0415` where needed, and preserve the `tests/test_import_graph.py` contract
- **No unnecessary try-except**: Only catch what can actually fail
- **Remove unused code**: Functions, imports, variables - delete ruthlessly
- **No premature abstraction**: Concrete implementations first
- **NO DUCKTYPING**: Explicit is better than implicit, so no `hasattr` or `getattr`, just use proper types with `isinstance` checks if needed

## Check for Common Cruft:

1. **Unjustified fallback patterns to DELETE** (subject to the compatibility policies above):
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
7. Remove obsolete or unjustified compatibility code IN THE FEATURE under the policies above
8. Delete unused imports, functions, variables IN THE FEATURE
9. Keep imports at file top except for the circular-import and heavy/optional-dependency cases above

Remember:
- **This codebase has NO users yet**. Be ruthless with NEW code.
- **BUT ONLY TOUCH FILES IN THE CURRENT DIFF!**
- Do not go on a cleanup spree outside the current feature
- Every line of NEW code is a liability - delete first, ask questions later
