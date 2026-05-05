Summary: One meaningful duplication candidate was found.
`src/mindroom/tool_system/tool_calls.py` and `src/mindroom/knowledge/redaction.py` both redact credential-bearing URLs and authorization/token text, with different redaction policies for their call sites.
There is also related, but not directly duplicate, JSON-safe payload normalization in `src/mindroom/llm_request_logging.py` and approval-preview reuse of the tool-call sanitizer in `src/mindroom/approval_manager.py`.

```coverage-tsv
qualname	kind	lines	status	search_terms	candidates_checked
ToolCallRecordDict	class	lines 102-121	related-only	ToolCallRecordDict fields correlation_id duration_ms reply_to_event_id JSONValue	src/mindroom/llm_request_logging.py:183; src/mindroom/hooks/context.py:683; src/mindroom/handled_turns.py:54
ToolCallRecord	class	lines 125-177	related-only	tool call record dataclass sanitized durable JSON record	src/mindroom/llm_request_logging.py:279; src/mindroom/history/types.py:124; src/mindroom/hooks/context.py:683
ToolCallRecord.as_dict	method	lines 146-177	related-only	as_dict optional fields dataclass payload to dict	src/mindroom/history/types.py:164; src/mindroom/handled_turns.py:785; src/mindroom/llm_request_logging.py:111
_unrepresentable_placeholder	function	lines 180-181	none-found	unrepresentable placeholder repr str BaseException	none
_safe_str	function	lines 184-188	related-only	safe str except BaseException unrepresentable	src/mindroom/approval_manager.py:106; src/mindroom/knowledge/redaction.py:44
_safe_repr	function	lines 191-195	related-only	safe repr except BaseException JSON fallback	src/mindroom/llm_request_logging.py:111; src/mindroom/workers/runtime.py:33
_normalize_secret_key	function	lines 198-202	duplicate-found	normalize secret key camel snake api_key token password	src/mindroom/llm_request_logging.py:40; src/mindroom/tool_system/metadata.py:325; src/mindroom/tools/*.py password config fields
_is_secret_key	function	lines 205-220	duplicate-found	secret key api_key auth_token password access_token client_secret	src/mindroom/llm_request_logging.py:40; src/mindroom/tool_system/metadata.py:325; src/mindroom/tools/x.py:25
_is_secret_query_key	function	lines 223-225	duplicate-found	URL query secret sig signature token access_key	src/mindroom/knowledge/redaction.py:22; src/mindroom/knowledge/redaction.py:44
_redact_secret_assignment	function	lines 228-244	related-only	redact key=value secret assignment authorization api key	src/mindroom/knowledge/redaction.py:44; src/mindroom/approval_manager.py:122
_truncate_text	function	lines 247-250	related-only	truncate string marker max length	src/mindroom/approval_manager.py:119; src/mindroom/approval_manager.py:125
_redact_matched_group	function	lines 253-258	related-only	regex match token group redaction	src/mindroom/knowledge/redaction.py:48; src/mindroom/knowledge/redaction.py:65
_redact_url_credentials	function	lines 261-290	duplicate-found	redact URL credentials userinfo query secret urlparse urlunparse	src/mindroom/knowledge/redaction.py:22; src/mindroom/knowledge/redaction.py:44; src/mindroom/api/knowledge.py:305
sanitize_failure_text	function	lines 293-300	duplicate-found	sanitize failure text bearer api key token URL credentials authorization	src/mindroom/knowledge/redaction.py:44; src/mindroom/api/knowledge.py:137; src/mindroom/knowledge/manager.py:694
sanitize_failure_value	function	lines 303-328	related-only	recursive JSON safe sanitize dict list secret keys max collection items	src/mindroom/llm_request_logging.py:111; src/mindroom/approval_manager.py:153; src/mindroom/tool_system/events.py:56
_sanitize_duration_ms	function	lines 331-334	related-only	duration_ms round finite monotonic milliseconds	src/mindroom/hooks/execution.py:212; src/mindroom/coalescing.py:335; src/mindroom/history/runtime.py:276
_safe_error_message	function	lines 337-338	related-only	error message sanitize str exception	src/mindroom/knowledge/manager.py:1075; src/mindroom/api/knowledge.py:137; src/mindroom/approval_manager.py:170
_safe_traceback	function	lines 341-346	none-found	traceback format_exception sanitize max traceback	none
build_tool_failure_record	function	lines 349-382	related-only	build failure record timestamp context error duration arguments	src/mindroom/llm_request_logging.py:183; src/mindroom/hooks/execution.py:211; src/mindroom/tool_system/tool_hooks.py:667
build_tool_success_record	function	lines 385-416	related-only	build success record timestamp context result duration arguments	src/mindroom/llm_request_logging.py:279; src/mindroom/hooks/execution.py:221; src/mindroom/tool_system/tool_hooks.py:256
_tool_call_log_path	function	lines 419-420	related-only	log path jsonl tracking dir daily log path	src/mindroom/llm_request_logging.py:74; src/mindroom/constants.py:460
_tool_call_logger	function	lines 423-442	related-only	logger cache RotatingFileHandler JSONL mkdir handlers clear	src/mindroom/llm_request_logging.py:104
_append_tool_call_record	function	lines 445-448	related-only	append JSONL record json dumps sort_keys allow_nan	src/mindroom/llm_request_logging.py:104; src/mindroom/matrix/sync_tokens.py:69
record_tool_failure	function	lines 451-497	related-only	persist failure record runtime_paths none debug exception	src/mindroom/tool_system/tool_hooks.py:667; src/mindroom/llm_request_logging.py:310
record_tool_success	function	lines 500-546	related-only	persist success record runtime_paths none debug exception	src/mindroom/tool_system/tool_hooks.py:256; src/mindroom/llm_request_logging.py:279
_reset_tool_call_loggers_for_tests	function	lines 549-554	none-found	reset cached loggers close handlers tests	none
```

