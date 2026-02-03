# Coding Agent Playbook

Project-neutral version of `CLAUDE.md`.

## Core Philosophy

- Embrace change; backward compatibility is optional when there are no users.
- Deliver the smallest working fix—no over-engineering or bonus features.
- Prefer functions and typed `dataclasses`; delete unused code aggressively.
- Keep imports at the top; add try/except only for genuine failure paths.

## Workflow

1. Read the request, main docs, and relevant source (including libs in `.venv`).
2. Look up official docs before guessing.
3. Install deps with the project tool (e.g., `uv sync --all-extras`), then
   activate the venv.
4. Add packages via the approved command (e.g., `uv add`, `uv add --dev`).
5. Inspect `git diff origin/main | cat` to understand recent work.
6. Stage files individually; commits stay atomic and imperative.
7. **CRITICAL**: run tests (e.g., `pytest`) and `pre-commit run --all-files`
   before claiming the task is done.

## CRITICAL Don’ts

- Never run `git add .`.
- Never mark work complete with failing tests.
- Never hand-edit generated files; rerun the generator instead.
