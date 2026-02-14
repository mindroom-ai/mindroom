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

### 2. Verify Configuration Docs Against Code

Compare docs under `docs/configuration/` against the Pydantic models in `src/mindroom/config.py`. Don't rely on a fixed list of files — discover what exists in both locations and check they match.

```bash
# Find all config models
grep -r "class.*BaseModel" src/mindroom/config.py -A 15

# Find all config doc files
ls docs/configuration/
```

Check:
- All config keys documented, types and defaults match code
- No models exist without corresponding docs (or vice versa)
- Example YAML would actually work

### 3. Verify Architecture Docs Against Source

```bash
# What source files actually exist?
git ls-files "src/mindroom/**/*.py"
```

Check `docs/architecture/` and the Architecture section of `CLAUDE.md`:
- Listed modules exist and descriptions match what the code does
- No source modules are missing from the listings
- Both locations can drift independently — check both

### 4. Verify Feature Docs Against Implementation

For every doc file under `docs/`, find the corresponding source module(s) and check they agree. Don't assume a fixed mapping — discover it:

```bash
ls docs/*.md docs/*/
ls src/mindroom/*.py src/mindroom/*/
```

Look for docs that describe features the code no longer has, or code with features the docs don't cover.

### 5. Check Examples

For examples in any doc:
- Would the `config.yaml` snippets actually work?
- Are names and references realistic and current?
- Do examples use current syntax (not deprecated options)?
- Do setup snippets reference real files/flags/commands that exist in this repo/CLI?
- Do NOT flag AI model names as invalid based on your training cutoff — look them up online first

### 6. Cross-Reference Consistency

The same info appears in multiple places. Check for conflicts between README.md, `docs/`, and `CLAUDE.md`.

Also verify that script paths and file references in CLAUDE.md and `docs/deployment/` match the actual filesystem layout.

### 6b. Verify Deployment Docs

Check `docs/deployment/` files against actual Dockerfiles, Helm charts, scripts, and environment variable defaults in `src/mindroom/constants.py`.

### 7. Self-Check This Prompt

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

- **File**: path/to/file.md:42
- **Problem**: What's wrong
- **Fix**: What to change
- **Verify**: How to confirm
```
