## Summary

The only meaningful duplication found for `src/mindroom/config/approval.py` is the repeated `timeout_days` bool-rejection validator on both approval config models.
Several other config modules use related blank-string, mutual-exclusion, and numeric-validation patterns, but their semantics differ enough that a shared helper is not clearly justified for this file alone.

## Coverage TSV

```coverage-tsv
qualname	kind	lines	status	search_terms	candidates_checked
ApprovalRuleConfig	class	lines 14-59	duplicate-found	approval config model validators timeout_days bool exactly one blank match script	src/mindroom/config/approval.py:14; src/mindroom/config/approval.py:62; src/mindroom/config/models.py:216; src/mindroom/mcp/config.py:47
ApprovalRuleConfig.validate_match	method	lines 26-31	related-only	blank string pydantic field validators not value.strip must not be empty	src/mindroom/config/plugin.py:26; src/mindroom/config/models.py:148; src/mindroom/config/matrix.py:82; src/mindroom/mcp/config.py:16
ApprovalRuleConfig.validate_script	method	lines 35-40	related-only	optional blank string validators value is not None not value.strip path script command url	src/mindroom/config/agent.py:142; src/mindroom/mcp/config.py:93; src/mindroom/mcp/config.py:104; src/mindroom/config/matrix.py:245
ApprovalRuleConfig.reject_bool_timeout_days	method	lines 44-49	duplicate-found	reject bool numeric timeout_days isinstance value bool pydantic mode before	src/mindroom/config/approval.py:71; src/mindroom/tool_system/metadata.py:208; src/mindroom/tool_system/metadata.py:341; src/mindroom/knowledge/manager.py:348
ApprovalRuleConfig.validate_action_or_script	method	lines 52-59	related-only	exactly one mutually exclusive model_validator one of action script	src/mindroom/config/models.py:206; src/mindroom/config/models.py:244; src/mindroom/config/models.py:282; src/mindroom/config/agent.py:304; src/mindroom/config/agent.py:437; src/mindroom/api/sandbox_runner.py:1399
ToolApprovalConfig	class	lines 62-78	duplicate-found	approval config model validators timeout_days bool top-level rules	src/mindroom/config/approval.py:14; src/mindroom/config/approval.py:62; src/mindroom/tool_approval.py:209; src/mindroom/approval_transport.py:58
ToolApprovalConfig.reject_bool_timeout_days	method	lines 73-78	duplicate-found	reject bool numeric timeout_days isinstance value bool pydantic mode before	src/mindroom/config/approval.py:42; src/mindroom/tool_system/metadata.py:208; src/mindroom/tool_system/metadata.py:341; src/mindroom/knowledge/manager.py:348
```

## Findings

1. `timeout_days` bool rejection is duplicated between the two approval models.

`ApprovalRuleConfig.reject_bool_timeout_days` in `src/mindroom/config/approval.py:42` and `ToolApprovalConfig.reject_bool_timeout_days` in `src/mindroom/config/approval.py:71` are functionally identical.
Both run as Pydantic `mode="before"` validators for a `TimeoutDays` field, reject `bool` before numeric coercion, raise the same message, and otherwise return the original object unchanged.
The only difference is the owning model and whether the field is optional.
That difference does not affect the helper logic because `None` already passes through unchanged.

2. Blank string validators are related but not duplicated enough to refactor here.

`ApprovalRuleConfig.validate_match` in `src/mindroom/config/approval.py:24` matches the common config pattern of stripping or checking `str.strip()` and raising `ValueError` on empty input.
Comparable examples include `PluginEntryConfig.validate_path` in `src/mindroom/config/plugin.py:26`, `ToolConfigEntry.validate_name` in `src/mindroom/config/models.py:148`, `MatrixSpaceConfig.validate_name` in `src/mindroom/config/matrix.py:82`, and `validate_mcp_identifier` in `src/mindroom/mcp/config.py:16`.
These are only related because several validators normalize or return stripped values, while `validate_match` preserves the original non-empty string.

`ApprovalRuleConfig.validate_script` in `src/mindroom/config/approval.py:33` is similarly related to optional string checks in `AgentPrivateConfig.validate_template_dir` at `src/mindroom/config/agent.py:142` and transport-field checks in `src/mindroom/mcp/config.py:93` and `src/mindroom/mcp/config.py:104`.
The candidate validators either normalize returned values or validate transport-specific combinations, so a shared helper would obscure field-specific behavior.

3. Exactly-one validation is related to existing mutual-exclusion helpers, but the semantics are different.

`ApprovalRuleConfig.validate_action_or_script` in `src/mindroom/config/approval.py:51` requires exactly one of `action` or `script`.
The closest config helper is `_validate_compaction_threshold_choice` in `src/mindroom/config/models.py:206`, used by `CompactionOverrideConfig.validate_threshold_choice` at `src/mindroom/config/models.py:244` and `CompactionConfig.validate_threshold_choice` at `src/mindroom/config/models.py:282`, but that helper allows neither value to be set.
History validators in `src/mindroom/config/agent.py:304`, `src/mindroom/config/agent.py:437`, and `src/mindroom/config/models.py:424` also enforce mutual exclusion only.
Because approval requires one field to be present, not just at most one, this is related-only.

## Proposed Generalization

Introduce a tiny private helper in `src/mindroom/config/approval.py`, for example `_reject_bool_timeout_days(value: object) -> object`, and have both `timeout_days` validators delegate to it.
No cross-module helper is recommended because the matching duplication is local to approval config and broader numeric validation code has different error types, finite-number rules, and user-facing messages.

No refactor is recommended for blank-string or exactly-one validation at this time.

## Risk/tests

The local helper extraction would be low risk because it preserves both existing validator decorators, field names, optionality, and error text.
Tests should cover boolean `timeout_days` rejection on both `ToolApprovalConfig(timeout_days=True)` and `ApprovalRuleConfig(match="*", action="auto_approve", timeout_days=True)`.
Existing tests for blank `match`, blank `script`, both `action` and `script`, and neither `action` nor `script` should continue to pin the field-specific messages.
