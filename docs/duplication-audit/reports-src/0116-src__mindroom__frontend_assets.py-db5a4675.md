## Summary

The only meaningful duplication candidate is the frontend build command sequence in `src/mindroom/frontend_assets.py` compared with the root-level Hatch build hook, but that candidate is outside the `./src` search scope for this assignment.
Within `./src`, frontend asset directory resolution and optional source-checkout building appear centralized in `src/mindroom/frontend_assets.py`; callers in the API and CLI consume `ensure_frontend_dist_dir` rather than duplicating it.

## Coverage TSV

```coverage-tsv
qualname	kind	lines	status	search_terms	candidates_checked
_resolve_frontend_dist_dir	function	lines 21-30	related-only	frontend_dist FRONTEND_DIST MINDROOM_FRONTEND_DIST runtime_env_path bundled _frontend dist	src/mindroom/api/frontend.py:60; src/mindroom/cli/main.py:160; src/mindroom/constants.py:776; src/mindroom/constants.py:908; src/mindroom/credentials_sync.py:54; src/mindroom/credentials_sync.py:183; src/mindroom/credentials_sync.py:339
ensure_frontend_dist_dir	function	lines 33-39	none-found	ensure_frontend_dist_dir frontend assets missing frontend/dist StaticFiles FileResponse Dashboard assets	src/mindroom/api/frontend.py:60; src/mindroom/cli/main.py:160; src/mindroom/api/frontend.py:19; src/mindroom/api/frontend.py:64
_build_repo_frontend_dist	function	lines 42-65	related-only	bun install frozen-lockfile bun run tsc vite build subprocess.run shutil.which MINDROOM_AUTO_BUILD_FRONTEND	src/mindroom/tool_system/dependencies.py:139; src/mindroom/tool_system/dependencies.py:175; src/mindroom/tool_system/dependencies.py:192; src/mindroom/cli/local_stack.py:194; src/mindroom/cli/local_stack.py:309; src/mindroom/custom_tools/coding.py:390; src/mindroom/knowledge/manager.py:235; hatch_build.py:57
```

## Findings

No active duplicated behavior was found elsewhere under `./src`.

`_resolve_frontend_dist_dir` centralizes the frontend directory precedence: `MINDROOM_FRONTEND_DIST`, packaged `_frontend`, then repo `frontend/dist`.
The API and CLI call the public helper at `src/mindroom/api/frontend.py:60` and `src/mindroom/cli/main.py:160`.
Other `runtime_env_path` call sites in `src/mindroom/credentials_sync.py:54`, `src/mindroom/credentials_sync.py:183`, and `src/mindroom/credentials_sync.py:339` are related runtime-env path resolution, but they resolve credential files rather than frontend assets.
`src/mindroom/constants.py:908` has a related bundled asset root helper for avatars, but its behavior is a static package/source path lookup and not the same override-plus-dist resolution.

`ensure_frontend_dist_dir` is the single orchestration point for "return existing assets or try building them".
The matching call sites do not reimplement the fallback logic.
`src/mindroom/api/frontend.py:19` resolves an already-selected frontend directory to a requested static file or SPA fallback, which is adjacent behavior but not duplicate directory discovery or build orchestration.

`_build_repo_frontend_dist` has related command-running patterns inside `src`, especially optional dependency installation in `src/mindroom/tool_system/dependencies.py:139`, `src/mindroom/tool_system/dependencies.py:175`, and `src/mindroom/tool_system/dependencies.py:192`.
Those helpers also locate executables and run subprocesses, but they manage Python package installation, return booleans, pass telemetry environment values, and use different error semantics.
The closest true duplicate command sequence is `hatch_build.py:57`, which runs `bun install --frozen-lockfile`, `bun run tsc`, and `bun run vite build`.
That file is outside `./src`, uses retries for install, builds into a passed output directory, and is part of packaging rather than runtime source-checkout serving.

## Proposed Generalization

No refactor recommended for `./src`.

If the audit scope later includes root build hooks, a small shared frontend build helper could be considered for the Bun command sequence.
It would need to preserve runtime behavior in `frontend_assets.py` where builds are optional, one-shot per process, disabled by `MINDROOM_AUTO_BUILD_FRONTEND=0`, and target the repo `frontend/dist`.
It would also need to preserve Hatch behavior where wheel builds are mandatory for standard wheels, editable builds may skip missing Bun, install retries are applied, and Vite receives an explicit `--outDir`.

## Risk/tests

No production changes were made.
If a future refactor extracts shared frontend build logic across `src/mindroom/frontend_assets.py` and `hatch_build.py`, tests should cover `tests/api/test_api.py:241`, `tests/api/test_api.py:274`, `tests/api/test_api.py:297`, and `tests/test_hatch_build.py:88`.
The main behavior risks would be changing when runtime startup auto-builds, whether missing Bun is tolerated, whether install retries apply, and whether output directories are cleaned or preserved.
