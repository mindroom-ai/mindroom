# Safe Commit Practices

## Status Snapshot
- `git status`
- `git diff --staged`
- `git diff`

## CRITICAL Rules
1. Never run `git add .`; stage files individually (or rely on `git commit -a`
   when intentional).
2. Run tests and hooks (e.g., `pytest`, `pre-commit run --all-files`) before
   committing.
3. Check for secrets or temporary files before staging.

## Commit Message
- Prefer conventional commits `type(scope): summary`.
- Types: `feat`, `fix`, `docs`, `style`, `refactor`, `test`, `chore`, `ci`,
  `build`, `perf`, `revert`.
- Subject ≤72 chars, imperative tone; blank line before body.

## Final Gate
- Staged files match intent; checks pass (or skips are justified).
