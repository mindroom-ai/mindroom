# Mindroom Skills + Plugins Plan

Last updated: 2026-02-03

This document is the entry point for all work on the "skills + plugins" feature. It is a
living plan: update it as we learn, revise assumptions, or change direction.

Update rule: if any implementation choice changes or a new constraint is discovered, add a
short note to the Change Log at the end and update the relevant section.

## 1) Background

Mindroom currently has tools registered in code (`src/mindroom/tools/*` and
`src/mindroom/tools_metadata.py`) and agents select tools directly in `config.yaml`. We want
an OpenClaw-style model:

- Skills are instruction packs (`SKILL.md`) and do not add capabilities.
- Plugins provide tools and optionally ship skills.
- Skills are compatible with OpenClaw's `SKILL.md` format.

## 2) Goals

- Load OpenClaw-compatible skills from disk and expose them in the system prompt.
- Use Agno's built-in Skills orchestrator and tools for skill access.
- Add a plugin system that can register new tools without hard-coding imports in core.
- Keep skills usable across agents and per-agent configurable.
- Allow users to install custom skills to `~/.mindroom/skills`.
- Allow plugins to ship skills and tools together.

## 3) Non-goals (MVP)

- No remote plugin registry.
- No automatic dependency installation (may be added later).
- No UI changes required in the first iteration.
- No cross-platform packaging changes in the first iteration.

## 4) Decisions (Locked)

- Skills locations:
  - Bundled: `skills/` in repo
  - Plugin-provided: plugin skill directories
  - User-managed: `~/.mindroom/skills`
  - Precedence: user > plugins > bundled
- Plugins are declared in `config.yaml`.
- Agents default to **no skills** when `skills` is not set.
- OpenClaw `metadata` in `SKILL.md` must be parsed as JSON5.
- Skill orchestration uses Agno's `Skills` (prompt snippet + get_skill_* tools).
- Plugin manifest filename: `mindroom.plugin.json`.
- Tool metadata delivery: API builds from the in-memory registry at runtime; `tools_metadata.json`
  remains for frontend/icon build scripts.

## 5) Open Questions (Track Here)

- Plugin packaging: local dir only, Python package, or both?
  - MVP proposal: local dir plugins with optional Python module imports.
- Should `AgentConfig.tools` remain supported, or be deprecated in favor of `skills`?

## 6) Architecture Overview

### 6.1 Skills subsystem

Inputs:
- `skills/` in repo
- `~/.mindroom/skills`
- Plugin-provided skills directories

Format:
- `SKILL.md` with YAML frontmatter (OpenClaw compatible)
- `metadata` field in frontmatter is JSON5 (single-line) and normalized to a dict

Implementation (current):
- Agno `Skills` orchestrator is used for prompt injection and skill tools.
- A Mindroom loader wraps Agno `LocalSkills(validate=False)` to keep OpenClaw metadata.

Eligibility (OpenClaw style):
- `metadata.openclaw.always` bypasses other checks.
- `metadata.openclaw.os` matches current OS.
- `metadata.openclaw.requires.bins` and `anyBins` (optional; in Mindroom this may map to
  PATH executables if present).
- `metadata.openclaw.requires.env` satisfied by real env vars or credentials.
- `metadata.openclaw.requires.config` satisfied by `config.yaml` paths.
- Skill is excluded if disabled in config or not eligible.

Prompt injection:
- Use Agno's `<skills_system>` snippet (names, descriptions, scripts, references).
- Agents access skills via `get_skill_instructions`, `get_skill_reference`, `get_skill_script`.

Per-agent filtering:
- `agent.skills` is the allowlist; empty list means no skills.
- If `agent.skills` is set, only those skill names are included.

### 6.2 Plugins subsystem

Plugin is a directory with a manifest and optional tools + skills.

Example:
```
plugins/my-plugin/
  mindroom.plugin.json
  tools.py
  skills/
    my-skill/SKILL.md
```

`mindroom.plugin.json` (draft fields):
```json
{
  "name": "my-plugin",
  "tools_module": "tools.py",
  "skills": ["skills"]
}
```

Behavior:
- Load plugin manifest paths from `config.yaml`.
- Add any `skills/` directories to the skills search list.
- Import `tools_module` and let it call `register_tool_with_metadata(...)`.

### 6.3 Tool registry changes

Current:
- Tools are registered in `src/mindroom/tools/*` and imported by
  `src/mindroom/tools/__init__.py`.

Target:
- Keep core tools as-is for now.
- Add a plugin loader that dynamically registers additional tools at runtime.
- Avoid hard-coded imports for plugin tools.

### 6.4 API / UI implications

- `GET /api/tools` and integrations use runtime registry metadata, including plugin tools.
- `tools_metadata.json` stays as a build artifact for frontend icon imports.

## 7) Config Changes (Draft)

`config.yaml` additions:
```yaml
plugins:
  - ./plugins/my-plugin

agents:
  code:
    skills: [github, file, shell]
```

`AgentConfig`:
- add `skills: list[str] = []`

`Config`:
- add `plugins: list[str] = []`

## 8) Minimal Contracts (MVP)

These are the smallest stable interfaces to keep the implementation simple and DRY.

### 8.1 SKILL.md parsing (OpenClaw-compatible)

Supported frontmatter fields:
- `name` (string, required)
- `description` (string, required)
- `homepage` (string, optional)
- `metadata` (string, optional, JSON5; single-line in OpenClaw)

