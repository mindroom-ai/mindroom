## Summary

Top duplication candidate: `openweather_tools` repeats the same registered-toolkit lazy import pattern used by many other `src/mindroom/tools/*` modules.
This is intentional wrapper boilerplate rather than a strong refactor target because the metadata differs per tool and the function body is only a three-line import/return shim.

## Coverage TSV

```coverage-tsv
qualname	kind	lines	status	search_terms	candidates_checked
openweather_tools	function	lines 77-81	related-only	openweather_tools; from agno.tools.openweather import OpenWeatherTools; return OpenWeatherTools; def .*_tools() -> type	src/mindroom/tools/google_maps.py:100; src/mindroom/tools/financial_datasets_api.py:50; src/mindroom/tools/giphy.py:56; src/mindroom/tools/__init__.py:95
```

## Findings

No real duplication that warrants consolidation was found for `openweather_tools`.

Related pattern: `src/mindroom/tools/openweather.py:77` lazily imports `OpenWeatherTools` and returns the toolkit class, which matches the wrapper style in `src/mindroom/tools/google_maps.py:100`, `src/mindroom/tools/financial_datasets_api.py:50`, and `src/mindroom/tools/giphy.py:56`.
The shared behavior is "registered tool factory returns an Agno toolkit class while avoiding runtime import at module import time."
The differences to preserve are the concrete imported class, return annotation, docstring, and per-module decorator metadata.

`src/mindroom/tools/__init__.py:95` imports and exports `openweather_tools`, matching the broad registry pattern for these per-tool wrappers.
That is related registration boilerplate, not duplicated weather-specific behavior.

Searches for weather/OpenWeather terms under `src` found no alternate OpenWeather API wrappers, weather request builders, forecast parsing, geocoding helpers, or duplicated current-weather behavior outside this module.

## Proposed Generalization

No refactor recommended.
The duplicated body shape is tiny, explicit, and consistent with the existing tool registration modules.
A generic factory helper would save only two executable lines per wrapper while making type annotations, import timing, and metadata discovery less direct.

## Risk/Tests

No production code was changed.
If this pattern were ever generalized, tests should cover tool metadata generation, import-time behavior for optional dependencies, and toolkit loading through `src/mindroom/tools/__init__.py`.
For this audit, no tests were required beyond source inspection because the deliverable is report-only.
