## Summary

Top duplication candidates are OAuth/JWT token response handling shared with `src/mindroom/oauth/providers.py`, file-lock plus atomic JSON persistence shared with several local state stores, and stream-delta aggregation that partially overlaps Agno's normal Responses invoke path but is Codex-specific because the endpoint requires streaming.

## Coverage TSV

```coverage-tsv
qualname	kind	lines	status	search_terms	candidates_checked
CodexAuthError	class	lines 43-44	related-only	CodexAuthError OAuthProviderError auth exceptions	src/mindroom/oauth/providers.py:159, src/mindroom/oauth/providers.py:461
normalize_codex_model_id	function	lines 47-52	related-only	model id prefix normalize provider normalize removeprefix	src/mindroom/model_loading.py:40, src/mindroom/tool_system/dependencies.py:53
borrow_codex_key	function	lines 55-94	duplicate-found	refresh token access token expiry persisted oauth credentials	src/mindroom/oauth/providers.py:473, src/mindroom/api/oauth.py:280, src/mindroom/oauth/service.py:200
_codex_auth_path	function	lines 97-103	related-only	auth path exists missing file validation	src/mindroom/credentials.py:142, src/mindroom/handled_turns.py:455
_codex_home_path	function	lines 106-107	related-only	env default path CODEX_HOME runtime path	src/mindroom/constants.py:388, src/mindroom/cli/config.py:993
_read_codex_auth	function	lines 110-116	duplicate-found	json load validate auth mode credentials file	src/mindroom/credentials.py:142, src/mindroom/handled_turns.py:455
_codex_auth_refresh_lock	function	lines 120-127	duplicate-found	fcntl lock contextmanager lock file	src/mindroom/handled_turns.py:440, src/mindroom/oauth/state.py:45, src/mindroom/interactive.py:260
_usable_access_token	function	lines 130-136	duplicate-found	token expiry skew access token refresh decision	src/mindroom/oauth/service.py:200, src/mindroom/oauth/state.py:148, src/mindroom/oauth/client.py:184
_write_codex_auth	function	lines 139-146	duplicate-found	atomic json write temp replace chmod safe_replace	src/mindroom/handled_turns.py:403, src/mindroom/matrix/state.py:200, src/mindroom/constants.py:1088
_jwt_exp	function	lines 149-157	duplicate-found	jwt base64 decode exp unverified	src/mindroom/oauth/providers.py:136
_refresh_codex_tokens	function	lines 160-185	duplicate-found	refresh_token oauth token endpoint preserve response	src/mindroom/oauth/providers.py:473, src/mindroom/oauth/google.py:90
_refresh_error_code	function	lines 188-194	related-only	parse json error code body response text	src/mindroom/cli/connect.py:174, src/mindroom/error_handling.py:13
_update_tokens	function	lines 197-200	duplicate-found	merge refreshed token fields preserve missing refresh token	src/mindroom/oauth/providers.py:498, src/mindroom/api/oauth.py:280
derive_codex_prompt_cache_key	function	lines 203-218	related-only	cache key identity thread session hash	src/mindroom/thread_summary.py:124, src/mindroom/voice_handler.py:61, src/mindroom/workers/runtime.py:37
_codex_prompt_cache_headers	function	lines 221-226	none-found	Codex prompt cache headers session_id window id	none
_codex_installation_id	function	lines 229-234	related-only	read optional local id file strip empty	src/mindroom/matrix/client_session.py:83, src/mindroom/credentials.py:142
_codex_prompt_cache_extra_body	function	lines 237-244	related-only	build client_metadata extra_body dict	src/mindroom/response_runner.py:109
_merge_codex_extra_body	function	lines 247-262	duplicate-found	merge nested client_metadata preserve caller values	src/mindroom/response_runner.py:109, src/mindroom/codex_model.py:418
CodexResponses	class	lines 266-411	related-only	OpenAIResponses subclass request params client override	src/mindroom/model_loading.py:106, src/mindroom/vertex_claude_prompt_cache.py:76
CodexResponses.__post_init__	method	lines 277-280	related-only	normalize model id post init	src/mindroom/model_loading.py:106
CodexResponses._get_client_params	method	lines 282-300	related-only	OpenAI client params headers token base_url	agno.models.openai.OpenAIResponses._get_client_params, src/mindroom/model_loading.py:98
CodexResponses._instructions_text	method	lines 302-304	related-only	system prompt instructions join default	src/mindroom/agent_prompts.py:1, agno.models.openai.OpenAIResponses.get_request_params
CodexResponses._prompt_cache_key	method	lines 306-307	not-a-behavior-symbol	trivial accessor prompt_cache_key	none
CodexResponses.get_request_params	method	lines 309-339	related-only	request params extra headers extra_body unsupported params	agno.models.openai.OpenAIResponses.get_request_params, src/mindroom/vertex_claude_prompt_cache.py:76
CodexResponses.invoke	method	lines 341-367	duplicate-found	invoke consumes stream aggregate deltas populate assistant	src/mindroom/codex_model.py:369, agno.models.openai.OpenAIResponses.invoke
CodexResponses.ainvoke	async_method	lines 369-395	duplicate-found	ainvoke consumes async stream aggregate deltas populate assistant	src/mindroom/codex_model.py:341, agno.models.openai.OpenAIResponses.ainvoke
CodexResponses.get_client	method	lines 397-403	duplicate-found	get_client OpenAI params http_client default no cache	agno.models.openai.OpenAIResponses.get_client
CodexResponses.get_async_client	method	lines 405-411	duplicate-found	get_async_client AsyncOpenAI params http_client default no cache	agno.models.openai.OpenAIResponses.get_async_client
_append_response_string	function	lines 414-415	related-only	append optional string delta reasoning content	none
_merge_dict_data	function	lines 418-429	duplicate-found	merge optional dict data extend list values	src/mindroom/response_runner.py:109, src/mindroom/codex_model.py:247
_extend_response_list	function	lines 432-438	duplicate-found	extend optional list initialize missing list	src/mindroom/codex_model.py:486
_merge_response_delta	function	lines 441-446	related-only	aggregate ModelResponse streaming delta	agno.models.openai.OpenAIResponses.invoke, tests/test_codex_model.py:316
_merge_response_content	function	lines 449-470	duplicate-found	merge streaming content and reasoning fields into response	agno.models.openai.OpenAIResponses.invoke_stream, tests/test_codex_model.py:316
_merge_response_media	function	lines 473-483	duplicate-found	merge response media list fields	src/mindroom/codex_model.py:432
_merge_response_tools	function	lines 486-492	duplicate-found	merge tool_calls and tool_executions lists	src/mindroom/codex_model.py:432
_merge_response_metadata	function	lines 495-506	duplicate-found	merge provider_data extra updated_session_state compression stats	src/mindroom/codex_model.py:418, src/mindroom/response_runner.py:109
_merge_response_metrics	function	lines 509-525	related-only	copy response usage token metrics	src/mindroom/ai.py:572, src/mindroom/ai.py:603
```

