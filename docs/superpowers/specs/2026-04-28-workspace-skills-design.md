# Workspace Skills Auto-Load Design

## Context

MindRoom already supports bundled, plugin, user, and agent workspace skill roots.
Today, `AgentConfig.skills` is both the allowlist and the activation switch.
When an agent has `skills: []`, `build_agent_skills()` returns `None`, so the agent workspace skill root is ignored.
This blocks Kubernetes worker deployments where only the agent workspace is mounted and also prevents agents from creating local skills without editing config.

## Goals

- Let agents use skills placed under their canonical shared workspace without adding those skill names to `config.yaml`.
- Preserve `skills:` as the allowlist for bundled, plugin, and user skills.
- Preserve the existing permission model for executing code.
- Keep the first version local to runtime skill loading and documentation.

## Non-Goals

- Do not auto-load requester-private workspace skills.
- Do not add dashboard or API management for agent workspace skills.
- Do not add proactive system-prompt guidance that tells agents when to create skills.
- Do not reintroduce `!skill` commands or OpenClaw user-invocable skill behavior.
- Do not implement OpenClaw `disable-model-invocation` behavior.

## Runtime Behavior

MindRoom will auto-load direct child skills under `<storage>/agents/<agent>/workspace/skills/`.
The workspace skill root is agent-scoped and is only available to the owning agent at runtime.
If the directory does not exist, it is treated as an empty skill root without warnings.
If a workspace skill is created, edited, or deleted during one run, the change becomes visible on the next agent run.
Same-turn activation is out of scope.
Malformed workspace skills are skipped with warnings and do not block agent construction.

## Precedence

Workspace skills override same-named user, plugin, and bundled skills for the owning agent.
`skills: []` continues to mean that no configured global skills are enabled.
`skills: []` does not disable workspace skills.
Omitted `skills` and explicit `skills: []` have the same workspace auto-load behavior.

## Script Policy

Workspace skill scripts remain readable through `get_skill_script(..., execute=False)`.
Workspace skill scripts must not execute through `get_skill_script(..., execute=True)`.
The execution block is based on the resolved skill source path being under the agent workspace skill root.
The block applies even when the workspace skill name is explicitly listed in `config.yaml`.
Non-workspace configured skills keep the existing Agno script execution behavior in this change.
Agents that have shell or file execution permissions can still read or execute workspace files through their normal tools.

## OpenClaw Compatibility

MindRoom will keep using OpenClaw-compatible `metadata.openclaw` filters for all skills.
Workspace skills will respect the same metadata filters as other skills.
MindRoom will align `always` with current OpenClaw behavior.
OS gating happens before `always`.
`always: true` bypasses `requires`, but it does not bypass OS mismatch.

## Documentation

`docs/skills.md` will document workspace auto-load behavior.
The docs will explain the canonical path, next-run activation, precedence, and script execution policy.
The docs will state that proactive skill creation is a user prompt policy, not a built-in behavior.
Generated MindRoom docs references should be updated if they are checked in by the repository workflow.

## Implementation Outline

Update `src/mindroom/tool_system/skills.py` so `build_agent_skills()` builds a `Skills` object when the configured allowlist is empty but workspace skills may exist.
Split configured global skill loading from workspace auto-loading so the global roots remain allowlisted and the workspace root is always eligible for the owning agent.
Add a MindRoom-owned wrapper around Agno skill tools so workspace-origin scripts can be read but cannot be executed through `get_skill_script`.
Keep `list_skill_listings()` and the global `/api/skills` surface unchanged for this first version.
Update OpenClaw metadata eligibility order so OS checks happen before `always`.

## Test Plan

- Empty `skills: []` loads workspace skills.
- Empty `skills: []` does not load bundled, plugin, or user skills.
- Omitted `skills` and explicit `skills: []` both allow workspace auto-load.
- Workspace skills override same-named configured global skills for the owning agent.
- Workspace skill scripts are readable through `get_skill_script(..., execute=False)`.
- Workspace skill scripts are rejected through `get_skill_script(..., execute=True)`.
- Non-workspace configured skill script execution behavior remains unchanged.
- Malformed workspace skills are skipped without failing agent construction.
- OpenClaw `os` gating wins over `always: true`.
