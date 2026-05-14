# SSRF URL Validation Report

PR URL: https://github.com/mindroom-ai/mindroom/pull/1041

Summary of changed files:
- `src/mindroom/server_fetch_url.py`: added reusable server-side HTTP(S) URL validation with direct IP, hostname, DNS result, metadata endpoint, and redirect target checks.
- `src/mindroom/custom_tools/website.py`: validates agent-callable website fetch URLs and manually validates redirect hops before following them.
- `src/mindroom/homeassistant_url_validation.py`, `src/mindroom/api/homeassistant_integration.py`, and `src/mindroom/custom_tools/homeassistant.py`: validate Home Assistant URLs before outbound requests, require explicit private/local URL opt-in, reuse shared user-facing error messages, log only non-sensitive rejection reasons, and avoid surfacing upstream response bodies on failures.
- `src/mindroom/tools/__init__.py`, `docs/tools/location-commerce-and-home.md`, and generated skill references: documented the Home Assistant private/local URL opt-in.
- `frontend/src/components/HomeAssistantIntegration/HomeAssistantIntegration.tsx` and its test: added the private/local URL opt-in control to the integration flow.
- `tests/test_server_fetch_url_validation.py`, `tests/test_website_tool.py`, `tests/test_homeassistant_tools.py`, and `tests/api/test_api.py`: added focused coverage for private URLs, localhost, metadata-style targets, public URLs, private opt-in behavior, validated URL reuse, redirect-to-private behavior, allowed redirect chains, and redirect limits.
- `tests/test_hatch_build.py`, `src/mindroom/cli/service.py`, `src/mindroom/services/launchd.py`, and `tests/test_services.py`: pre-commit-driven mechanical cleanup.

Tests run:
- `uv sync --all-extras`
- `uv run pytest tests/test_server_fetch_url_validation.py tests/test_website_tool.py tests/test_homeassistant_tools.py tests/api/test_api.py::test_homeassistant_connect_oauth_uses_pending_oauth_state tests/api/test_api.py::test_homeassistant_oauth_callback_uses_pending_payload_not_live_credentials tests/api/test_api.py::test_homeassistant_token_connect_rejects_private_url_without_opt_in tests/api/test_api.py::test_homeassistant_token_connect_allows_private_url_with_opt_in tests/api/test_api.py::test_homeassistant_connection_failure_does_not_return_response_body tests/test_hatch_build.py::test_build_frontend_rejects_git_lfs_pointer_assets -q` - 61 passed.
- `bun run test:unit src/components/HomeAssistantIntegration/HomeAssistantIntegration.test.tsx` - 2 passed.
- `bunx eslint --quiet src/components/HomeAssistantIntegration/HomeAssistantIntegration.tsx src/components/HomeAssistantIntegration/HomeAssistantIntegration.test.tsx` - passed.
- `uv run ruff check src/mindroom/server_fetch_url.py src/mindroom/custom_tools/website.py src/mindroom/api/homeassistant_integration.py src/mindroom/custom_tools/homeassistant.py tests/test_server_fetch_url_validation.py tests/test_website_tool.py tests/test_homeassistant_tools.py tests/api/test_api.py` - passed.
- `uv run pre-commit run --all-files` - passed.

Residual risk or review notes:
- Public hostname validation resolves DNS before the HTTP client opens its own connection, which blocks direct internal DNS answers in normal operation but does not pin the resolved IP across the later socket connection.
- Home Assistant private/local access is intentionally opt-in through `allow_private_url`; metadata endpoints remain blocked even when private/local access is enabled.
- The shared URL validator remains synchronous because it is used by the synchronous website reader; async DNS offloading can be added later as a separate API if event-loop latency becomes a measured problem.
