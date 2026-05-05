Summary: No meaningful duplication found.
`src/mindroom/vendor_telemetry.py` is already the central implementation for vendor telemetry opt-out environment values and best-effort loaded-module patching.
The closest related behavior is repeated `telemetry=False` on Agno `Agent`/`Team` constructors, but that controls Agno runtime telemetry per object and does not duplicate the broader vendor environment/module guards in this file.

```coverage-tsv
qualname	kind	lines	status	search_terms	candidates_checked
disable_vendor_telemetry	function	lines 16-21	related-only	disable_vendor_telemetry; telemetry disable entry points; vendor opt-out env update	src/mindroom/__init__.py:6; src/mindroom/__init__.py:8; src/mindroom/cli/__init__.py:3; src/mindroom/cli/__init__.py:5; src/mindroom/tools/composio.py:8; src/mindroom/tools/composio.py:199; src/mindroom/constants.py:45
vendor_telemetry_env_values	function	lines 24-26	related-only	vendor_telemetry_env_values; subprocess env telemetry values; VENDOR_TELEMETRY_ENV_VALUES	src/mindroom/constants.py:45; src/mindroom/tools/shell.py:169; src/mindroom/tool_system/dependencies.py:148; src/mindroom/tool_system/dependencies.py:179; src/mindroom/tool_system/dependencies.py:197; src/mindroom/api/sandbox_exec.py:241; src/mindroom/api/sandbox_exec.py:296; src/mindroom/api/sandbox_runner.py:884
_disable_loaded_vendor_modules	function	lines 29-53	none-found	posthog default_client disabled send; mem0.memory.telemetry MEM0_TELEMETRY; litellm telemetry False; huggingface_hub.constants HF_HUB_DISABLE_TELEMETRY; composio.utils.sentry update_dsn atexit.unregister	src/mindroom/vendor_telemetry.py:31; src/mindroom/vendor_telemetry.py:40; src/mindroom/vendor_telemetry.py:44; src/mindroom/vendor_telemetry.py:47; src/mindroom/vendor_telemetry.py:50; src/mindroom/tool_system/plugin_imports.py:381; src/mindroom/tool_system/plugin_imports.py:392; src/mindroom/tool_system/registry_state.py:185; src/mindroom/tool_system/metadata.py:879; src/mindroom/tool_system/plugins.py:293
```

Findings:

No real duplicated behavior was found for this primary file.

`disable_vendor_telemetry` has multiple call sites in package import, CLI import, and Composio tool loading, but those are invocations of the shared helper rather than duplicate implementations.
The centralized environment payload is `VENDOR_TELEMETRY_ENV_VALUES` in `src/mindroom/constants.py:45`, and no other source file under `src` reconstructs the same mapping.

`vendor_telemetry_env_values` is reused by subprocess environment builders in `src/mindroom/tools/shell.py:169`, `src/mindroom/tool_system/dependencies.py:148`, `src/mindroom/tool_system/dependencies.py:179`, `src/mindroom/tool_system/dependencies.py:197`, `src/mindroom/api/sandbox_exec.py:241`, and `src/mindroom/api/sandbox_exec.py:296`.
These call sites repeat the generic pattern of copying or updating subprocess environments, but they correctly depend on the shared telemetry value helper.
`src/mindroom/api/sandbox_runner.py:884` indirectly picks up the same values through `sandbox_exec.generic_subprocess_env()`, which is related env construction behavior but not a duplicate telemetry source.

`_disable_loaded_vendor_modules` is the only place found that mutates already-imported vendor modules for PostHog, Mem0, LiteLLM, Hugging Face Hub, and Composio Sentry.
Other `sys.modules` matches in `src/mindroom/tool_system/plugin_imports.py`, `src/mindroom/tool_system/registry_state.py`, `src/mindroom/tool_system/metadata.py`, and `src/mindroom/tool_system/plugins.py` are plugin import/cache isolation logic, not telemetry mutation.
The repeated `telemetry=False` matches in agent construction sites such as `src/mindroom/agents.py:1260`, `src/mindroom/teams.py:621`, `src/mindroom/teams.py:1367`, `src/mindroom/routing.py:98`, `src/mindroom/topic_generator.py:96`, `src/mindroom/thread_summary.py:345`, and `src/mindroom/scheduling.py:695` are related only.
They disable Agno telemetry for specific constructed objects and do not cover subprocess env propagation or already-loaded vendor modules.

Proposed generalization:

No refactor recommended.
The telemetry behavior is already centralized in `src/mindroom/vendor_telemetry.py` and `src/mindroom/constants.py`.
The `telemetry=False` constructor repetition may be worth a separate Agno factory audit, but it is outside this file's duplicated behavior because it is per-object construction policy rather than vendor environment/module cleanup.

Risk/tests:

No code changes were made.
If this area is refactored later, tests should preserve all three behavior surfaces: updating a supplied mapping without touching loaded modules, returning a mutable copy of the constant mapping, and patching/unregistering loaded vendor modules when operating on `os.environ`.
