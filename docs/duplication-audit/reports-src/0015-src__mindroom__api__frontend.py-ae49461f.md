## Summary

No meaningful duplication found.
`src/mindroom/api/frontend.py` is the only source module that resolves request paths into bundled dashboard files and serves the SPA fallback.
Related code exists for locating/building the frontend dist directory and for browser login redirects, but those modules provide collaborators rather than duplicate the asset-serving behavior.

## Coverage TSV

```coverage-tsv
qualname	kind	lines	status	search_terms	candidates_checked
_resolve_frontend_asset	function	lines 19-42	none-found	_resolve_frontend_asset, index.html, PurePosixPath, unquote, FileResponse, StaticFiles, path traversal	src/mindroom/frontend_assets.py:21; src/mindroom/api/frontend.py:19; src/mindroom/api/knowledge.py:624; src/mindroom/api/sandbox_exec.py:115; src/mindroom/knowledge/manager.py:512
serve_frontend	async_function	lines 47-68	related-only	serve_frontend, request_has_frontend_access, login_redirect_for_request, sanitize_next_path, ensure_frontend_dist_dir, API route prefixes	src/mindroom/api/auth.py:377; src/mindroom/api/auth.py:411; src/mindroom/api/auth.py:424; src/mindroom/api/auth.py:634; src/mindroom/api/oauth.py:90; src/mindroom/frontend_assets.py:33; src/mindroom/cli/main.py:160; src/mindroom/api/main.py:820
```

## Findings

No real duplication found.

`_resolve_frontend_asset` has related path-normalization patterns elsewhere, but no duplicate behavior.
`src/mindroom/api/sandbox_exec.py:115` normalizes a configured storage subpath into `Path.parts`, and `src/mindroom/knowledge/manager.py:512` strips leading/trailing slashes from a knowledge-base path.
Those checks do not resolve URL-decoded frontend requests, do not block `..` in `PurePosixPath.parts`, do not serve nested `index.html`, and do not implement SPA fallback behavior.

`serve_frontend` composes existing collaborators rather than duplicating them.
Frontend access validation lives in `src/mindroom/api/auth.py:377`, redirect sanitization in `src/mindroom/api/auth.py:411`, login redirect construction in `src/mindroom/api/auth.py:424`, and frontend dist discovery/building in `src/mindroom/frontend_assets.py:33`.
`src/mindroom/api/oauth.py:90` has a related browser-login redirect flow after an OAuth API-user check, but it does not serve static assets or apply the dashboard catch-all routing rules.
`src/mindroom/cli/main.py:160` also calls `ensure_frontend_dist_dir`, but only to print dashboard availability at startup.

## Proposed Generalization

No refactor recommended.
The current module is already the single source for dashboard request-path resolution and SPA static serving, while auth and asset-location concerns are factored into focused helpers.

## Risk/Tests

The main behavior risks are path traversal protection, preserving SPA fallback for extensionless routes, not shadowing `/api` or `/v1`, and preserving browser login redirects.
Existing tests in `tests/api/test_api.py` cover root serving, SPA fallback, API-route shadowing, traversal blocking, API-key login redirects, authenticated serving, and platform-cookie frontend access.
