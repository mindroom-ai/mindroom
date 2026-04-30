# Skills

MindRoom uses Agno's skills system with OpenClaw-compatible metadata. Skills are instruction packs (a `SKILL.md` file) with optional scripts and references that guide agents without adding new code capabilities.

## Skill directory structure

A skill is a directory containing:

```
my-skill/
├── SKILL.md         # Required: instructions; YAML frontmatter is recommended
├── scripts/         # Optional: executable scripts
│   └── audit.sh
└── references/      # Optional: reference documents
    └── examples.md
```

Agents access skills via `get_skill_instructions()`, scripts via `get_skill_script()`, and references via `get_skill_reference()`.

## SKILL.md format (OpenClaw compatible)

```
---
name: repo-quick-audit
description: Quick repository audit checklist
metadata: '{openclaw:{requires:{bins:["git"], env:["GITHUB_TOKEN"]}}}'
---

# Repo Quick Audit

1. Check CI status
2. Review open issues
```

Notes:

- `metadata` can be a JSON5 string (shown above) or a YAML mapping.
- If `name` is omitted, MindRoom falls back to the skill directory name.
- If `description` is omitted or blank, MindRoom falls back to the resolved skill name.
- If YAML frontmatter is omitted entirely, the skill still loads with those same name/description fallbacks. Frontmatter is still recommended for clearer listings and metadata.

## Frontmatter fields

| Field           | Type                    | Description                                                                           |
| --------------- | ----------------------- | ------------------------------------------------------------------------------------- |
| `name`          | string                  | Unique skill identifier                                                               |
| `description`   | string                  | Brief summary shown to users/models; defaults to the skill name when omitted or blank |
| `metadata`      | mapping or JSON5 string | OpenClaw metadata and custom fields                                                   |
| `license`       | string                  | Informational only; accepted but not used by the runtime                              |
| `compatibility` | string                  | Informational only; accepted but not used by the runtime                              |
| `allowed-tools` | list                    | Reserved; accepted in frontmatter but not enforced by the runtime                     |

## Eligibility gating (OpenClaw metadata)

If `metadata.openclaw` is present, MindRoom filters skills using these rules:

- `os: ["linux", "darwin", "windows"]`
- `always: true` bypasses `requires`, but it does not bypass an OS mismatch
- `requires.env`: env var set or credential key exists
- `requires.config`: config path is truthy (e.g., `agents.code.tools`)
- `requires.bins`: all binaries must exist in PATH
- `requires.anyBins`: at least one binary must exist in PATH

Skills without `metadata.openclaw` are always eligible.

## Skill locations and precedence

MindRoom resolves skills for each agent from these locations, in this order:

1. Bundled skills: `skills/` at the repository root (if present)
1. Plugin-provided skill directories (see [Plugins](https://docs.mindroom.chat/plugins/index.md))
1. User skills: `~/.mindroom/skills/`
1. Agent workspace skills: `<storage>/agents/<agent>/workspace/skills/`

If multiple skills share the same name, the last one wins (agent workspace > user > plugin > bundled).

Agent workspace skills are only available to the owning agent at runtime. They do not appear in the global skills API or dashboard listing because those views are not agent-scoped.

## Configuring skills

Add skills to an agent allowlist in `config.yaml`:

```
agents:
  developer:
    display_name: Developer
    role: A coding assistant
    model: sonnet
    skills:
      - repo-quick-audit
      - code-review
```

The `skills:` list is an allowlist for bundled, plugin, and user skills.
If `skills` is empty or unset, the agent gets no bundled, plugin, or user skills.
Workspace skills under `<storage>/agents/<agent>/workspace/skills/` are still auto-loaded for that agent.
This lets an agent create or receive skills in its own workspace without editing `config.yaml`.

Workspace auto-loading is a runtime capability, not a proactive behavior policy.
If you want agents to create skills on their own when they notice reusable workflows, add that guidance to the agent's prompt or instructions.

## Using skills at runtime

Agents see available skills in the system prompt and can load details using these tools:

- `get_skill_instructions(skill_name)` - Load the full instructions for a skill
- `get_skill_reference(skill_name, reference_path)` - Access reference documentation
- `get_skill_script(skill_name, script_path, execute=False, args=None, timeout=30)` - Read or execute scripts

Workspace skill scripts can be read with `get_skill_script(..., execute=False)`.
Workspace skill scripts cannot be executed through `get_skill_script(..., execute=True)`.
Agents that have shell or file execution permissions can still read and execute workspace files through their normal authorized tools.

## Skill vs tool

| Aspect       | Skills                    | Tools            |
| ------------ | ------------------------- | ---------------- |
| Definition   | Markdown + YAML           | Python code      |
| Location     | File system               | Code/plugins     |
| Filtering    | Automatic by requirements | Always available |
| Instructions | Rich markdown             | Docstrings       |
| Invocation   | Model via skill tools     | Model only       |

## Hot reloading

MindRoom polls skill directories every second. When a `SKILL.md` file is added, removed, or modified, the skill cache is automatically cleared so agents pick up the new instructions on their next request.
For workspace skills created during an agent turn, assume they become available on the next agent run rather than in the same response.

## Best practices

1. Keep skills focused - one skill per capability
1. Declare dependencies with `metadata.openclaw.requires`
1. Use descriptive names like `code-review`
