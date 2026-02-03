---
icon: lucide/zap
---

# Skills System

MindRoom supports an OpenClaw-compatible skills system that extends agent capabilities with reusable, metadata-driven components.

## What are Skills?

Skills are self-contained capability modules that can be attached to agents. Unlike tools (which are code functions), skills are defined in markdown files with YAML frontmatter that describe:

- When the skill should be available
- What it does
- How to invoke it
- Dependencies and requirements

## SKILL.md Format

Each skill is defined in a `SKILL.md` file:

```markdown
---
name: my-skill
description: A helpful skill that does something useful
user_invocable: true
disable_model_invocation: false
requires:
  env:
    - MY_API_KEY
  binaries:
    - some-cli
  os:
    - linux
    - macos
dispatch:
  tool: my_tool
  arg_mode: raw
---

# My Skill

Detailed instructions for the skill...
```

## Frontmatter Fields

| Field | Type | Description |
|-------|------|-------------|
| `name` | string | Unique skill identifier |
| `description` | string | Brief description shown to users |
| `user_invocable` | bool | Can users invoke directly? (default: true) |
| `disable_model_invocation` | bool | Hide from AI model? (default: false) |
| `requires.env` | list | Required environment variables |
| `requires.binaries` | list | Required CLI tools |
| `requires.os` | list | Supported operating systems |
| `dispatch.tool` | string | Tool to call when invoked |
| `dispatch.arg_mode` | string | How to pass arguments (raw, json) |

## Skill Locations

Skills are loaded from three locations (in priority order):

1. **User skills**: `~/.mindroom/skills/`
2. **Plugin skills**: From installed plugins
3. **Bundled skills**: Shipped with MindRoom

## Configuring Skills

Add skills to agents in `config.yaml`:

```yaml
agents:
  developer:
    display_name: Developer
    role: A coding assistant
    model: sonnet
    skills:
      - agent-cli-dev
      - code-review
    tools:
      - file
      - shell
```

Or use the dashboard's Agents tab to enable skills visually.

## Conditional Loading

Skills are automatically filtered based on:

### Operating System

```yaml
requires:
  os:
    - macos
    - linux
```

The skill only loads on matching platforms.

### Environment Variables

```yaml
requires:
  env:
    - GITHUB_TOKEN
    - OPENAI_API_KEY
```

The skill only loads when required env vars are set.

### Binary Dependencies

```yaml
requires:
  binaries:
    - docker
    - kubectl
```

The skill only loads when required CLI tools are available.

## Creating a Skill

1. Create a directory in `~/.mindroom/skills/my-skill/`

2. Add `SKILL.md`:

```markdown
---
name: my-skill
description: Does something awesome
requires:
  env:
    - MY_API_KEY
---

# My Skill

Instructions for using this skill...

## Usage

Explain how to use it here.
```

3. Add the skill to an agent's config:

```yaml
agents:
  assistant:
    skills:
      - my-skill
```

4. Restart MindRoom or trigger hot-reload

## Skill vs Tool

| Aspect | Skills | Tools |
|--------|--------|-------|
| Definition | Markdown + YAML | Python code |
| Location | File system | Code/plugins |
| Filtering | Automatic by requirements | Always available |
| Instructions | Rich markdown | Docstrings |
| Invocation | User or model | Model only |

## Best Practices

1. **Keep skills focused** - One skill per capability
2. **Document thoroughly** - The markdown body is shown to users
3. **Declare dependencies** - Use `requires` to prevent loading on incompatible systems
4. **Use descriptive names** - `code-review` not `cr`
