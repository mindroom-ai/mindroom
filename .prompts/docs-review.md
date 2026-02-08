Review documentation for accuracy, completeness, and consistency. Focus on things that require judgment—automated checks handle the rest.

## What's Already Automated

Don't waste time on these—CI and pre-commit hooks handle them:

- **README CLI output**: `markdown-code-runner` regenerates CLI help blocks via `docs/run_markdown_code_runner.py`
- **Linting/formatting**: Handled by pre-commit

## What This Review Is For

Focus on things that require judgment:

1. **Accuracy**: Does the documentation match what the code actually does?
2. **Completeness**: Are there undocumented features, options, or behaviors?
3. **Clarity**: Would a new user understand this? Are examples realistic?
4. **Consistency**: Do different docs contradict each other?
5. **Freshness**: Has the code changed in ways the docs don't reflect?

## Review Process

### 1. Check Recent Changes

```bash
# What changed recently that might need doc updates?
git log --oneline -20 | grep -iE "feat|fix|add|remove|change|option"

# What code files changed?
git diff --name-only HEAD~20 | grep "\.py$"
```

Look for new features, changed defaults, renamed options, or removed functionality.

### 2. Verify docs/configuration/ Against Pydantic Models

Compare against Pydantic models in `src/mindroom/config.py`:

```bash
# Find all config models
grep -r "class.*BaseModel" src/mindroom/config.py -A 15
```

Check across `docs/configuration/`:
- `agents.md`: Agent config keys match `AgentConfig` model
- `models.md`: Model config keys match `ModelConfig` model
- `router.md`: Router config matches `RouterConfig` model
- `teams.md`: Team config matches `TeamConfig` model
- `index.md`: Top-level config matches `MindRoomConfig` model
- Types, defaults, and required fields match code
- Example YAML would actually work with `config.yaml`

### 3. Verify docs/architecture/ and CLAUDE.md

```bash
# What source files actually exist?
git ls-files "src/mindroom/**/*.py"
```

Check **both** `docs/architecture/` and `CLAUDE.md` (Architecture section):
- Listed modules exist (`bot.py`, `agents.py`, `routing.py`, `teams.py`, `memory/`, `tools/`, `matrix/`, etc.)
- No modules are missing from the listings
- Descriptions match what the code does
- `docs/architecture/matrix.md` reflects actual Matrix client behavior
- `docs/architecture/orchestration.md` reflects `MultiAgentOrchestrator` behavior

Both locations have architecture listings that can drift independently.

### 4. Verify Tool Documentation

Check `docs/tools/`:
- `builtin.md`: Lists all built-in tool groups available in `src/mindroom/tools/`
- `mcp.md`: MCP tool integration docs match actual implementation
- Tool names in docs match what's accepted in `config.yaml` `tools:` lists

### 5. Verify Other Docs

- `docs/memory.md`: Matches `src/mindroom/memory/` implementation (agent, room, team scopes)
- `docs/voice.md`: Matches `src/mindroom/voice_handler.py`
- `docs/skills.md`: Matches `src/mindroom/skills.py`
- `docs/scheduling.md`: Matches `src/mindroom/scheduling.py`
- `docs/plugins.md`: Matches `src/mindroom/plugins.py`
- `docs/cli.md`: Matches `src/mindroom/cli.py` options

### 6. Check Examples

For examples in any doc:
- Would the `config.yaml` snippets actually work?
- Are agent names, tool names, and model references realistic?
- Do examples use current syntax (not deprecated options)?

### 7. Cross-Reference Consistency

The same info appears in multiple places. Check for conflicts:
- README.md vs docs/index.md
- docs/configuration/ vs CLAUDE.md config examples
- docs/architecture/ vs CLAUDE.md architecture section
- Tool lists across different docs

### 8. Self-Check This Prompt

This prompt can become outdated too. If you notice:
- New automated checks that should be listed above
- New doc files that need review guidelines
- Patterns that caused issues

Include prompt updates in your fixes.

## Output Format

Categorize findings:

1. **Critical**: Wrong info that would break user workflows
2. **Inaccuracy**: Technical errors (wrong defaults, paths, types)
3. **Missing**: Undocumented features or options
4. **Outdated**: Was true, no longer is
5. **Inconsistency**: Docs contradict each other
6. **Minor**: Typos, unclear wording

For each issue, provide a ready-to-apply fix:

```
### Issue: [Brief description]

- **File**: docs/configuration/agents.md:42
- **Problem**: `tools` field documents `web` but the actual tool group is `web_search`
- **Fix**: Replace `web` with `web_search` in the tools list
- **Verify**: Check `src/mindroom/tools/` for actual group names
```
