# Repository Initialization Guide

## Understand the Assignment
- Restate the request to confirm intent.
- Check branch/state: `git branch --show-current`, `git status --short`, and an explicit base diff after verifying the base ref is current.

## Gather Context
1. Read `README.md`, contributor docs, architecture notes.
2. Scan relevant modules to learn entry points and utilities.
3. Review config (env vars, feature flags, secrets) before changes.

## Working Agreements
- Expect speech-to-text typos; clarify rather than guess.
- Look for existing helpers before writing new ones.
- Sync dependencies with the repository's package manager; in MindRoom use `uv sync --all-extras` and never invoke `pip` directly.
- Follow the coding playbook (simplicity, tidy imports, remove unused code).

## Next
- Outline the plan, confirm if needed, then proceed with focused, tested work.
