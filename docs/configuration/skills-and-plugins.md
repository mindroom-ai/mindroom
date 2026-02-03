---
icon: lucide/puzzle
---

# Skills and Plugins

Skills are instruction packs that guide agents. Plugins add new tools and can optionally ship skills. Both are configured in `config.yaml`.

## Concepts

- **Skills**: A `SKILL.md` file with instructions, plus optional scripts and references.
- **Plugins**: A folder (or Python package) that registers tools and optionally exposes skills.

Skills do not add capabilities by themselves. Plugins add capabilities through tools.

## Skill sources and precedence

MindRoom loads skills from these locations, in this order:

1. User-managed: `~/.mindroom/skills`
2. Plugin-provided skill directories
3. Bundled repo skills: `skills/`

If multiple skills share the same name, the first one found in this precedence wins. Skills are only loaded for agents that explicitly allow them.

## Configure skills and plugins

```yaml
plugins:
  - ./plugins/my-plugin
  - python:my_skill_pack

agents:
  general:
    display_name: General
    role: A helpful assistant
    model: sonnet
    tools: [file]
    skills: [repo-quick-audit, hello]
```

- `agents.<name>.skills` is an allowlist. If empty or unset, the agent gets no skills.
- `plugins` entries may be absolute paths, paths relative to `config.yaml`, or Python packages.

## Skill format (OpenClaw compatible)

Each skill lives in its own directory with a `SKILL.md` that uses YAML frontmatter.

```
---
name: repo-quick-audit
description: Quick repository audit checklist
metadata: '{openclaw:{requires:{bins:["git"], env:["GITHUB_TOKEN"]}}}'
user-invocable: true
command-dispatch: tool
command-tool: repo_audit.run
command-arg-mode: raw
---

# Repo Quick Audit

1. Check for CI status
2. Review open issues
```

Notes:
- `metadata` is a JSON5 string and can include OpenClaw gating rules.
- `user-invocable`, `command-dispatch`, `command-tool`, and `command-arg-mode` are optional and support `!skill`.

## Eligibility gating (OpenClaw style)

If `metadata.openclaw` is present, these rules apply:

- `always: true` bypasses other checks
- `os: ["linux", "darwin", "windows"]`
- `requires.env`: env var set or credential exists
- `requires.config`: config path is truthy (e.g., `agents.code.tools`)
- `requires.bins`: all binaries must exist in PATH
- `requires.anyBins`: at least one binary must exist in PATH

Ineligible skills are hidden from the agent.

## Using skills at runtime

Agents see available skills in the system prompt and access details using Agno tools:

- `get_skill_instructions(skill_name)`
- `get_skill_reference(skill_name, reference_path)`
- `get_skill_script(skill_name, script_path, execute=False)`

This avoids loading full instructions until needed.

## Skill command dispatch (`!skill`)

Users can run a skill by name:

```
!skill repo-quick-audit --recent
```

Rules:
- The skill must be in the agent allowlist and `user-invocable` must be `true`.
- If `command-dispatch: tool` is set, MindRoom runs the tool directly.
- `command-tool` can be `toolkit_name` (single-function toolkit) or `toolkit_name.function`.
- `command-arg-mode: raw` passes the raw argument string as `command`.
- If `disable-model-invocation: true` and no tool dispatch is configured, the command fails.

## Plugin layout

```
plugins/my-plugin/
  mindroom.plugin.json
  tools.py
  skills/
    my-skill/
      SKILL.md
```

`mindroom.plugin.json`:

```json
{
  "name": "my-plugin",
  "tools_module": "tools.py",
  "skills": ["skills"]
}
```

## Plugin tools module example

```python
from agno.tools import Toolkit
from mindroom.tools_metadata import ToolCategory, register_tool_with_metadata

class DemoTools(Toolkit):
    def __init__(self) -> None:
        super().__init__(name="demo", tools=[self.ping])

    def ping(self, command: str, commandName: str, skillName: str) -> str:
        return f"{commandName}:{skillName}:{command}"

@register_tool_with_metadata(
    name="demo_plugin",
    display_name="Demo Plugin",
    description="Demo plugin tool",
    category=ToolCategory.DEVELOPMENT,
)
def demo_plugin_tools():
    return DemoTools
```

## Python package plugins

You can also point `plugins` to importable packages:

```yaml
plugins:
  - my_skill_pack
  - python:my_skill_pack
  - python:my_skill_pack:plugins/demo
```

MindRoom resolves the package location and looks for `mindroom.plugin.json` in the package (or the optional subpath).
