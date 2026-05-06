## Summary

The only duplicated behavior in `src/mindroom/tools/moviepy_video_tools.py` is the repeated tool-wrapper factory pattern used by many files in `src/mindroom/tools`.
No separate MoviePy video processing, caption parsing, SRT generation, or audio extraction implementation was found under `./src`; those behaviors are delegated to `agno.tools.moviepy_video.MoviePyVideoTools`.

## Coverage TSV

```coverage-tsv
qualname	kind	lines	status	search_terms	candidates_checked
moviepy_video_tools	function	lines 63-67	duplicate-found	"moviepy_video_tools", "MoviePyVideoTools", "def .*tools", "return .*Tools", video/caption/audio tool wrappers	src/mindroom/tools/lumalabs.py:77-81; src/mindroom/tools/youtube.py:75-79; src/mindroom/tools/gemini.py:91-95; src/mindroom/tools/openai.py:119-123
```

## Findings

### Repeated decorated toolkit factory

`src/mindroom/tools/moviepy_video_tools.py:63-67` is a lazy import factory that imports one Agno toolkit class inside the function and returns the class.
The same behavior appears in `src/mindroom/tools/lumalabs.py:77-81`, `src/mindroom/tools/youtube.py:75-79`, `src/mindroom/tools/gemini.py:91-95`, and `src/mindroom/tools/openai.py:119-123`.
Each module also pairs that factory with `@register_tool_with_metadata` metadata and `TYPE_CHECKING` imports.

This is structural duplication rather than duplicated video-processing logic.
Differences to preserve are the per-tool metadata, config fields, dependency names, docs URLs, function names, and returned Agno toolkit class.

## Proposed Generalization

No refactor recommended for this file alone.
The repeated wrapper shape is active across the tool registry, but extracting it would need to preserve decorator-time metadata registration and type-checker friendliness across many modules.
A worthwhile future refactor would need to address the whole `src/mindroom/tools` registration pattern, not only this small MoviePy wrapper.

## Risk/tests

The main risk in generalizing this pattern is changing when optional Agno dependencies are imported.
These wrappers intentionally defer runtime imports until the tool class is requested.
Tests should cover metadata registration for `moviepy_video_tools`, lazy import behavior when optional dependencies are absent, and successful class resolution when `moviepy`/Agno dependencies are installed.
