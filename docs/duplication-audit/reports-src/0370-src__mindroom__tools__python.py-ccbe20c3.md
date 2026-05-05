## Summary

Top duplication candidate: `src/mindroom/tools/python.py` intentionally replaces two near-duplicate Agno installer methods with one local shared installer path.
The only meaningful local overlap is related installer command construction in `src/mindroom/tool_system/dependencies.py`, which is already reused by `python.py`.
No additional source-level refactor is recommended.

## Coverage TSV

```coverage-tsv
qualname	kind	lines	status	search_terms	candidates_checked
_install_package_with_current_python	function	lines 28-30	related-only	install_command_for_current_python; subprocess.check_call; python -m pip; uv pip install	src/mindroom/tool_system/dependencies.py:158; src/mindroom/tool_system/dependencies.py:192; .venv/lib/python3.13/site-packages/agno/tools/python.py:173; .venv/lib/python3.13/site-packages/agno/tools/python.py:194
_install_package_with_status	function	lines 33-51	duplicate-found	successfully installed package; Error installing package; Installing package; pip_install_package; uv_pip_install_package	.venv/lib/python3.13/site-packages/agno/tools/python.py:173; .venv/lib/python3.13/site-packages/agno/tools/python.py:194; tests/test_python_tools.py:43
_python_tools_runtime	function	lines 54-59	related-only	from agno.tools.python; lazy runtime; return PythonTools warn log_debug logger	src/mindroom/tools/cartesia.py:79; src/mindroom/tools/openbb.py:12; src/mindroom/tools/file.py:10; src/mindroom/tools/file.py:69
python_tools	function	lines 115-140	related-only	register_tool_with_metadata python; return MindRoomPythonTools; wrapper around Agno	src/mindroom/tools/file.py:267; src/mindroom/tools/file.py:395; src/mindroom/tools/shell.py:252; src/mindroom/tools/shell.py:561; src/mindroom/tools/__init__.py:100
python_tools.<locals>.pip_install_package	nested_function	lines 122-129	duplicate-found	pip_install_package; install package current environment; warn log_debug check_call	.venv/lib/python3.13/site-packages/agno/tools/python.py:173; src/mindroom/tools/python.py:131; tests/test_python_tools.py:33
python_tools.<locals>.uv_pip_install_package	nested_function	lines 131-138	duplicate-found	uv_pip_install_package; install package current environment; warn log_debug check_call	.venv/lib/python3.13/site-packages/agno/tools/python.py:194; src/mindroom/tools/python.py:122; tests/test_python_tools.py:33
```

## Findings

### 1. Agno Python package installer behavior is duplicated and locally consolidated

- Primary behavior: `src/mindroom/tools/python.py:33` wraps package installation with `warn()`, Agno debug logging, MindRoom structured logging, installer execution, and string responses.
- Original candidate: `.venv/lib/python3.13/site-packages/agno/tools/python.py:173` implements `pip_install_package` with the same user-facing success and error strings, `warn()`, debug logging, and `subprocess.check_call`.
- Original candidate: `.venv/lib/python3.13/site-packages/agno/tools/python.py:194` repeats the same flow for `uv_pip_install_package`, differing only in the command.
- Primary nested methods: `src/mindroom/tools/python.py:122` and `src/mindroom/tools/python.py:131` both delegate to `_install_package_with_status`, so the duplication is already reduced inside MindRoom.

Differences to preserve: MindRoom intentionally uses `install_command_for_current_python()` instead of Agno's hard-coded `sys.executable -m pip install` or `sys.executable -m uv pip install`.
MindRoom also logs `python_package_install_started` and `python_package_install_failed` through its structured logger, while preserving Agno-style return strings covered by `tests/test_python_tools.py:43`.

### 2. Current-interpreter package install command construction is related shared behavior

- Primary behavior: `src/mindroom/tools/python.py:28` executes `[*install_command_for_current_python(), package_name]`.
- Related source: `src/mindroom/tool_system/dependencies.py:158` builds the command for the active interpreter, preferring the current Python's `uv` module, then the `uv` executable, then `pip`.
- Related source: `src/mindroom/tool_system/dependencies.py:192` uses the same command builder to install MindRoom optional extras.

This is related behavior, not unmanaged duplication, because `python.py` already delegates command selection to the shared dependency helper.
The remaining difference is execution style: `python.py` uses `subprocess.check_call` and catches exceptions to return tool-readable strings, while dependency auto-install uses `subprocess.run(..., check=False)` and returns booleans.

### 3. Tool factory and lazy Agno loading patterns are common but not duplicate behavior needing extraction

- Primary behavior: `src/mindroom/tools/python.py:54` lazily imports Agno runtime pieces because the wrapper needs Agno's module-level `warn`, `log_debug`, and logger objects.
- Related wrappers: many simple tool modules import an Agno class inside the factory, such as `src/mindroom/tools/cartesia.py:79`.
- Related custom wrapper: `src/mindroom/tools/file.py:69` subclasses Agno file tools, but imports Agno at module import time and implements different file safety behavior.

The pattern is related registration/loading structure, but the runtime tuple is specific to PythonTools and its installer override.
Extracting a generic Agno runtime loader would add indirection without reducing active duplication.

## Proposed Generalization

No refactor recommended.
The active duplication from Agno's two installer methods has already been centralized locally in `_install_package_with_status`, and command construction already uses the shared dependency helper.
If this area changes later, the smallest useful extraction would be a package-install execution helper in `src/mindroom/tool_system/dependencies.py` that returns a structured success/error result, but that is not currently justified because the only string-returning tool caller is `python.py`.

## Risk/tests

Primary risks are user-visible installer command selection and exact tool response strings.
Tests that would need attention for any future refactor are `tests/test_python_tools.py`, especially coverage for both installer entrypoints and failure responses, plus `tests/test_tool_dependencies.py` for `install_command_for_current_python()`.
No tests were run for this audit because the requested work was source inspection and report generation only.
