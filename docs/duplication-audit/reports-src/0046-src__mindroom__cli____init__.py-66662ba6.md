## Summary

Top duplication candidate: `src/mindroom/cli/__init__.py` and `src/mindroom/__init__.py` both perform import-time vendor telemetry disabling by importing and calling `disable_vendor_telemetry()`.
This is real module-level behavior duplication, but it is small and likely intentional so the CLI package remains protected even if imported without first importing the root package.

## Coverage TSV

```coverage-tsv
qualname	kind	lines	status	search_terms	candidates_checked
MODULE_LEVEL	module	lines 1-5	duplicate-found	disable_vendor_telemetry vendor_telemetry __init__.py telemetry import side effect	src/mindroom/__init__.py:6; src/mindroom/__init__.py:8; src/mindroom/vendor_telemetry.py:16; src/mindroom/tools/composio.py:199
```

## Findings

### Import-time vendor telemetry opt-out is repeated

- Primary behavior: `src/mindroom/cli/__init__.py:3` imports `disable_vendor_telemetry`, and `src/mindroom/cli/__init__.py:5` calls it at package import time.
- Duplicate behavior: `src/mindroom/__init__.py:6` imports the same helper, and `src/mindroom/__init__.py:8` calls it at root package import time.
- Shared implementation: `src/mindroom/vendor_telemetry.py:16` applies `VENDOR_TELEMETRY_ENV_VALUES` to the environment and patches already-loaded vendor modules.

These are functionally the same side effect: importing a package initializer disables known third-party telemetry defaults before more runtime code loads.
The difference to preserve is import order and coverage surface.
`mindroom.__init__` also patches ChromaDB compatibility and exposes `__version__`, while `mindroom.cli.__init__` exists only to protect CLI imports.
`src/mindroom/tools/composio.py:199` also calls `disable_vendor_telemetry()`, but that is a narrower tool-specific guard around Composio behavior rather than a package initializer duplicate.

## Proposed Generalization

No refactor recommended for this five-line module.

If this import-time side effect grows, the minimal generalization would be a single bootstrapping helper such as `mindroom.bootstrap.disable_global_side_effects()` called from both initializers.
For the current code, extracting another wrapper would add indirection without reducing meaningful complexity because the duplicated behavior is already centralized in `mindroom.vendor_telemetry.disable_vendor_telemetry()`.

## Risk/tests

- Risk of removing the CLI initializer call: direct `mindroom.cli` imports could skip telemetry opt-out if the root package initializer is not executed first in an unusual import path.
- Risk of changing root initializer behavior: startup-wide telemetry environment guarantees could shift before dependencies import.
- Tests to consider only if refactoring: assert importing `mindroom` and importing `mindroom.cli` each sets representative telemetry environment variables from `VENDOR_TELEMETRY_ENV_VALUES`.
