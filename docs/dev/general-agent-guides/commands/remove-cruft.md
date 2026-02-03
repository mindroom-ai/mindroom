# Anti-Cruft Checklist

## Scope Guardrails
- Run `git diff origin/main`; only touch files in that diff.

## CRITICAL Principles
1. Delete backward-compat paths and deprecated shims.
2. Favor simple functions/dataclasses; avoid factories and class hierarchies.
3. No over-engineering or “just in case” branches.
4. Remove defensive checks and unnecessary try/except blocks.
5. Keep imports at top (local only for circular fixes).
6. Delete unused code; replace duck typing with explicit types when needed.

## Execution
1. Confirm file is in scope.
2. Apply targeted deletions/simplifications.
3. Re-run tests.
