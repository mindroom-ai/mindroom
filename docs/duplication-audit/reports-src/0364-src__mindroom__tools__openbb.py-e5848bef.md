Summary: The OpenBB import-time environment guard is specific to `src/mindroom/tools/openbb.py`; no matching `OPENBB_AUTO_BUILD` or temporary environment override helper was found elsewhere under `src`.
The public `openbb_tools` factory duplicates the common MindRoom tool wrapper pattern used by many `src/mindroom/tools/*.py` modules, but the OpenBB-specific loader makes a shared refactor low value.

```coverage-tsv
qualname	kind	lines	status	search_terms	candidates_checked
_load_openbb_tools	function	lines 15-26	none-found	OPENBB_AUTO_BUILD; importlib.import_module; previous env restore; os.environ temporary override	src/mindroom/tools/openbb.py:17; src/mindroom/embeddings.py:91; src/mindroom/model_loading.py:79; src/mindroom/api/integrations.py:72; src/mindroom/workers/backends/kubernetes_resources.py:586; src/mindroom/api/auth.py:160; src/mindroom/tools/agentql.py:20
openbb_tools	function	lines 116-118	related-only	def *_tools() factories; register_tool_with_metadata wrappers; financial tool factories	src/mindroom/tools/yfinance.py:108; src/mindroom/tools/financial_datasets_api.py:50; src/mindroom/tools/pandas.py:49; src/mindroom/tools/openbb.py:116
```

Findings:

1. Related-only: toolkit factory wrappers are repeated across tool modules.
   `openbb_tools` at `src/mindroom/tools/openbb.py:116` returns a toolkit class through `_load_openbb_tools`.
   Similar registry-facing wrappers exist at `src/mindroom/tools/yfinance.py:108`, `src/mindroom/tools/financial_datasets_api.py:50`, and `src/mindroom/tools/pandas.py:49`.
   These functions all provide a zero-argument factory for `register_tool_with_metadata`, but most directly import and return an Agno toolkit class while OpenBB needs the `OPENBB_AUTO_BUILD=false` import guard.
   The shared behavior is the registry factory shape, not the OpenBB import behavior.

No real duplication was found for `_load_openbb_tools`.
Searches for `OPENBB_AUTO_BUILD`, temporary `os.environ` mutation and restoration, and `importlib.import_module` candidates found only generic lazy imports in `src/mindroom/embeddings.py:91`, `src/mindroom/model_loading.py:79`, `src/mindroom/api/integrations.py:72`, `src/mindroom/workers/backends/kubernetes_resources.py:586`, `src/mindroom/api/auth.py:160`, and `src/mindroom/tools/agentql.py:20`.
Those candidates do not temporarily override process environment variables around an import.

Proposed generalization: No refactor recommended.
The duplicated factory shape is intentional registry boilerplate and OpenBB's loader has a unique side effect constraint.
A generic lazy-toolkit helper would save only a couple of lines per module while making tool registration less explicit.

Risk/tests: If this area is changed later, preserve restoration of a pre-existing `OPENBB_AUTO_BUILD` value and removal when it was initially unset.
Useful coverage would assert `_load_openbb_tools` restores both unset and pre-set environment states, ideally with `importlib.import_module` patched.
