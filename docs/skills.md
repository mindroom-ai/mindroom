---
icon: lucide/zap
---

# Skills

MindRoom uses Agno's skills system with OpenClaw-compatible metadata. Skills are instruction packs (a `SKILL.md` file) with optional scripts and references that guide agents without adding new code capabilities.

## Skill directory structure

A skill is a directory containing:

```
my-skill/
├── SKILL.md         # Required: frontmatter + instructions
├── scripts/         # Optional: executable scripts
│   └── audit.sh
└── references/      # Optional: reference documents
    └── examples.md
```

Agents access skills via `get_skill_instructions()`, scripts via `get_skill_script()`, and references via `get_skill_reference()`.

## SKILL.md format (OpenClaw compatible)

```markdown
---
name: repo-quick-audit
description: Quick repository audit checklist
metadata: '{openclaw:{requires:{bins:["git"], env:["GITHUB_TOKEN"]}}}'
user-invocable: true
disable-model-invocation: false
command-dispatch: tool
command-tool: repo_audit.run
command-arg-mode: raw
---

# Repo Quick Audit

1. Check CI status
2. Review open issues
```

Notes:

- `metadata` can be a JSON5 string (shown above) or a YAML mapping.
- `user-invocable`, `disable-model-invocation`, and `command-*` also accept snake_case names.

## Frontmatter fields

| Field | Type | Description |
| --- | --- | --- |
| `name` | string | Unique skill identifier |
| `description` | string | Brief summary shown to users/models |
| `metadata` | mapping or JSON5 string | OpenClaw metadata and custom fields |
| `user-invocable` | bool | Allow `!skill` (default: true) |
| `disable-model-invocation` | bool | Prevent model invocation (default: false) |
| `command-dispatch` | `"tool"` | Set to `tool` to run a tool directly |
| `command-tool` | string | Function to call: `function_name`, `toolkit.function_name`, or `toolkit` (if the toolkit exposes exactly one function) |
| `command-arg-mode` | `"raw"` | Argument passing mode; only `raw` is currently supported |

## Eligibility gating (OpenClaw metadata)

If `metadata.openclaw` is present, MindRoom filters skills using these rules:

- `always: true` - Bypasses all checks.
- `os: ["linux", "darwin", "windows"]` - Skill is only available on the listed operating systems.
- `requires.env` - Each listed env var must be set or exist as a credential key.
- `requires.config` - Each listed config path must be truthy (e.g., `agents.code.tools`).
- `requires.bins` - All listed binaries must exist in PATH.
- `requires.anyBins` - At least one listed binary must exist in PATH.

Skills without `metadata.openclaw` are always eligible.

## Skill locations and precedence

MindRoom loads skills from these locations, in this order:

1. Bundled skills: `skills/` at the repository root (if present)
2. Plugin-provided skill directories (see [Plugins](plugins.md))
3. User skills: `~/.mindroom/skills/`

If multiple skills share the same name, the last one wins (user > plugin > bundled). Skill names are matched case-insensitively.

## Configuring skills

Add skills to an agent allowlist in `config.yaml`:

```yaml
agents:
  developer:
    display_name: Developer
    role: A coding assistant
    model: sonnet
    skills:
      - repo-quick-audit
      - code-review
```

If `skills` is empty or unset, the agent gets no skills.

## Using skills at runtime

Agents see available skills in the system prompt and can load details using these tools:

- `get_skill_instructions(skill_name)` - Load the full instructions and metadata for a skill.
- `get_skill_reference(skill_name, reference_path)` - Access a specific reference document from a skill.
- `get_skill_script(skill_name, script_path, execute=False)` - Read or execute a script from a skill. When `execute=True`, optional `args` and `timeout` parameters are available.

## Skill command dispatch (`!skill`)

Users can run a skill by name:

```
!skill repo-quick-audit --recent
```

Agent resolution:

- If you mention an agent (e.g., `@mindroom_code !skill build`), that agent handles the skill.
- If only one agent in the room has the skill enabled, it handles the request.
- If multiple agents have the skill, you must mention one to disambiguate.

Rules:

- The skill must be in the agent allowlist and `user-invocable` must be `true`.
- If `command-dispatch: tool` is set, MindRoom runs the tool directly.
- If `disable-model-invocation: true` and no tool dispatch is configured, the command fails.

## Skill vs tool

| Aspect | Skills | Tools |
| --- | --- | --- |
| Definition | Markdown + YAML | Python code |
| Location | File system | Code/plugins |
| Filtering | Automatic by requirements | Always available |
| Instructions | Rich markdown | Docstrings |
| Invocation | User or model | Model only |

## Hot reloading

MindRoom polls skill directories every second. When a `SKILL.md` file is added, removed, or modified, the skill cache is automatically cleared so agents pick up the new instructions on their next request.

## Best practices

1. Keep skills focused: one skill per capability.
2. Declare dependencies with `metadata.openclaw.requires` to ensure skills only appear when prerequisites are met.
3. Use descriptive, hyphenated names like `code-review` or `repo-quick-audit`.
