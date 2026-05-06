## Summary

One small duplication candidate exists: `PluginEntryConfig.validate_path` repeats the common config-validator behavior of trimming a string and rejecting an empty result.
The hook override and plugin entry model shapes are related to hook registry/import consumers, but I did not find duplicated behavior that warrants a shared abstraction.

## Coverage TSV

```coverage-tsv
qualname	kind	lines	status	search_terms	candidates_checked
HookOverrideConfig	class	lines 10-15	related-only	enabled priority timeout_ms hooks override RegisteredHook HookMetadata	src/mindroom/hooks/registry.py:69, src/mindroom/hooks/registry.py:79, src/mindroom/hooks/decorators.py:25, src/mindroom/hooks/types.py:155
PluginEntryConfig	class	lines 18-34	related-only	PluginEntryConfig plugins settings hooks enabled path load plugin config	src/mindroom/config/main.py:373, src/mindroom/config/main.py:429, src/mindroom/tool_system/plugin_imports.py:79, src/mindroom/hooks/registry.py:69
PluginEntryConfig.validate_path	method	lines 28-34	duplicate-found	field_validator path strip must not be empty return stripped normalized	src/mindroom/config/agent.py:142, src/mindroom/config/models.py:148, src/mindroom/config/matrix.py:82, src/mindroom/config/approval.py:24
```

## Findings

### Repeated non-empty string normalization in config validators

`src/mindroom/config/plugin.py:28` strips `PluginEntryConfig.path`, rejects an empty result, and returns the normalized string.
The same behavior appears in nearby config models:

- `src/mindroom/config/agent.py:142` strips `private.template_dir`, rejects empty values, and returns the stripped string.
- `src/mindroom/config/models.py:148` strips `ToolConfigEntry.name`, rejects empty values, and returns the stripped string.
- `src/mindroom/config/matrix.py:82` strips `MatrixSpaceConfig.name`, rejects empty values, and returns the stripped string.
- `src/mindroom/config/approval.py:24` validates that `tool_approval.rules[].match` is non-empty after stripping, but preserves the original value rather than returning the stripped value.

The first three candidates duplicate the same functional behavior as `PluginEntryConfig.validate_path`: normalize surrounding whitespace and reject blank config strings with a field-specific error message.
`ApprovalRuleConfig.validate_match` is related but not identical because it only validates non-emptiness and does not normalize the returned value.

### Hook override model fields are not duplicated behavior

`HookOverrideConfig` carries optional per-hook overrides for `enabled`, `priority`, and `timeout_ms`.
`src/mindroom/hooks/registry.py:69` consumes those overrides while compiling `RegisteredHook` records, and `src/mindroom/hooks/decorators.py:25` / `src/mindroom/hooks/types.py:155` define hook metadata/runtime fields with matching names.
This is shared domain vocabulary, not duplicated implementation.
The config model represents user-authored override values, while the hook metadata and registered hook types represent discovered and compiled runtime state.

### Plugin entry model is consumed in one plugin-loading flow

`PluginEntryConfig` is the root config representation for plugins.
`src/mindroom/config/main.py:429` normalizes legacy string entries into mappings before validation, `src/mindroom/tool_system/plugin_imports.py:79` resolves enabled entries into plugin bases, and `src/mindroom/hooks/registry.py:69` consumes its hook overrides.
These are distinct phases of one pipeline rather than duplicated model logic.

## Proposed Generalization

A very small helper could live in `src/mindroom/config/validators.py`, for example `normalize_non_empty_string(value: str, field_name: str) -> str`.
It could replace the identical strip/reject/return validators in plugin path, tool name, matrix space name, and private template directory.

No refactor is recommended specifically for `HookOverrideConfig` or `PluginEntryConfig`.
The config shape is already narrow, and sharing it with runtime hook metadata would blur authored config with compiled hook state.

## Risk/tests

The main behavior risk is accidentally changing whitespace preservation for validators that currently validate but do not normalize, especially `ApprovalRuleConfig.validate_match`.
Tests should cover accepted trimmed values and blank-value validation errors for plugin paths, tool names, matrix space names, and private template directories before any helper extraction.
No production code was changed for this audit.
