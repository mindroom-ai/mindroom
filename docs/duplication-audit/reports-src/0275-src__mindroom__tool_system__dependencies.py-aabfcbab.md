## Summary

Top duplication candidates:

- `src/mindroom/tool_system/metadata.py:616` duplicates the dependency pre-check, optional-extra auto-install, cache invalidation, and missing-dependency error flow that `ensure_optional_deps()` and `ensure_tool_deps()` already centralize.
- `src/mindroom/tool_system/metadata.py:631` and `src/mindroom/api/auth.py:159` both implement lazy import failure handling followed by optional-extra auto-install and retry.
- `src/mindroom/tools/python.py:28` intentionally reuses `install_command_for_current_python()` for user-requested package installs, so no separate install-command duplication was found outside the dependency helper.

## Coverage TSV

```coverage-tsv
qualname	kind	lines	status	search_terms	candidates_checked
_pip_name_to_import	function	lines 51-61	related-only	pip import name mapping find_spec package dependency import names	src/mindroom/tool_system/metadata.py:616; src/mindroom/tools/*.py dependency declarations; src/mindroom/tools/e2b.py:45; src/mindroom/tools/github.py:42
_normalize_extra_name	function	lines 64-66	related-only	extra normalize replace underscore dash optional extras	src/mindroom/model_loading.py:40; src/mindroom/tool_system/skills.py:305; src/mindroom/cli/connect.py:160
check_deps_installed	function	lines 69-75	duplicate-found	check_deps_installed find_spec dependencies pre-check missing deps	src/mindroom/tool_system/metadata.py:616; src/mindroom/tool_system/metadata.py:619; src/mindroom/runtime_support.py:91; src/mindroom/embeddings.py:78
auto_install_enabled	function	lines 78-81	duplicate-found	MINDROOM_NO_AUTO_INSTALL_TOOLS auto_install_enabled disabled hint env_value	src/mindroom/api/auth.py:163; src/mindroom/tool_system/metadata.py:620; src/mindroom/tool_system/metadata.py:633
_has_lockfile	function	lines 84-86	none-found	uv.lock lockfile pyproject install sync	src/mindroom/tool_system/dependencies.py:216; none outside primary
_available_optional_extras	function	lines 90-102	none-found	project.optional-dependencies Provides-Extra importlib.metadata pyproject tomllib	src/mindroom/tool_system/dependencies.py:107; pyproject.toml:58; none outside primary
_resolve_optional_extra_name	function	lines 105-115	none-found	resolve optional extra normalized matching available extras	src/mindroom/tool_system/dependencies.py:225; none outside primary
_is_uv_tool_install	function	lines 118-120	none-found	uv-receipt.toml uv tool sys.prefix receipt	src/mindroom/tool_system/dependencies.py:210; none outside primary
_in_virtualenv	function	lines 123-124	related-only	sys.prefix sys.base_prefix virtualenv venv install command	src/mindroom/tool_system/dependencies.py:160; src/mindroom/tool_system/dependencies.py:180; src/mindroom/tool_system/dependencies.py:216
_get_current_uv_tool_extras	function	lines 127-136	none-found	uv-receipt requirements extras tool receipt tomllib	src/mindroom/tool_system/dependencies.py:211; none outside primary
_install_via_uv_tool	function	lines 139-150	related-only	uv tool install force python vendor telemetry subprocess	src/mindroom/tool_system/dependencies.py:215; src/mindroom/api/sandbox_exec.py:241; src/mindroom/api/sandbox_exec.py:296; src/mindroom/tools/shell.py:169
_current_python_has_module	function	lines 153-155	related-only	importlib.util.find_spec current python module uv	src/mindroom/tool_system/dependencies.py:161; src/mindroom/tool_system/dependencies.py:73
install_command_for_current_python	function	lines 158-172	related-only	uv pip install --python sys.executable pip install --user	src/mindroom/tools/python.py:28; src/mindroom/tools/python.py:30
_install_via_uv_sync	function	lines 175-189	related-only	uv sync locked inexact no-dev active VIRTUAL_ENV vendor telemetry	src/mindroom/tool_system/dependencies.py:217; src/mindroom/api/sandbox_exec.py:241; src/mindroom/api/sandbox_exec.py:296; src/mindroom/tools/shell.py:169
_install_in_environment	function	lines 192-199	related-only	install_command_for_current_python package_spec subprocess capture_output vendor telemetry	src/mindroom/tools/python.py:28; src/mindroom/tools/python.py:30
_install_optional_extras	function	lines 202-218	duplicate-found	install optional extras uv tool uv sync pip merge extras auto install	src/mindroom/tool_system/metadata.py:620; src/mindroom/tool_system/metadata.py:633; src/mindroom/api/auth.py:165
auto_install_optional_extra	function	lines 221-228	duplicate-found	auto install optional extra enabled resolve extra quiet retry	src/mindroom/api/auth.py:163; src/mindroom/api/auth.py:165; src/mindroom/tool_system/metadata.py:620; src/mindroom/tool_system/metadata.py:633
auto_install_tool_extra	function	lines 231-233	duplicate-found	auto_install_tool_extra tool_name runtime_paths optional extra	src/mindroom/tool_system/metadata.py:620; src/mindroom/tool_system/metadata.py:633; src/mindroom/api/auth.py:165
ensure_optional_deps	function	lines 236-244	duplicate-found	ensure optional deps missing dependencies install extra invalidate caches ImportError	src/mindroom/tool_system/metadata.py:616; src/mindroom/tool_system/metadata.py:619; src/mindroom/tool_system/metadata.py:624; src/mindroom/tool_system/metadata.py:627
ensure_tool_deps	function	lines 247-255	duplicate-found	ensure tool deps tool extra dependencies missing auto install import caches	src/mindroom/tool_system/metadata.py:616; src/mindroom/tool_system/metadata.py:620; src/mindroom/tool_system/metadata.py:627; src/mindroom/api/integrations.py:20
```

