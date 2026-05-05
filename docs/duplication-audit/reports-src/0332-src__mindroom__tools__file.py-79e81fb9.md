Summary: Top duplication candidates are the local-file path restriction/read/write/glob behavior repeated between `src/mindroom/tools/file.py` and `src/mindroom/custom_tools/coding.py`.
`src/mindroom/workspaces.py` and `src/mindroom/api/knowledge.py` contain related containment helpers, but those are domain-specific validators rather than duplicate file-tool operations.

```coverage-tsv
qualname	kind	lines	status	search_terms	candidates_checked
_blocked_path_message	function	lines 23-28	duplicate-found	blocked path outside base_dir restrict_to_base_dir message	src/mindroom/custom_tools/coding.py:342; src/mindroom/workspaces.py:52; src/mindroom/api/knowledge.py:84
_format_path_for_output	function	lines 31-36	duplicate-found	relative_to base_dir fallback absolute output	src/mindroom/custom_tools/coding.py:510; src/mindroom/custom_tools/coding.py:903; src/mindroom/api/knowledge.py:106
_is_within_base_dir	function	lines 39-45	duplicate-found	resolve relative_to base_dir is_relative_to containment	src/mindroom/custom_tools/coding.py:350; src/mindroom/workspaces.py:37; src/mindroom/api/knowledge.py:74; src/mindroom/tool_system/output_files.py:405
_split_search_pattern	function	lines 48-66	related-only	glob has_magic normalize search root absolute pattern	src/mindroom/custom_tools/coding.py:489; src/mindroom/custom_tools/coding.py:703; src/mindroom/custom_tools/coding.py:1062
MindRoomFileTools	class	lines 69-264	duplicate-found	file tool toolkit read write list search base_dir restriction	src/mindroom/custom_tools/coding.py:563; src/mindroom/tools/python.py:119; src/mindroom/tools/coding.py:20
MindRoomFileTools.__init__	method	lines 72-106	related-only	tool init base_dir restrict_to_base_dir enable file functions	src/mindroom/custom_tools/coding.py:570; src/mindroom/tools/python.py:119; src/mindroom/tools/coding.py:30
MindRoomFileTools.check_escape	method	lines 108-114	duplicate-found	resolve path relative to base_dir optionally prevent traversal	src/mindroom/custom_tools/coding.py:350; src/mindroom/workspaces.py:37; src/mindroom/api/knowledge.py:74
MindRoomFileTools.save_file	method	lines 116-133	duplicate-found	write_text mkdir parents overwrite base_dir restriction	src/mindroom/custom_tools/coding.py:649; src/mindroom/api/skills.py:84; src/mindroom/api/skills.py:104
MindRoomFileTools.read_file_chunk	method	lines 135-148	related-only	read file selected line range split separator	src/mindroom/custom_tools/coding.py:585; src/mindroom/custom_tools/coding.py:412
MindRoomFileTools.replace_file_chunk	method	lines 150-176	related-only	read text replace selected region write file	src/mindroom/custom_tools/coding.py:607; src/mindroom/custom_tools/coding.py:625
MindRoomFileTools.read_file	method	lines 178-194	duplicate-found	read_text max length max lines base_dir restriction	src/mindroom/custom_tools/coding.py:531; src/mindroom/custom_tools/coding.py:585; src/mindroom/api/skills.py:63
MindRoomFileTools.delete_file	method	lines 196-210	related-only	unlink rmdir after containment check	src/mindroom/api/skills.py:133; src/mindroom/workspaces.py:215
MindRoomFileTools.list_files	method	lines 212-226	duplicate-found	iterdir format relative paths list directory	src/mindroom/custom_tools/coding.py:464; src/mindroom/api/knowledge.py:88
MindRoomFileTools.search_files	method	lines 228-264	duplicate-found	glob pattern search format relative paths restrict base dir	src/mindroom/custom_tools/coding.py:489; src/mindroom/custom_tools/coding.py:743
file_tools	function	lines 393-395	not-a-behavior-symbol	tool factory returns class registration wrapper	src/mindroom/tools/coding.py:52; src/mindroom/tools/python.py:116
```

Findings:

