# ISSUE-239 and ISSUE-240 Implementation Report

## Root cause confirmation

- ISSUE-239 was caused by toolkit constructor arguments being assembled only from each tool's declared `ConfigField` entries.
- Agno's universal `Toolkit.include_tools` and `Toolkit.exclude_tools` arguments were absent from that field set, so the values never reached toolkit construction.
- The existing runtime config validator already rejected fields outside its recognized field set, but there was no config-load regression test proving that arbitrary override keys fail with the tool and key in the error.
- ISSUE-240 was caused by request logging reading the `tools` argument at the outer `Model.ainvoke` or `Model.ainvoke_stream` boundary before provider-specific request preparation ran.
- Claude and OpenAI Responses apply native deferred-tool transformations later, so the logged catalog did not match the final provider request array.
- The logger now captures the final tools from the provider request-preparation seams and writes the request record only after that capture is available.
- Homegrown deferred tools remain absent until `load_tool`, while native Claude deferred definitions remain in the HTTP tools array with `defer_loading: true` and the server-side search tool because that is the exact native wire representation.

## Files changed

- `src/mindroom/tool_system/metadata.py` adds universal toolkit filter metadata, validation, normalization, and constructor forwarding.
- `tests/test_tools_metadata.py` verifies real SearXNG function filtering and strict config-load rejection of an unknown override.
- `src/mindroom/llm_request_logging.py` adds request-local final-tools capture and delays request persistence until provider preparation has run.
- `src/mindroom/claude_prompt_cache.py` records the final Claude tools array after deferred-search and cache-marker transformations.
- `src/mindroom/openai_tool_search.py` records the final OpenAI Responses tools array after deferred-search transformation.
- `tests/test_llm_request_logging.py` verifies deferred tools are excluded before load and included after load, and separately verifies Claude's native wire array.
- `.claude/REPORT-issue-239-240.md` records this implementation and its verification evidence.

## Review remediation

- Optional request and response log failures are isolated at the observability boundary, so they cannot replace model responses, mask provider errors, terminate streams, or suppress later usage telemetry.
- Universal Toolkit filters are applied after construction, which supports Toolkit subclasses that do not expose Agno's base filter kwargs.
- Toolkit-filter capability is explicit in runtime validation snapshots.
- Metadata-only helpers and the Composio non-Toolkit integration reject universal filter fields during config validation.
- OpenAI native wire-tool capture, log-write failures, provider-error preservation, stream resilience, custom Toolkit constructors, `exclude_tools`, and validation-snapshot transport have focused regressions.
- Configuration docs now describe Toolkit function filters and final provider-prepared request logging.
- Bundled `mindroom-docs` references were regenerated from the updated docs.

## Test evidence

- `uv sync --all-extras` completed successfully before validation.
- Focused logging and tool-metadata regressions passed with 57 tests.
- Provider, dynamic-tool, worker-snapshot, sandbox-proxy, and import-graph suites passed.
- `uv run ruff check`, `uv run ruff format --check`, and `uv run ty check` passed on every changed Python file.
- `uv run tach check --dependencies --interfaces` passed without a boundary update.
- `env -u MINDROOM_OWNER_USER_ID -u MINDROOM_DOCKER_WORKER_IMAGE uv run pytest -n 0 --no-cov -q` passed repository-wide.
- `uv run pre-commit run --all-files` passed every hook.

## Deviations

- The requested exclude-until-load assertion covers the homegrown deferred-tool flow.
- Separate regressions cover native Claude and OpenAI behavior, where exact wire logs correctly include deferred definitions marked with `defer_loading`.
- A live Matrix test was not run because the changed invariants are local constructor filtering and injected filesystem-write failures, both exercised directly at their owning seams.
- No functional behavior outside the two requested fixes was changed.