## Findings

### 1. Tool metadata repeats optional dependency pre-check and install handling

`src/mindroom/tool_system/metadata.py:616` gets the tool dependency list, calls `check_deps_installed()`, tries `auto_install_tool_extra()`, formats a missing-dependency `ImportError`, and calls `importlib.invalidate_caches()` after install.
That is the same behavior as `ensure_tool_deps()` via `ensure_optional_deps()` in `src/mindroom/tool_system/dependencies.py:236`.

Differences to preserve:

- `metadata.py:622` emits tool-specific warnings before raising.
- The error message includes the tool name, while `ensure_optional_deps()` currently emits a generic `pip install 'mindroom[extra]'` hint.
- `metadata.py:631` also has a safety-net retry after an unanticipated `ImportError` from `build()`, which is not covered by `ensure_tool_deps()`.

### 2. Lazy import auto-install/retry is repeated in tool metadata and Supabase auth

`src/mindroom/tool_system/metadata.py:631` catches `ImportError`, calls `auto_install_tool_extra()`, invalidates import caches, and retries `build()`.
`src/mindroom/api/auth.py:159` catches `ModuleNotFoundError`, checks/install the `supabase` extra with `auto_install_enabled()` and `auto_install_tool_extra()`, then retries `importlib.import_module("supabase")`.

Both are implementations of "try to import/build, install optional extra on missing dependency, retry once."

Differences to preserve:

- Supabase auth adds a disabled-auto-install hint from `MINDROOM_NO_AUTO_INSTALL_TOOLS`.
- Tool metadata logs first and second import failures with tool names.
- The tool metadata catch is broader (`ImportError`) because nested imports from a tool can fail, while Supabase only catches the top-level `ModuleNotFoundError`.

### 3. Install command construction is centralized already

`src/mindroom/tools/python.py:28` installs arbitrary user-requested packages, but it delegates command construction to `install_command_for_current_python()` in the audited module.
The subprocess invocation is intentionally separate because the Python tool needs response formatting, Agno warnings, and exception-to-string behavior for chat tool output.

No refactor is recommended for this path.

## Proposed Generalization

Add one small helper in `src/mindroom/tool_system/dependencies.py` only if this code is being touched for dependency behavior:

- `ensure_importable_or_install(dependencies, extra_name, runtime_paths, *, missing_message: str | None = None)`, or extend `ensure_optional_deps()` with an optional message callback.
- Use it from `metadata.py` for the pre-check block while keeping metadata-specific logging around it.
- Consider a second narrowly scoped helper for retrying a callable after `ImportError`, but only if both `metadata.py` and `api/auth.py` can use it without weakening their different exception scopes and messages.

No broad architecture change is recommended.

## Risk/Tests

Primary behavior risks:

- Missing-dependency errors must keep the existing user-facing hints and tool-specific logs.
- Retrying after install must still call `importlib.invalidate_caches()` before the second import/build.
- `metadata.py` must preserve the safety-net behavior for dependencies not listed in tool metadata.
- Supabase auth must preserve the explicit disabled-auto-install hint.

Tests to cover before refactoring:

- Tool build with declared missing deps where auto-install succeeds.
- Tool build with declared missing deps where auto-install is disabled or unavailable.
- Tool build where the pre-check passes but `build()` raises `ImportError`, then auto-install succeeds on retry.
- Supabase auth import with missing `supabase`, including disabled-auto-install messaging.