Ignored for MVP (not yet wired):
- `user-invocable`, `disable-model-invocation`, `command-dispatch`, `command-tool`, `command-arg-mode`

Resolution rules:
- `name` and `description` come from frontmatter only.
- `metadata` is parsed from JSON5 (string) to a dict when present.
- `source_path` is the skill directory path.
- If frontmatter is missing/invalid, the skill is skipped.

### 8.2 Eligibility rules (OpenClaw-style)

Use `metadata.openclaw` when present:
- `always: true` bypasses other checks.
- `os`: include only if it matches current OS.
- `requires.env`: include only if env var is set or credentials supply it.
- `requires.config`: include only if config path is truthy.

Out of scope for MVP:
- `requires.bins`, `requires.anyBins` (optional later if needed)

### 8.3 Prompt injection

Inject only when the agent has `skills` configured.

Format:
Agno's `<skills_system>` snippet (see `agno.skills.Skills.get_system_prompt_snippet()`).

Prompt rule:
- Choose one skill if clearly applicable, then use `get_skill_instructions(...)`.
- Read at most one skill up front.

### 8.4 Plugin manifest

`mindroom.plugin.json` fields:
- `name` (string, required)
- `tools_module` (string, optional; relative to plugin root)
- `skills` (array of relative paths, optional; defaults to none)

Behavior:
- If `tools_module` is present, import it and expect tool registration via
  `register_tool_with_metadata(...)`.
- If `skills` is present, add those directories to the skills search path.

## 8) Implementation Plan (Phased)

### Phase 1: Skills loader + prompt injection
Status: complete (2026-02-02)

- [x] Add skill discovery for `skills/` and `~/.mindroom/skills`.
- [x] Parse `SKILL.md` frontmatter + JSON5 metadata.
- [x] Apply OpenClaw gating rules.
- [x] Use Agno `Skills` system prompt snippet + get_skill_* tools.
- [x] Add unit tests for parsing + gating.

### Phase 2: Plugin loader (local dirs)
Status: complete (2026-02-02)

- [x] Add plugin discovery from `config.yaml`.
- [x] Load `mindroom.plugin.json`.
- [x] Import `tools_module` dynamically.
- [x] Add plugin skill dirs to the skills loader.
- [x] Add tests for plugin load + tool registration.

### Phase 3: Decide tool metadata delivery
Status: complete (2026-02-02)

- [x] Generate API tool metadata from the runtime registry (core + plugins).
- [x] Keep `tools_metadata.json` generation for frontend icon scripts.

### Phase 4: Skill command / dispatch (OpenClaw-style)

- Parse command dispatch fields from `SKILL.md` (e.g., `command-dispatch`, `command-tool`,
  `command-arg-mode`, `user-invocable`).
- Add a chat command handler (e.g., `!skill <name> [args]`) that:
  - Resolves the skill by name (same precedence as normal skill discovery).
  - Optionally maps to a tool call (`command-tool`) or inlines skill usage guidance.
  - Supports basic argument pass-through (`command-arg-mode`) with safe defaults.
- Ensure this path is opt-in per agent (new config flag or require `user-invocable: true`).
- Add tests for command parsing + dispatch behavior.

### Phase 5: Optional enhancements
- Skills watcher (hot reload).
- Dependency installer helper.
- Plugin packaging beyond local dirs.

## 9) Testing Strategy

- Skill parsing:
  - YAML frontmatter present/missing.
  - JSON5 metadata (trailing commas).
- Eligibility gating:
  - `always`, `os`, `requires.env`, `requires.config`.
- Prompt injection:
  - Skills appear only when eligible.
  - Agent with no skills gets no skills section.
- Plugin load:
  - Plugin tools registered in registry.
  - Plugin skill dir contributes to skills list.
- Live smoke test (local):
  - Start local Matrix + backend with OpenAI-compatible server on port 9292.
  - Use Matty to send two skill-triggering messages in Lobby.
  - Verify `SKILL_USED: hello` and `SKILL_USED: repo-quick-audit` responses.

## 10) Security + Safety

- Skills and plugins are executable code (plugins) or instructions (skills).
- Treat `~/.mindroom/skills` and plugins as trusted code locations.
- Log a warning when loading non-bundled plugins.
- Keep a clear audit trail of loaded plugins + skill sources.

## 11) Performance Notes

- Skills list should be compact to avoid prompt bloat.
- Cache parsed skills and only rebuild when sources change.
- Consider debounced file watching in later phases.

## 12) Change Log

- 2026-02-02: Initial plan created. Locked decisions recorded. Phased implementation defined.
- 2026-02-02: Phase 1 implemented (skills loader, gating, prompt injection, tests).
- 2026-02-02: Phase 2 implemented (plugin loader, tool registration, plugin skills).
- 2026-02-02: Phase 3 complete (API tool metadata is runtime; JSON remains for frontend build).
- 2026-02-02: Live skills smoke test performed via Matty (hello + repo-quick-audit) against local OpenAI-compatible server on port 9292.
- 2026-02-02: Added Phase 4 plan for skill command/dispatch; moved optional enhancements to Phase 5.
- 2026-02-03: Switched skills to Agno `Skills` + `LocalSkills(validate=False)` with OpenClaw JSON5 metadata normalization; prompt injection now uses Agno `<skills_system>` snippet.
