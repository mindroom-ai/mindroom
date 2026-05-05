## Summary

No meaningful Spotify-specific duplication found.
The only required behavior symbol, `spotify_tools`, is a conventional registered tool factory that lazily imports and returns an Agno toolkit class.
That factory shape is repeated across many `src/mindroom/tools/*` modules, but the repeated behavior is intentionally local metadata registration and does not justify a refactor for this file alone.

## Coverage TSV

```coverage-tsv
qualname	kind	lines	status	search_terms	candidates_checked
spotify_tools	function	lines 71-75	related-only	SpotifyTools; spotify_tools; name="spotify"; from agno.tools.* import *Tools; return *Tools	src/mindroom/tools/spotify.py:71; src/mindroom/tools/shopify.py:73; src/mindroom/tools/youtube.py:75; src/mindroom/tools/notion.py:73; src/mindroom/tools/todoist.py:43; src/mindroom/tools/cartesia.py:77; src/mindroom/tools/lumalabs.py:77; src/mindroom/tools/giphy.py:56; src/mindroom/api/integrations.py:67; src/mindroom/tools_metadata.json:6364; src/mindroom/tools/__init__.py:116
```

## Findings

No real duplication requiring refactor was found for `spotify_tools`.

Related pattern: lazy Agno toolkit factory wrappers are repeated in many tool modules.
`src/mindroom/tools/spotify.py:71` imports `SpotifyTools` inside the factory and returns the class.
The same behavior shape appears in `src/mindroom/tools/shopify.py:73`, `src/mindroom/tools/youtube.py:75`, `src/mindroom/tools/notion.py:73`, `src/mindroom/tools/todoist.py:43`, `src/mindroom/tools/cartesia.py:77`, `src/mindroom/tools/lumalabs.py:77`, and `src/mindroom/tools/giphy.py:56`.
These are functionally similar because each registered factory defers importing an optional Agno toolkit until the tool registry asks for it.
The differences to preserve are the concrete toolkit import path, return type, metadata decorator arguments, dependencies, config fields, docs URL, and function names.

Spotify OAuth support in `src/mindroom/api/integrations.py:67` is related to the same external service but not duplicate behavior.
That module installs and imports `spotipy`, creates OAuth clients, checks stored credentials, and saves callback credentials.
`spotify_tools` only exposes the Agno `SpotifyTools` toolkit class and does not perform OAuth, credential persistence, or API calls itself.

`src/mindroom/tools_metadata.json:6364` contains generated/exported Spotify metadata that mirrors the decorator data.
It is a derived metadata artifact rather than independent source behavior to generalize from this module.

## Proposed Generalization

No refactor recommended for this file.
Although a tiny helper such as `make_agno_tool_factory(module_path, class_name)` could reduce the repeated three-line factory bodies, it would move type-specific imports and annotations into strings and make individual tool modules less explicit.
For this file, the existing direct factory is clearer and lower risk.

## Risk/Tests

No production code was changed.
If the broader lazy-factory pattern were ever generalized, tests should verify registry loading, optional dependency behavior, type metadata export, and tool instantiation for representative configured and unconfigured tools.
Spotify-specific tests should continue to cover metadata export and the separate OAuth credential flow because those behaviors live outside `spotify_tools`.