Findings:

1. Credential redaction is duplicated across tool-call failure logging and knowledge Git error handling.
`src/mindroom/tool_system/tool_calls.py:261` redacts HTTP(S) URL userinfo and secret query parameters, and `src/mindroom/tool_system/tool_calls.py:293` also redacts bearer tokens, API-key messages, token-like values, and secret assignments.
`src/mindroom/knowledge/redaction.py:22` redacts URL userinfo for any parsed URL scheme and strips path params, query, and fragment, while `src/mindroom/knowledge/redaction.py:44` redacts embedded URLs and `Authorization: Basic/Bearer ...` headers.
The shared behavior is credential removal from free-form failure text before logging or API display.
Differences to preserve: tool-call logging keeps non-secret query parameters and supports token-like provider key patterns; knowledge redaction intentionally strips all URL query/fragment data for repository identity and Git command safety, supports Basic auth decoding, and handles non-HTTP schemes.

2. Recursive JSON-safe conversion is related but not a direct duplicate.
`src/mindroom/tool_system/tool_calls.py:303` recursively converts arbitrary values to JSON-compatible data while redacting secret keys, bounding depth, bounding collection size, replacing non-finite floats with `None`, and sanitizing strings.
`src/mindroom/llm_request_logging.py:111` recursively converts Pydantic models, dataclasses, bytes, paths, mappings, and sequences to JSON-compatible payloads for request logs, but does not redact secrets or bound depth/size.
`src/mindroom/approval_manager.py:153` already reuses `sanitize_failure_value` for approval previews, so the approval path is not duplicate.
The overlap is generic JSON-safe normalization, but the tool-call sanitizer has security and size constraints that make a shared helper risky unless carefully parameterized.

3. JSONL persistence is similar but not a strong refactor target.
`src/mindroom/tool_system/tool_calls.py:423` creates cached rotating loggers and `src/mindroom/tool_system/tool_calls.py:445` writes sorted JSON through logging.
`src/mindroom/llm_request_logging.py:104` appends JSONL directly to a daily file.
Both persist JSONL records after creating parent directories, but rotation, async offloading, file naming, and JSON options differ enough that a shared writer would add policy parameters without clear payoff.

Proposed generalization:

1. Add a narrow credential-redaction helper module only if both current policies need to evolve together, for example `src/mindroom/redaction.py`.
2. Put shared primitives there, not one broad sanitizer: URL parsing/userinfo redaction, authorization-header token replacement, token-like replacement, and secret-key name normalization.
3. Keep two public policy functions with explicit names, such as `sanitize_tool_failure_text` and `redact_git_error_text`, so query stripping and Basic auth handling remain intentional.
4. Leave recursive JSON-safe conversion and JSONL persistence separate for now.

Risk/tests:

Credential redaction changes are security-sensitive.
Tests would need fixtures for HTTP(S) userinfo, SSH Git URLs, query signatures, bearer tokens, Basic auth headers with decoded secrets, secret assignments, provider-style API keys, and non-secret query parameters.
The main behavior risk is accidentally preserving Git URL query secrets or over-stripping useful tool-call failure context.
