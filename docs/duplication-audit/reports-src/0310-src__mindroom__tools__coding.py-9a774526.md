## Summary

Top duplication candidates:

1. `coding_tools` uses the same registered-tool lazy import/return-class wrapper pattern as many modules under `src/mindroom/tools`.
2. The returned `CodingTools` toolkit duplicates part of the older `file` tool behavior: base-dir constrained path resolution plus read/write/list/search-style local file operations.
3. No refactor is recommended from `src/mindroom/tools/coding.py` alone because this file is only registration metadata and a lazy class lookup; meaningful dedupe would need to preserve the intentionally different coding-oriented UX in `src/mindroom/custom_tools/coding.py`.

## Coverage TSV

```coverage-tsv
qualname	kind	lines	status	search_terms	candidates_checked
coding_tools	function	lines 52-56	duplicate-found	coding_tools, registered tool wrapper, return Toolkit class, read_file/write_file/list_files/base_dir/restrict_to_base_dir	src/mindroom/tools/file.py:393; src/mindroom/tools/python.py:115; src/mindroom/tools/daytona.py:185; src/mindroom/custom_tools/coding.py:350; src/mindroom/custom_tools/coding.py:531; src/mindroom/custom_tools/coding.py:585; src/mindroom/custom_tools/coding.py:649; src/mindroom/custom_tools/coding.py:743; src/mindroom/tools/file.py:23; src/mindroom/tools/file.py:108; src/mindroom/tools/file.py:116; src/mindroom/tools/file.py:178; src/mindroom/tools/file.py:212; src/mindroom/tools/file.py:228
```

## Findings

### 1. Repeated registered-tool wrapper pattern

`src/mindroom/tools/coding.py:52` lazily imports `CodingTools` and returns the toolkit class.
The same behavior appears in many tool registration modules, including `src/mindroom/tools/file.py:393`, `src/mindroom/tools/python.py:115`, and `src/mindroom/tools/daytona.py:185`.

The duplicated behavior is the registration module acting as a metadata-bearing factory that returns a toolkit class while keeping heavier imports lazy.
The differences to preserve are the per-tool metadata decorators, type hints, config fields, dependencies, docs URLs, and sometimes wrapper class construction, as in `python_tools`.

This is real but low-value duplication.
A generic helper would not remove much code because the decorator metadata remains tool-specific, and the explicit functions make registry exports readable.

### 2. Overlapping local file operation behavior with `file`

`coding_tools` returns `src/mindroom/custom_tools/coding.py:563` `CodingTools`, which exposes `read_file`, `write_file`, `find_files`, `ls`, `grep`, and `edit_file`.
Several of these overlap with the older file toolkit in `src/mindroom/tools/file.py`.

Shared behavior includes:

- Base-dir constrained path resolution: `src/mindroom/custom_tools/coding.py:350` and `src/mindroom/tools/file.py:108`.
- Blocked outside-base-dir errors: `src/mindroom/custom_tools/coding.py:342` and `src/mindroom/tools/file.py:23`.
- File reads: `src/mindroom/custom_tools/coding.py:531` / `src/mindroom/custom_tools/coding.py:585` and `src/mindroom/tools/file.py:178`.
- File writes: `src/mindroom/custom_tools/coding.py:649` and `src/mindroom/tools/file.py:116`.
- Directory/file discovery: `src/mindroom/custom_tools/coding.py:743` / `src/mindroom/custom_tools/coding.py:786` and `src/mindroom/tools/file.py:212` / `src/mindroom/tools/file.py:228`.

The duplication is functional rather than literal.
Both toolkits resolve relative paths against `base_dir`, optionally prevent traversal outside that base, return string error messages instead of raising to the model, and expose local file IO/discovery primitives.

Important differences to preserve:

- `CodingTools.read_file` returns line-numbered, paginated output with byte/line truncation hints; `MindRoomFileTools.read_file` returns raw contents and asks callers to use `read_file_chunk` when too large.
- `CodingTools.find_files` filters hidden and gitignored files and returns newline output; `MindRoomFileTools.search_files` uses glob and JSON output.
- `CodingTools.ls` includes dotfiles and directory suffixes; `MindRoomFileTools.list_files` returns JSON and uses base-dir-relative formatting.
- `CodingTools.edit_file` supports exact/fuzzy replacement plus diff output; `MindRoomFileTools.replace_file_chunk` uses explicit line ranges.
- `CodingTools.grep` has ripgrep and Python fallback behavior that has no direct equivalent in `file`.

## Proposed Generalization

No refactor recommended for this task.

If production changes are later requested, the smallest useful generalization would be a private shared path helper module for local toolkits, likely under `src/mindroom/tools/path_safety.py` or `src/mindroom/tool_system/path_safety.py`, containing only:

1. Resolve a path against `base_dir`.
2. Enforce or skip `restrict_to_base_dir`.
3. Format the outside-base-dir error message consistently.
4. Optionally format base-dir-relative paths for output.

Do not generalize the higher-level file read/list/search output formats unless a single UX is intentionally chosen.
Those differences are active tool behavior, not accidental duplication.

## Risk/tests

Risks for any future dedupe:

- Error message text changes could break tests or alter model-facing guidance.
- File listing/search output shape differs between newline text and JSON; sharing too much would be a behavior change.
- Symlink and parent traversal handling must remain strict for `restrict_to_base_dir=True`.
- `CodingTools` gitignore/hidden-file filtering and `file` tool JSON/expose-base-directory behavior should remain distinct.

Tests to inspect or add before a future refactor:

- Existing tool registration tests that load `coding`, `file`, and `python` tools.
- Path escape tests for both `coding` and `file` tools.
- Read/write/list/search behavior tests preserving current output formats.
- A symlink traversal case with `restrict_to_base_dir=True`.
