## Summary

The main duplication candidate is the Home Assistant REST client behavior split between `src/mindroom/custom_tools/homeassistant.py` and `src/mindroom/api/homeassistant_integration.py`.
Both paths independently resolve Home Assistant credentials, build Bearer-authenticated `httpx` requests with `urljoin`, handle status/error cases, list and filter entities, and call Home Assistant services.

The tool methods for `turn_on`, `turn_off`, `toggle`, scene activation, automation trigger, light brightness/color, and climate temperature are thin wrappers around service calls.
The dashboard API already exposes the same generic service-call behavior, so the duplicated behavior is active even though the outward response shapes differ.

## Coverage TSV

```coverage-tsv
qualname	kind	lines	status	search_terms	candidates_checked
HomeAssistantTools	class	lines 22-364	duplicate-found	HomeAssistantTools homeassistant integration REST service entities credentials	src/mindroom/api/homeassistant_integration.py:40, src/mindroom/api/homeassistant_integration.py:89, src/mindroom/api/homeassistant_integration.py:374, src/mindroom/api/homeassistant_integration.py:428, src/mindroom/tools/__init__.py:279
HomeAssistantTools.__init__	method	lines 25-65	related-only	Toolkit homeassistant managed_init_args worker_target shared integrations	src/mindroom/tools/__init__.py:279, src/mindroom/tool_system/worker_routing.py:29, src/mindroom/tool_system/metadata.py:493
HomeAssistantTools._load_config	method	lines 67-74	duplicate-found	load_scoped_credentials homeassistant load_credentials_for_target shared credentials	src/mindroom/api/homeassistant_integration.py:74, src/mindroom/api/homeassistant_integration.py:150, src/mindroom/api/homeassistant_integration.py:381, src/mindroom/api/homeassistant_integration.py:438, src/mindroom/api/tools.py:77
HomeAssistantTools._api_request	async_method	lines 76-115	duplicate-found	httpx AsyncClient urljoin Authorization Bearer status_code Home Assistant	src/mindroom/api/homeassistant_integration.py:89, src/mindroom/api/homeassistant_integration.py:393, src/mindroom/api/homeassistant_integration.py:455
HomeAssistantTools.get_entity_state	async_method	lines 117-139	related-only	/api/states entity_id state attributes last_changed	src/mindroom/api/homeassistant_integration.py:119, src/mindroom/api/homeassistant_integration.py:374
HomeAssistantTools.list_entities	async_method	lines 141-173	duplicate-found	/api/states domain filter entity_id startswith friendly_name entities	src/mindroom/api/homeassistant_integration.py:374, src/mindroom/api/homeassistant_integration.py:409
HomeAssistantTools.turn_on	async_method	lines 175-191	duplicate-found	/api/services domain turn_on entity_id call_service	src/mindroom/api/homeassistant_integration.py:428, src/mindroom/api/homeassistant_integration.py:450
HomeAssistantTools.turn_off	async_method	lines 193-209	duplicate-found	/api/services domain turn_off entity_id call_service	src/mindroom/api/homeassistant_integration.py:428, src/mindroom/api/homeassistant_integration.py:450
HomeAssistantTools.toggle	async_method	lines 211-227	duplicate-found	/api/services domain toggle entity_id call_service	src/mindroom/api/homeassistant_integration.py:428, src/mindroom/api/homeassistant_integration.py:450
HomeAssistantTools.set_brightness	async_method	lines 229-251	duplicate-found	light turn_on brightness 0 255 service_data	src/mindroom/api/homeassistant_integration.py:428, src/mindroom/api/homeassistant_integration.py:450
HomeAssistantTools.set_color	async_method	lines 253-277	duplicate-found	light turn_on rgb_color 0 255 service_data	src/mindroom/api/homeassistant_integration.py:428, src/mindroom/api/homeassistant_integration.py:450
HomeAssistantTools.set_temperature	async_method	lines 279-298	duplicate-found	climate set_temperature entity_id temperature service_data	src/mindroom/api/homeassistant_integration.py:428, src/mindroom/api/homeassistant_integration.py:450
HomeAssistantTools.activate_scene	async_method	lines 300-315	duplicate-found	scene turn_on entity_id service_data	src/mindroom/api/homeassistant_integration.py:428, src/mindroom/api/homeassistant_integration.py:450
HomeAssistantTools.trigger_automation	async_method	lines 317-332	duplicate-found	automation trigger entity_id service_data	src/mindroom/api/homeassistant_integration.py:428, src/mindroom/api/homeassistant_integration.py:450
HomeAssistantTools.call_service	async_method	lines 334-364	duplicate-found	call_service domain service entity_id data json.loads /api/services	src/mindroom/api/homeassistant_integration.py:428, src/mindroom/api/homeassistant_integration.py:450
```

## Findings

1. Home Assistant REST request handling is duplicated.

