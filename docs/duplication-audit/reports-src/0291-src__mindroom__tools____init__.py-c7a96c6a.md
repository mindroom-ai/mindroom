Summary: No meaningful duplication found.
The two required symbols are registry factories in `src/mindroom/tools/__init__.py`.
`_openclaw_compat_tools` is related to preset expansion and dashboard display logic, but does not duplicate the actual expansion behavior.
`_homeassistant_tools` follows the standard one-line toolkit factory pattern used throughout `src/mindroom/tools`, but this is intentional registry boilerplate rather than duplicated domain behavior worth extracting.

```coverage-tsv
qualname	kind	lines	status	search_terms	candidates_checked
_openclaw_compat_tools	function	lines 272-276	related-only	openclaw_compat, OpenClaw, TOOL_PRESETS, config-only presets, Toolkit return	src/mindroom/config/main.py:358; src/mindroom/api/tools.py:121; src/mindroom/tools/browser.py:20; src/mindroom/tool_system/skills.py:516
_homeassistant_tools	function	lines 326-330	related-only	homeassistant_tools, HomeAssistantTools, homeassistant metadata, return toolkit class	src/mindroom/custom_tools/homeassistant.py:22; src/mindroom/api/tools.py:77; src/mindroom/api/homeassistant_integration.py:148; src/mindroom/tools/browser.py:46
```

Findings:

No real duplication found for the required symbols.

`_openclaw_compat_tools` returns the base `agno.tools.Toolkit` solely so the metadata registry has a factory for the `openclaw_compat` tool name.
The actual preset expansion lives in `Config.TOOL_PRESETS` at `src/mindroom/config/main.py:358`, and dashboard visibility for config-only presets is handled in `src/mindroom/api/tools.py:121`.
Those paths are related to the same public preset, but they do different work: config expands the preset into concrete tools, the API appends display-only preset metadata, and `_openclaw_compat_tools` is a registry placeholder.

`_homeassistant_tools` returns `HomeAssistantTools` from `src/mindroom/custom_tools/homeassistant.py:22`.
Many files under `src/mindroom/tools` use the same factory shape, for example `src/mindroom/tools/browser.py:46`, but the repeated behavior is the registry convention itself: lazy-import a toolkit class and return it.
The Home Assistant-specific runtime behavior is in `HomeAssistantTools`, and the dashboard configuration check in `src/mindroom/api/tools.py:77` only validates stored credentials for UI availability.

Proposed generalization:

No refactor recommended.
Extracting these one-line factories would add indirection without reducing meaningful duplicated behavior.
The only possible future cleanup would be a declarative metadata-only preset registration helper if more config-only presets like `openclaw_compat` are added.

Risk/tests:

No production changes were made.
If a future refactor touches these paths, tests should cover `Config.expand_tool_names`/preset expansion for `openclaw_compat`, registered metadata visibility in the tools API, and `get_tool_by_name("homeassistant")` constructing `HomeAssistantTools` with managed credentials and worker-target arguments.
