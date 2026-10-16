# Mindroom Skills + Plugins Plan

This archived plan records why MindRoom splits extensions into skills and plugins following OpenClaw's model, and the decisions behind the skills integration.

## Why

Tools were registered only in core code and selected per agent in `config.yaml`, so adding a tool meant changing MindRoom.
The goal was OpenClaw's model, so OpenClaw skills carry over: skills are instruction packs in OpenClaw's `SKILL.md` format that add no capabilities and stay reusable across agents, while plugins register new tools without hard-coded imports in core and may ship skills alongside them.

## Decisions

- Skill orchestration uses Agno's `Skills` prompt snippet and `get_skill_*` tools instead of a custom `<available_skills>` prompt block, so Agno's snippet is the single source of truth for the skills prompt.
  The skills section must stay compact to avoid prompt bloat.
- OpenClaw writes `metadata` as a single-line JSON5 string in the `SKILL.md` frontmatter, so skills load through Agno with validation disabled (`LocalSkills(validate=False)`) and MindRoom normalizes the metadata to preserve it.

## Assumptions

- `~/.mindroom/skills` and plugin locations are trusted: plugins are executable code and skills are instructions.