- Primary: `src/mindroom/custom_tools/homeassistant.py:76` builds an authenticated `httpx.AsyncClient` request to `urljoin(instance_url, endpoint)`, uses `Authorization: Bearer ...`, treats 401 specially, accepts 200/201, parses JSON or returns success, and maps `httpx` failures to user-facing errors.
- Candidate: `src/mindroom/api/homeassistant_integration.py:89` tests the same API using the same `httpx.AsyncClient`, `urljoin`, Bearer header, timeout, 401 handling, and request-error mapping.
- Candidate: `src/mindroom/api/homeassistant_integration.py:393` and `src/mindroom/api/homeassistant_integration.py:455` repeat the same authenticated request shape for entity listing and service calls.
- Why duplicated: these are not just similar HTTP calls; they encode the same Home Assistant credential fields, endpoint construction, authentication header, timeout, status handling, and connection-error translation.
- Differences to preserve: the custom tool returns JSON-serializable dictionaries or JSON strings for agents, while the FastAPI routes raise `HTTPException` and sometimes return Pydantic models or simpler success messages.

2. Home Assistant credential resolution and configured-state checks are duplicated.

- Primary: `src/mindroom/custom_tools/homeassistant.py:67` loads `homeassistant` scoped credentials and later `src/mindroom/custom_tools/homeassistant.py:87` checks for `instance_url` plus `access_token` or `long_lived_token`.
- Candidate: `src/mindroom/api/homeassistant_integration.py:74` loads the same service credentials, and `src/mindroom/api/homeassistant_integration.py:161`, `src/mindroom/api/homeassistant_integration.py:386`, and `src/mindroom/api/homeassistant_integration.py:443` repeat the same `instance_url` and token extraction.
- Candidate: `src/mindroom/api/tools.py:77` has a special Home Assistant availability check with the same required credential fields.
- Why duplicated: the same provider-specific credential schema is recognized in three places.
- Differences to preserve: the tool must respect `load_scoped_credentials` and worker-target restrictions for shared-only integrations; dashboard routes use `resolve_request_credentials_target`/`load_credentials_for_target`.

3. Entity listing and domain filtering are duplicated.

- Primary: `src/mindroom/custom_tools/homeassistant.py:141` gets `/api/states`, optionally filters by `entity_id.startswith(f"{domain}.")`, and maps entities into a smaller result.
- Candidate: `src/mindroom/api/homeassistant_integration.py:374` gets `/api/states`, applies the same domain prefix filter at `src/mindroom/api/homeassistant_integration.py:409`, and maps entities to a response shape.
- Why duplicated: both implement the same Home Assistant entity-list workflow and domain filtering rule.
- Differences to preserve: the tool caps the response to 50 and includes `friendly_name`; the API returns all matching entities and includes `attributes`/`last_changed`.

4. Service-call construction is duplicated.

- Primary: `src/mindroom/custom_tools/homeassistant.py:175`, `src/mindroom/custom_tools/homeassistant.py:193`, and `src/mindroom/custom_tools/homeassistant.py:211` derive the domain from `entity_id`, construct `/api/services/{domain}/{service}`, and send `{"entity_id": entity_id}`.
- Primary: `src/mindroom/custom_tools/homeassistant.py:229`, `src/mindroom/custom_tools/homeassistant.py:253`, `src/mindroom/custom_tools/homeassistant.py:279`, `src/mindroom/custom_tools/homeassistant.py:300`, and `src/mindroom/custom_tools/homeassistant.py:317` are fixed service-call wrappers with provider-specific payload fields.
- Candidate: `src/mindroom/api/homeassistant_integration.py:428` exposes the same generic `/api/services/{domain}/{service}` behavior and builds `service_data` from `data` plus optional `entity_id` at `src/mindroom/api/homeassistant_integration.py:450`.
- Why duplicated: both sides know how to translate domain/service/entity data into the Home Assistant service-call endpoint.
- Differences to preserve: the agent tool accepts extra `data` as a JSON string and returns invalid JSON as a JSON-string error, while the API accepts `data` as an already parsed `dict[str, Any] | None`.

## Proposed Generalization

Add a small shared Home Assistant client module, for example `src/mindroom/homeassistant_client.py`, only if production edits are later requested.
It should stay provider-specific and expose minimal pure/request helpers:

1. `resolve_homeassistant_credentials(config: Mapping[str, Any]) -> HomeAssistantCredentials | None` to centralize `instance_url` plus `access_token`/`long_lived_token` extraction and URL normalization.
2. `async request_homeassistant(credentials, method, endpoint, json_data=None) -> HomeAssistantResponse` to centralize `httpx`, `urljoin`, Bearer headers, timeout, accepted status codes, and response parsing.
3. `async list_homeassistant_entities(credentials, domain=None) -> list[dict[str, Any]]` to centralize `/api/states` and domain-prefix filtering while letting callers shape/truncate the response.
4. `async call_homeassistant_service(credentials, domain, service, service_data) -> dict[str, Any]` to centralize `/api/services/{domain}/{service}`.

The FastAPI layer could adapt client errors into `HTTPException`; the custom tool layer could adapt them into JSON strings.
No broad abstraction across unrelated HTTP tools is recommended because the duplicated behavior is Home Assistant-specific and includes provider-specific credential semantics.

## Risk/tests

Primary risks are changing error text, status-code behavior, response truncation, and service-call success payloads that agents or dashboard UI may rely on.
Tests should cover missing credentials, invalid token/401, non-200/201 responses, timeout/request errors, entity domain filtering, the 50-entity cap in the tool, generic service-call payload merging, and JSON-string parsing errors in `HomeAssistantTools.call_service`.

No production code was edited for this audit.