## Findings

1. Codex OAuth state handling repeats general OAuth token behavior.
`borrow_codex_key()` and helpers in `src/mindroom/codex_model.py:55`, `src/mindroom/codex_model.py:130`, `src/mindroom/codex_model.py:149`, `src/mindroom/codex_model.py:160`, and `src/mindroom/codex_model.py:197` duplicate pieces already present in `src/mindroom/oauth/providers.py:136`, `src/mindroom/oauth/providers.py:473`, `src/mindroom/oauth/providers.py:498`, and `src/mindroom/api/oauth.py:280`.
The shared behavior is unverified JWT payload decoding, deciding whether a token is expired or refreshable, refreshing with a refresh token, and preserving old token fields when the provider omits replacements.
Differences to preserve are Codex's fixed client id, sync `httpx.post`, `auth.json` shape with `tokens.access_token`, lock-protected refresh-token rotation, and Codex-specific invalid refresh error codes.

2. Codex local auth persistence repeats local JSON store locking and atomic-write mechanics.
`_codex_auth_refresh_lock()` and `_write_codex_auth()` in `src/mindroom/codex_model.py:120` and `src/mindroom/codex_model.py:139` overlap with lock/write patterns in `src/mindroom/handled_turns.py:403`, `src/mindroom/handled_turns.py:440`, `src/mindroom/oauth/state.py:45`, `src/mindroom/interactive.py:260`, and `src/mindroom/constants.py:1088`.
The shared behavior is creating a sibling lock file, acquiring `fcntl` locks, writing JSON through a temporary file, then replacing the target.
Differences to preserve are Codex's explicit `0o600` temp-file mode and final `chmod`, because this file contains OAuth credentials.

