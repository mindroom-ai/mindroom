# Anti-Cruft Checklist

## Scope Guardrails
- Establish task or PR scope first, verify the intended base ref, then inspect both its diff and `git status --short` so untracked files are not missed.

## CRITICAL Principles
1. Delete backward-compat paths and deprecated shims.
2. Favor simple functions/dataclasses; avoid factories and class hierarchies.
3. No over-engineering or “just in case” branches.
4. Remove redundant defensive checks and unnecessary try/except blocks; retain validation at trust, persistence, and external-I/O boundaries.
5. Keep imports at top unless avoiding a circular import or deferring a heavy optional dependency; use an explicit function-local import with `# noqa: PLC0415` when required.
6. Delete unused code; replace duck typing with explicit types when needed.

## Execution
1. Confirm file is in scope.
2. Apply targeted deletions/simplifications.
3. Run relevant tests through `uv run --all-extras pytest ...` or the matching `just` recipe.
4. Run `uv run pre-commit run --all-files`; for Python cruft, ensure the configured vulture hook runs.
