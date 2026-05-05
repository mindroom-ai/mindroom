## Summary

No meaningful duplication found.
`validate_plugin_name` is the canonical plugin identifier validator and is already reused by plugin manifest parsing and plugin runtime state resolution.
Nearby name validators cover different domains with different accepted character sets, so they are related validation logic rather than duplicated plugin-name behavior.

## Coverage TSV

```coverage-tsv
qualname	kind	lines	status	search_terms	candidates_checked
validate_plugin_name	function	lines 10-19	related-only	validate_plugin_name; PLUGIN_NAME_PATTERN; Invalid plugin name; lowercase ASCII letters; plugin_name.strip; fullmatch name validators	src/mindroom/tool_system/plugin_imports.py:274; src/mindroom/tool_system/plugin_imports.py:280; src/mindroom/hooks/context.py:71; src/mindroom/tool_system/runtime_context.py:539; src/mindroom/api/skills.py:17; src/mindroom/api/skills.py:107; src/mindroom/config/main.py:75; src/mindroom/config/main.py:447; src/mindroom/config/plugin.py:26; src/mindroom/config/models.py:148; src/mindroom/tool_system/plugin_imports.py:344
```

## Findings

No real duplication found.

Related-only candidates:

- `src/mindroom/api/skills.py:17` and `src/mindroom/api/skills.py:107` validate user-created skill names by trimming whitespace, rejecting empty names, and matching lowercase alphanumerics with hyphens.
  This resembles plugin-name validation structurally, but the domain and rules differ: skill names must start and end with a letter or digit and do not allow underscores.
- `src/mindroom/config/main.py:75` and `src/mindroom/config/main.py:447` validate agent, team, and MCP server names with alphanumeric/underscore rules.
  This is another identifier validator, but it allows uppercase letters and rejects hyphens.
- `src/mindroom/config/plugin.py:26` and `src/mindroom/config/models.py:148` trim and reject empty plugin paths and tool names.
  These share only the generic non-empty string normalization pattern, not plugin identifier syntax.
- `src/mindroom/tool_system/plugin_imports.py:344` builds import-safe plugin slugs by replacing invalid characters.
  It is downstream sanitization for module names, not user-facing validation, and preserving its fallback behavior matters for relative module path parts.

Existing direct uses confirm centralization rather than duplication:

- `src/mindroom/tool_system/plugin_imports.py:274` and `src/mindroom/tool_system/plugin_imports.py:280` parse manifest names and call `validate_plugin_name`.
- `src/mindroom/hooks/context.py:71` validates plugin names before deriving plugin state roots.
- `src/mindroom/tool_system/runtime_context.py:539` validates plugin names before creating plugin-scoped runtime context.

## Proposed Generalization

No refactor recommended.
The current helper is already focused and reused where plugin identity matters.
Generalizing it with skill, entity, tool, or path validators would either weaken domain-specific rules or require a parameterized identifier framework with little immediate maintenance benefit.

## Risk/Tests

No production change is recommended, so no behavior risk is introduced.
If future work changes plugin-name syntax, tests should cover manifest parsing failures, plugin state-root resolution, runtime plugin context creation, and import package naming for names containing hyphens and underscores.