3. Codex request metadata merging repeats small dict/list merge helpers.
`_merge_codex_extra_body()` in `src/mindroom/codex_model.py:247` and `_merge_dict_data()` in `src/mindroom/codex_model.py:418` both implement shallow merge behavior around optional dicts and list extension.
They are also related to `_merge_response_extra_content()` in `src/mindroom/response_runner.py:109`, which conditionally creates a metadata dict and inserts additional keys.
Differences to preserve are important: Codex client metadata uses `setdefault()` so caller-provided metadata wins, while response-delta metadata overwrites scalar values and extends lists.

4. Sync and async Codex `invoke` paths duplicate each other and mirror the Agno non-streaming invoke shape.
`CodexResponses.invoke()` in `src/mindroom/codex_model.py:341` and `CodexResponses.ainvoke()` in `src/mindroom/codex_model.py:369` have the same non-streaming-from-streaming aggregation flow.
Agno's `OpenAIResponses.invoke` and `OpenAIResponses.ainvoke` perform the analogous non-streaming request and parse path, but Codex must consume `invoke_stream` because the Codex endpoint is stream-only.
Differences to preserve are sync versus async iteration and final `_populate_assistant_message()` after all deltas are merged.

5. Client construction intentionally duplicates Agno's client construction with one behavior change.
`CodexResponses.get_client()` and `CodexResponses.get_async_client()` in `src/mindroom/codex_model.py:397` and `src/mindroom/codex_model.py:405` are near copies of Agno's `OpenAIResponses.get_client` and `OpenAIResponses.get_async_client`.
The key difference is deliberate: Codex does not cache clients so each request can refresh the borrowed OAuth access token.

## Proposed Generalization

No production refactor is recommended from this audit alone.
If this area is touched again, the lowest-risk cleanup would be:

1. Add a tiny shared JWT helper, for example `src/mindroom/oauth/jwt.py`, returning unverified claims or a specific `exp` claim.
2. Add a credential-file persistence helper only if another secret-bearing file needs the exact `0o600` atomic write semantics.
3. Keep Codex refresh orchestration in `codex_model.py`, because its sync file format and error handling are provider-specific.
4. Extract a private `_consume_response_stream()` and `_consume_async_response_stream()` pair only if more stream-only model providers are added.
5. Do not generalize `_merge_codex_extra_body()` with `_merge_dict_data()` unless the helper can explicitly encode the caller-wins versus delta-wins merge policy.

## Risk/tests

JWT helper extraction would need tests covering invalid token shape, invalid base64/JSON, non-dict claims, integer `exp`, and missing `exp`.
Any persistence helper would need tests for file mode, lock behavior, temp cleanup, and replacement behavior on failure.
Any stream aggregation extraction would need `tests/test_codex_model.py::test_codex_responses_invoke_aggregates_streaming_deltas` plus async coverage for content, provider data, media lists, tool calls, and metrics.
The current duplication is mostly localized and provider-specific, so broad refactoring would carry more risk than benefit unless a second Codex-like provider appears.
