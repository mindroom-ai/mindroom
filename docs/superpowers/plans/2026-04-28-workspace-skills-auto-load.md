# Workspace Skills Auto-Load Implementation Plan

## Goal

Enable an agent to load skills from its canonical shared workspace at `<storage>/agents/<agent>/workspace/skills/` without listing those skills in `config.yaml`.
Keep `skills:` as the allowlist for bundled, plugin, and user skills.
Block workspace skill script execution through `get_skill_script(..., execute=True)` while keeping script reads available.

## Constraints

Workspace skills are agent-scoped and should not appear in global skill listings.
Workspace skills should override same-named bundled, plugin, and user skills for normal runtime loading.
Explicit test-provided skill roots should keep their current highest-precedence behavior.
OpenClaw `os` gating should run before `always`.
Malformed workspace skills should be skipped with warnings rather than failing agent construction.

## Steps

1. Add tests in `tests/test_skills.py` for empty and omitted `skills`, global allowlist separation, workspace precedence, workspace script read versus execute behavior, malformed metadata skipping, and OpenClaw `os` before `always`.
2. Run the new targeted tests before implementation and record the expected failures.
3. Update `src/mindroom/tool_system/skills.py` to split configured global loading from workspace auto-loading.
4. Return a MindRoom `Skills` subclass that rejects `execute=True` for scripts whose resolved skill source path is under the workspace skills root.
5. Update OpenClaw eligibility ordering so OS mismatch wins before `always`.
6. Update `docs/skills.md` to document workspace auto-load behavior, precedence, next-run activation, script policy, and proactive skill creation policy.
7. Run targeted skills tests, then broader pytest and pre-commit checks as feasible.
8. Commit the implementation, push the branch, and open a PR.
