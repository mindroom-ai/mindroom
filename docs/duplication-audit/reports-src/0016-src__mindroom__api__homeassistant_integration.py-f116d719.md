Summary: The main duplication candidate is Home Assistant REST/client behavior split between the dashboard API in `src/mindroom/api/homeassistant_integration.py` and agent tools in `src/mindroom/custom_tools/homeassistant.py`.
Both modules load the same Home Assistant credential shape, call the same REST endpoints with bearer auth, filter/simplify entity state data, and construct service-call payloads.
OAuth route structure is related to `src/mindroom/api/oauth.py` and `src/mindroom/api/integrations.py`, but Home Assistant's instance URL and client ID flow keep most of that service-specific rather than directly duplicate.

```coverage-tsv
qualname	kind	lines	status	search_terms	candidates_checked
HomeAssistantStatus	class	lines 40-49	related-only	HomeAssistantStatus SpotifyStatus OAuthStatusResponse status connected has_credentials entities_count	src/mindroom/api/integrations.py:80; src/mindroom/api/oauth.py:60
HomeAssistantAuthUrl	class	lines 52-55	related-only	auth_url OAuthConnectResponse connect response BaseModel	src/mindroom/api/oauth.py:52; src/mindroom/api/integrations.py:147
HomeAssistantConfig	class	lines 58-63	related-only	instance_url client_id long_lived_token config_fields homeassistant	src/mindroom/tools/__init__.py:280; src/mindroom/api/tools.py:77
_normalize_instance_url	function	lines 66-71	none-found	normalize instance_url rstrip startswith http urljoin homeassistant	none
_get_stored_config	function	lines 74-76	duplicate-found	load_credentials_for_target homeassistant load_scoped_credentials _load_config	src/mindroom/custom_tools/homeassistant.py:67; src/mindroom/api/tools.py:77
_save_config	function	lines 79-86	related-only	save_credentials _source ui homeassistant normalize instance_url	src/mindroom/api/integrations.py:95; src/mindroom/api/credentials.py:1006
_test_connection	async_function	lines 89-144	duplicate-found	httpx AsyncClient Home Assistant /api/config /api/states Authorization Bearer timeout	src/mindroom/custom_tools/homeassistant.py:76; src/mindroom/custom_tools/homeassistant.py:94
get_status	async_function	lines 148-194	duplicate-found	has url token homeassistant configured access_token long_lived_token status	src/mindroom/api/tools.py:77; src/mindroom/custom_tools/homeassistant.py:83
connect_oauth	async_function	lines 198-242	related-only	issue_pending_oauth_state auth authorize redirect_uri client_id callback	src/mindroom/api/oauth.py:102; src/mindroom/api/integrations.py:147
connect_token	async_function	lines 246-288	related-only	long_lived_token test_connection save credentials connect token	src/mindroom/api/credentials.py:1006; src/mindroom/api/tools.py:77
callback	async_function	lines 292-357	related-only	OAuth callback code state consume_pending_oauth_request exchange token save credentials	src/mindroom/api/oauth.py:368; src/mindroom/api/integrations.py:174
disconnect	async_function	lines 361-371	related-only	delete_credentials disconnected provider disconnect src/mindroom/api/integrations.py:236; src/mindroom/api/oauth.py:485
get_entities	async_function	lines 375-425	duplicate-found	/api/states filter by domain simplify entity_id state attributes last_changed list_entities	src/mindroom/custom_tools/homeassistant.py:141; src/mindroom/custom_tools/homeassistant.py:151
call_service	async_function	lines 429-473	duplicate-found	/api/services domain service entity_id service_data call_service post bearer	src/mindroom/custom_tools/homeassistant.py:334; src/mindroom/custom_tools/homeassistant.py:359
```

Findings:

1. Home Assistant REST request wrapper is duplicated across API and tool runtime.
`src/mindroom/api/homeassistant_integration.py:89` opens an `httpx.AsyncClient`, joins a Home Assistant endpoint, sends `Authorization: Bearer ...`, uses a 10 second timeout, maps non-success status codes, and converts `httpx` failures into user-facing errors.
`src/mindroom/custom_tools/homeassistant.py:76` performs the same core operation for agent tools, including credential lookup, bearer headers, URL joining, timeout, 401 handling, non-200/201 handling, JSON response parsing, and timeout/request-error mapping.
Differences to preserve: the API raises `HTTPException` and returns typed dashboard payloads, while the tool returns JSON-serializable dictionaries with `"error"` fields.

