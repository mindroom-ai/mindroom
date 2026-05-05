## Summary

`shopify_tools` duplicates the lazy Agno toolkit factory shape used across the built-in tool registration modules.
The duplication is real but intentionally small: each module has one registered factory that imports and returns its provider-specific toolkit class.

## Coverage TSV

```coverage-tsv
qualname	kind	lines	status	search_terms	candidates_checked
shopify_tools	function	lines 73-77	duplicate-found	shopify_tools, def *_tools() -> type, lazy import return Agno toolkit, register_tool_with_metadata	src/mindroom/tools/openweather.py:77; src/mindroom/tools/linear.py:44; src/mindroom/tools/notion.py:73; src/mindroom/tools/__init__.py:112
```

## Findings

### Lazy toolkit factory boilerplate

`src/mindroom/tools/shopify.py:73` defines `shopify_tools`, imports `ShopifyTools` inside the function, and returns the toolkit class.
The same behavior appears in many sibling modules, including `src/mindroom/tools/openweather.py:77`, `src/mindroom/tools/linear.py:44`, and `src/mindroom/tools/notion.py:73`.
In each case, the registered factory defers importing the Agno toolkit until the registry needs it, then returns the concrete toolkit class.

The differences to preserve are the concrete import path, return type, docstring, and metadata attached by `register_tool_with_metadata`.
The Shopify module also has Shopify-specific configuration fields (`shop_name`, `access_token`, `api_version`, `timeout`) and a Shopify-specific function list, so only the tiny factory body is duplicated.

`src/mindroom/tools/__init__.py:112` imports and exports `shopify_tools` alongside the other factories.
That is related registry wiring, not duplicate behavior by itself.

## Proposed Generalization

No refactor recommended for `shopify_tools` alone.
The repeated lazy factory pattern is widespread, but replacing it would require a shared dynamic importer or metadata-driven factory creation across many tool modules.
That would reduce only three lines per tool while making type checking, docstrings, and explicit imports less direct.

If this pattern becomes a maintenance problem across the tool catalog, a small helper in `mindroom.tool_system.metadata` or a new `mindroom.tools.factory` module could build a lazy toolkit factory from an import path and class name.
That should be evaluated as a separate catalog-wide cleanup, not as a Shopify-specific change.

## Risk/tests

The main risk of generalizing is changing import timing or error surfacing for optional Agno dependencies.
Tests would need to cover metadata registration, factory lookup, lazy import behavior for configured and unconfigured tools, and a missing optional dependency case.
For the current Shopify module, no production change is recommended and no tests are required.
