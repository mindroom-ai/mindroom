Summary: One meaningful duplication candidate exists in `src/mindroom/tool_approval.py`.
`tool_requires_approval_for_openai_compat` and `evaluate_tool_approval` both walk `config.tool_approval.rules`, match `rule.match` with `fnmatchcase`, interpret `rule.action`, and fall back to `tool_approval.default`.
The remaining symbols are either data carriers, thin public wrappers over `approval_manager`, or related to broader approval/script-loading behavior without a same-purpose duplicate.

```coverage-tsv
qualname	kind	lines	status	search_terms	candidates_checked
ToolApprovalScriptError	class	lines 66-67	related-only	ToolApprovalScriptError ToolApprovalTransportError RuntimeError approval script error	src/mindroom/approval_manager.py:65; src/mindroom/tool_system/tool_hooks.py:490
ToolApprovalCall	class	lines 71-81	not-a-behavior-symbol	ToolApprovalCall dataclass request_tool_approval_for_call callers	src/mindroom/tool_system/tool_hooks.py:478
MatrixApprovalAction	class	lines 85-93	not-a-behavior-symbol	MatrixApprovalAction dataclass handle_matrix_approval_action approval inbound	src/mindroom/approval_inbound.py:91
_terminal_decision	function	lines 96-102	related-only	ApprovalDecision resolved_at datetime.now _new_decision terminal decision	src/mindroom/approval_manager.py:1203
_check_callable_from_module	function	lines 105-116	none-found	callable check(tool_name arguments agent_name) getattr module check	none
_load_script_module	function	lines 119-155	related-only	spec_from_file_location module_from_spec exec_module module cache	src/mindroom/tool_system/metadata.py:862; src/mindroom/tool_system/plugins.py:400
_clear_script_cache	function	lines 158-161	none-found	script cache clear lock approval _SCRIPT_CACHE	none
tool_requires_approval_for_openai_compat	function	lines 164-179	duplicate-found	tool_approval.rules fnmatchcase rule.match rule.action require_approval default	src/mindroom/tool_approval.py:199; src/mindroom/approval_transport.py:56
resolve_tool_approval_approver	function	lines 182-196	related-only	requester_id startswith colon is_agent_id bot_accounts mindroom_user_id	src/mindroom/matrix/identity.py:244; src/mindroom/authorization.py:61
evaluate_tool_approval	async_function	lines 199-235	duplicate-found	tool_approval.rules fnmatchcase timeout_days rule.action require_approval script	src/mindroom/tool_approval.py:164; src/mindroom/approval_transport.py:56
request_tool_approval_for_call	async_function	lines 238-271	related-only	request_tool_approval_for_call request_approval ToolApprovalCall approval_decision	src/mindroom/tool_system/tool_hooks.py:464; src/mindroom/approval_manager.py:262
is_process_approval_card	function	lines 274-277	related-only	knows_in_memory_approval_card get_approval_store process approval card	src/mindroom/approval_manager.py:1060
is_process_active_approval_card	function	lines 280-283	related-only	has_active_in_memory_approval_card get_approval_store active approval card	src/mindroom/approval_manager.py:1070; src/mindroom/approval_inbound.py:125
handle_matrix_approval_action	async_function	lines 286-308	related-only	handle_live_approval_id_response handle_card_response sanitized reason approval action	src/mindroom/approval_inbound.py:66; src/mindroom/approval_manager.py:399; src/mindroom/approval_manager.py:422
initialize_approval_runtime	function	lines 311-328	related-only	initialize_approval_runtime initialize_approval_store bind_approval_runtime	src/mindroom/approval_transport.py:91; src/mindroom/approval_manager.py:1227
expire_orphaned_approval_cards_on_startup	async_function	lines 331-336	related-only	discard_pending_on_startup expire_orphaned approval startup	src/mindroom/approval_transport.py:403; src/mindroom/approval_manager.py:367
shutdown_approval_runtime	async_function	lines 339-341	related-only	shutdown_approval_runtime shutdown_approval_store shutdown_approval_manager	src/mindroom/orchestrator.py:1652; src/mindroom/tool_approval.py:344
shutdown_approval_store	async_function	lines 344-349	related-only	shutdown_approval_store shutdown_approval_manager clear script cache	src/mindroom/approval_manager.py:885; src/mindroom/tool_approval.py:158
```

Findings:

1. Approval rule resolution is duplicated inside the same module.
   `src/mindroom/tool_approval.py:164` implements the OpenAI-compatible visibility check by initializing `require_approval` from `config.tool_approval.default`, iterating ordered rules, matching `rule.match` with `fnmatchcase`, interpreting `rule.action`, and falling back to the default.
   `src/mindroom/tool_approval.py:199` repeats the same rule/default/action walk before adding timeout and script execution.
   These are functionally the same policy lookup for action rules; the only difference is that `evaluate_tool_approval` also returns timeout seconds and executes script-backed rules, while `tool_requires_approval_for_openai_compat` treats any matching script rule as approval-required because `/v1` tool listing cannot execute per-call policy.

2. Approval timeout scanning is related but not a direct duplicate.
   `src/mindroom/approval_transport.py:56` scans `config.tool_approval.rules` for the maximum configured `timeout_days` to choose a startup cleanup lookback.
   This overlaps with `evaluate_tool_approval` at `src/mindroom/tool_approval.py:209` and `src/mindroom/tool_approval.py:214`, but the behavior is intentionally different: startup cleanup needs a conservative maximum across all rules, while per-call evaluation needs the first matching rule's timeout.

3. Dynamic module loading is related but should remain separate for now.
   `_load_script_module` at `src/mindroom/tool_approval.py:119` and plugin loaders at `src/mindroom/tool_system/metadata.py:862` and `src/mindroom/tool_system/plugins.py:400` all use `spec_from_file_location`, `module_from_spec`, and `exec_module`.
   The plugin paths manage package chains, `sys.modules`, registration scoping, and reload recovery, while approval scripts use a private mtime-keyed cache and intentionally anonymous module names.
   A shared helper would need enough parameters for cache behavior, package context, module registration, and error wording that it would likely obscure the current code.

Proposed generalization:

1. Add a small private helper in `src/mindroom/tool_approval.py`, for example `_matching_tool_approval_rule(config, tool_name)`, returning the first matching rule or `None`.
2. Use it from both `tool_requires_approval_for_openai_compat` and `evaluate_tool_approval`.
3. Keep timeout handling and script execution in `evaluate_tool_approval`.
4. Keep OpenAI-compatible script-rule behavior explicit: matching script rule means return `True`.
5. Add focused tests that compare direct action rules, script rules, no-match defaults, and first-match precedence across both callers.

Risk/tests:

The main risk is changing first-match precedence or the script-rule behavior used by OpenAI-compatible tool listing.
Tests should cover ordered rules with mixed `auto_approve`, `require_approval`, and script entries; default fallback for both `auto_approve` and `require_approval`; and per-rule timeout behavior in `evaluate_tool_approval`.
No refactor is recommended for the dynamic module-loading or manager-wrapper related-only areas without additional repeated call sites.