2. Home Assistant credential resolution is duplicated in shape and validation.
`src/mindroom/api/homeassistant_integration.py:74`, `src/mindroom/api/homeassistant_integration.py:161`, `src/mindroom/api/homeassistant_integration.py:386`, and `src/mindroom/api/homeassistant_integration.py:443` all expect `instance_url` plus either `access_token` or `long_lived_token`.
`src/mindroom/custom_tools/homeassistant.py:67` and `src/mindroom/custom_tools/homeassistant.py:87` load the same service credentials and select the same token fields.
`src/mindroom/api/tools.py:77` repeats the same availability predicate for dashboard tool status.
Differences to preserve: the dashboard API resolves a `RequestCredentialsTarget`, while the tool uses scoped worker-aware credential loading and only allows shared Home Assistant credentials.

3. Entity listing and simplification are duplicated.
`src/mindroom/api/homeassistant_integration.py:393` fetches `/api/states`, optionally filters entities by `entity_id.startswith(f"{domain}.")`, and returns selected fields.
`src/mindroom/custom_tools/homeassistant.py:141` fetches the same endpoint, applies the same domain prefix filter, and simplifies each entity.
Differences to preserve: the API includes `attributes` and `last_changed` and returns every entity, while the tool limits to 50 and exposes `friendly_name`.

4. Generic service call payload construction is duplicated.
`src/mindroom/api/homeassistant_integration.py:450` starts with request `data or {}`, adds `entity_id` when present, posts to `/api/services/{domain}/{service}`, accepts 200/201, and returns a success message.
`src/mindroom/custom_tools/homeassistant.py:334` builds the same service path and entity payload, then merges additional JSON string data before calling the same underlying API.
Differences to preserve: the tool accepts extra data as a JSON string and returns the raw Home Assistant result as JSON text, while the dashboard endpoint accepts a dictionary and returns a simple success response.

5. OAuth/status/disconnect route patterns are related but not strong duplication.
`src/mindroom/api/homeassistant_integration.py:198`, `src/mindroom/api/homeassistant_integration.py:292`, and `src/mindroom/api/homeassistant_integration.py:361` resemble the generic OAuth and Spotify integration routes in `src/mindroom/api/oauth.py:102`, `src/mindroom/api/oauth.py:368`, `src/mindroom/api/oauth.py:485`, and `src/mindroom/api/integrations.py:147`.
The common route lifecycle is visible: issue state, consume state on callback, verify the browser user, resolve a credential target, save or delete credentials, and redirect after success.
This is related rather than a refactor candidate in this audit because Home Assistant's OAuth endpoint is instance-local and stores `instance_url` plus `client_id`, while the generic provider route depends on provider registry objects and PKCE-capable provider methods.

Proposed generalization:

1. Add a focused shared Home Assistant client module such as `src/mindroom/homeassistant/client.py`.
2. Move pure helpers there for `normalize_instance_url`, extracting `instance_url` and token from a credential dict, building bearer headers, requesting Home Assistant endpoints, filtering entities by domain, simplifying entity payloads, and building service data.
3. Keep transport-specific error adaptation at the edges: API wrappers translate client exceptions/results to `HTTPException`; tool wrappers translate them to `{"error": ...}` dictionaries and JSON strings.
4. Update the dashboard API, agent tool, and tool availability check to use the same credential-shape helper.
5. Cover with focused tests for URL normalization, credential extraction, entity filtering/simplification, service data construction, 401/non-success mapping, timeout/request-error mapping, and preserving the tool's 50 entity limit.

Risk/tests:

The main behavior risk is accidentally changing public error shapes: FastAPI endpoints currently raise `HTTPException`, while agent tools return JSON strings containing error dictionaries.
The entity-list refactor must preserve the dashboard's full entity list and richer fields separately from the tool's capped, `friendly_name`-oriented output.
The service-call refactor must avoid mutating caller-owned `data` dictionaries when adding `entity_id`; current dashboard code mutates `data` when a dictionary is provided.
Tests should mock `httpx.AsyncClient` responses for `/api/`, `/api/config`, `/api/states`, and `/api/services/{domain}/{service}` and assert both API-level and tool-level behavior remain distinct.
