# Repository Initialization Guide

## Understand the Assignment
- Restate the request to confirm intent.
- Check branch/state: `git branch --show-current`, `git diff origin/main --stat`.

## Gather Context
1. Read `README.md`, contributor docs, architecture notes.
2. Scan relevant modules to learn entry points and utilities.
3. Review config (env vars, feature flags, secrets) before changes.

## Working Agreements
- Expect speech-to-text typos; clarify rather than guess.
- Look for existing helpers before writing new ones.
- Sync deps with the project tool (e.g., `uv`, `pip`, `pnpm`, `cargo`) and
  activate the venv.
- Follow the coding playbook (simplicity, tidy imports, remove unused code).

## Next
- Outline the plan, confirm if needed, then proceed with focused, tested work.
