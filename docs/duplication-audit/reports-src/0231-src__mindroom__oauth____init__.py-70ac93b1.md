Summary: No meaningful duplication found.
`src/mindroom/oauth/__init__.py` is a package-level import surface that re-exports OAuth provider types, registry loaders, and service URL helpers.
Related barrel-export patterns exist in other package `__init__.py` files, but they do not duplicate OAuth behavior or justify a shared abstraction.

```coverage-tsv
qualname	kind	lines	status	search_terms	candidates_checked
MODULE_LEVEL	module	lines 1-29	related-only	oauth __init__ __all__ re-export OAuthProvider load_oauth_providers build_oauth_authorize_url; from mindroom.oauth import; package-level import surfaces	src/mindroom/oauth/__init__.py:1, src/mindroom/api/oauth.py:24, src/mindroom/memory/__init__.py:1, src/mindroom/history/__init__.py:1, src/mindroom/matrix/cache/__init__.py:1
```

Findings:
No real duplicated behavior was found for this primary file.
The module only centralizes imports from `mindroom.oauth.providers`, `mindroom.oauth.registry`, and `mindroom.oauth.service`, then lists the public names in `__all__`.
`src/mindroom/api/oauth.py:24` consumes this public OAuth package surface for a few exported symbols.
Other modules such as `src/mindroom/memory/__init__.py:1`, `src/mindroom/history/__init__.py:1`, and `src/mindroom/matrix/cache/__init__.py:1` use the same package-boundary re-export idiom, but that is a normal Python package pattern rather than duplicated runtime behavior.

Proposed generalization:
No refactor recommended.
Generating `__all__` dynamically or introducing a shared package-export helper would add indirection without reducing meaningful behavior duplication.

Risk/tests:
No behavior risk because no production change is proposed.
If this import surface changes later, existing import smoke tests or API-route tests that import `mindroom.oauth` and `mindroom.api.oauth` would be sufficient.