1. Base-dir path restriction and blocked-path messaging are duplicated between the generic file tool and coding tool.
`src/mindroom/tools/file.py:23` formats the file-tool blocked-path error, `src/mindroom/tools/file.py:39` checks whether a resolved path is under `base_dir`, and `MindRoomFileTools.check_escape` delegates path checks through Agno with the MindRoom `restrict_to_base_dir` flag.
`src/mindroom/custom_tools/coding.py:342` builds nearly the same user-facing "outside base_dir" message, and `src/mindroom/custom_tools/coding.py:350` independently resolves relative paths, optionally allows escapes, and rejects paths outside `base_dir`.
The duplicated behavior is active because both registered tools expose `base_dir` and `restrict_to_base_dir` config fields.
Differences to preserve: `file.py` returns `(safe, path)` and tool-specific error strings, while `coding.py` raises `ValueError` and includes the resolved path in the message.

2. Relative path formatting is duplicated for file listing/search output.
`src/mindroom/tools/file.py:31` returns `path.relative_to(base_dir)` when possible and falls back to the absolute path outside the base dir.
`src/mindroom/custom_tools/coding.py:510` and `src/mindroom/custom_tools/coding.py:903` repeat the same relative-then-absolute behavior when formatting find/grep paths.
`src/mindroom/api/knowledge.py:106` is related but not a duplicate because knowledge files must stay inside the knowledge root and do not need the absolute fallback.
Differences to preserve: `file.py` emits JSON, while `coding.py` emits newline-oriented agent output.

3. File read/write/list/glob operations overlap between the generic file tool and coding tool.
`MindRoomFileTools.save_file` at `src/mindroom/tools/file.py:116` and `CodingTools.write_file` at `src/mindroom/custom_tools/coding.py:649` both resolve a path, create parent directories, write UTF-8 text, and return a success/error string.
`MindRoomFileTools.read_file` at `src/mindroom/tools/file.py:178` and `_resolve_and_read`/`CodingTools.read_file` at `src/mindroom/custom_tools/coding.py:531` and `src/mindroom/custom_tools/coding.py:585` both combine path restriction, existence/type checks or max-size checks, `read_text`, and formatted error returns.
`MindRoomFileTools.list_files` at `src/mindroom/tools/file.py:212` and `_list_directory` at `src/mindroom/custom_tools/coding.py:464` both iterate directory contents and format a tool response.
`MindRoomFileTools.search_files` at `src/mindroom/tools/file.py:228` and `_find_files_in`/`CodingTools.find_files` at `src/mindroom/custom_tools/coding.py:489` and `src/mindroom/custom_tools/coding.py:743` both perform glob-based discovery and relative path formatting.
Differences to preserve: the coding tool adds line numbers, pagination, limits, hidden/gitignored filtering, and text-oriented output; the file tool preserves Agno-compatible JSON shapes, `overwrite`, `max_file_length`, `max_file_lines`, and `expose_base_directory`.

4. `_split_search_pattern` is related to coding-tool search-root normalization but is not a direct duplicate.
`src/mindroom/tools/file.py:48` splits a possibly absolute glob pattern into a concrete root and remaining glob expression before the first glob component.
`src/mindroom/custom_tools/coding.py:703` and `src/mindroom/custom_tools/coding.py:743` normalize unrestricted searches in the same broad problem space, but coding search also validates glob patterns, filters ignored paths, and has separate grep/find behavior.
No shared helper is clearly justified unless both tools are intentionally aligned on one absolute-glob policy.

Proposed generalization:

1. Add a small focused helper module such as `src/mindroom/tool_system/path_tools.py` for base-dir path operations used by tool implementations.
2. Move only shared pure helpers first: `resolve_tool_path(base_dir, path, restrict_to_base_dir)`, `format_tool_path(path, base_dir)`, and `blocked_base_dir_message(...)`.
3. Update `file.py` and `custom_tools/coding.py` to call those helpers while preserving each tool's return strings and output format.
4. Defer unifying read/write/list/search bodies until there is a clear product decision to keep both tools long term; their output contracts and coding-specific filters are meaningfully different.
5. Cover the helper with tests for relative paths, absolute paths, symlink/`..` escapes, unrestricted mode, and fallback formatting outside `base_dir`.

Risk/tests:

The main risk is changing user-visible error strings or allowing/blocking paths differently for either tool.
Tests should exercise both `MindRoomFileTools` and `CodingTools` with the same temporary directory cases: allowed relative path, parent traversal escape, absolute path inside base, absolute path outside base with restriction on/off, symlink escape, and relative output formatting.
For `search_files`, add cases for absolute glob patterns and glob components before/after static path segments because `_split_search_pattern` has behavior not currently shared with the coding tool.
