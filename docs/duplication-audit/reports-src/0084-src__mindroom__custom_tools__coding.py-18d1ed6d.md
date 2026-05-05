# Duplication Audit: `src/mindroom/custom_tools/coding.py`

## Summary

Top duplication candidates:

1. Path restriction, blocked-path messaging, file read/write, directory listing, and glob search overlap with the legacy `src/mindroom/tools/file.py` wrapper.
2. Glob-root splitting in `_normalize_search_root` closely repeats `src/mindroom/tools/file.py`'s `_split_search_pattern`.
3. Hidden-path and root-containment filtering is related to `src/mindroom/knowledge/manager.py` and `src/mindroom/workspaces.py`, but those modules preserve stricter semantic-indexing or symlink contracts, so they are related-only rather than direct extraction candidates.

```coverage-tsv
qualname	kind	lines	status	search_terms	candidates_checked
_TruncateResult	class	lines 38-44	related-only	truncate result dataclass was_truncated total_lines shown_lines	src/mindroom/history/compaction.py:1366; src/mindroom/tool_system/tool_calls.py:29
_truncate_head	function	lines 47-69	related-only	truncate max bytes max lines head splitlines	src/mindroom/history/compaction.py:1366; src/mindroom/agents.py:282; src/mindroom/tool_system/tool_calls.py:29
_truncate_to_max_bytes	function	lines 72-78	none-found	truncate encoded byte length utf-8 errors ignore	none
_normalize_for_fuzzy	function	lines 122-135	none-found	unicodedata NFC smart quotes dashes rstrip fuzzy	none
_MatchResult	class	lines 139-145	not-a-behavior-symbol	match result dataclass start end matched_text was_fuzzy	none
_NormalizedLineMap	class	lines 149-153	not-a-behavior-symbol	normalized line map norm_to_orig norm_len	none
_split_line_body_ending	function	lines 156-162	none-found	split CRLF line ending preserving	none
_normalize_prefix_map	function	lines 165-174	none-found	normalized offset map NFC prefix map	none
_normalize_prefix_map_linear	function	lines 177-189	none-found	linear normalized offset map translate chars	none
_normalize_prefix_map_slow	function	lines 192-210	none-found	slow prefix normalized offset map non NFC	none
_build_normalized_line_maps	function	lines 213-234	none-found	per-line normalized original offset maps trailing whitespace	none
_find_all_matches	function	lines 237-288	none-found	fuzzy find all matches old_text normalized replacement	none
_cumulative_offsets	function	lines 291-296	related-only	cumulative line offsets splitlines offsets	src/mindroom/response_runner.py:136
_norm_to_orig_offset	function	lines 299-322	none-found	map normalized offset original offset bisect	none
_make_diff	function	lines 325-339	none-found	difflib unified_diff before after	none
_outside_base_dir_message	function	lines 342-347	duplicate-found	outside base_dir message restrict_to_base_dir	src/mindroom/tools/file.py:23
_resolve_path	function	lines 350-365	duplicate-found	resolve path base_dir restrict traversal relative_to	src/mindroom/tools/file.py:39; src/mindroom/tools/file.py:108; src/mindroom/workspaces.py:37; src/mindroom/api/sandbox_exec.py:347
_is_git_repo	function	lines 368-371	related-only	git repo check .git parents	src/mindroom/knowledge/manager.py:678
_gitignored_paths	function	lines 374-409	related-only	git check-ignore stdin ignored paths	src/mindroom/knowledge/manager.py:678
_format_read_output	function	lines 412-438	duplicate-found	read file line numbers offset limit pagination	src/mindroom/tools/file.py:135; src/mindroom/tools/file.py:178
_apply_byte_limit	function	lines 441-452	related-only	byte limit read output truncate	src/mindroom/history/compaction.py:1366
_pagination_hint	function	lines 455-461	none-found	pagination hint showing lines offset continue	none
_list_directory	function	lines 464-486	duplicate-found	list directory sorted entries slash suffix limit	src/mindroom/tools/file.py:212
_find_files_in	function	lines 489-528	duplicate-found	glob files format results limit filter hidden gitignored	src/mindroom/tools/file.py:228; src/mindroom/knowledge/manager.py:632
_resolve_and_read	function	lines 531-546	duplicate-found	resolve path read_text file not found not file	src/mindroom/tools/file.py:178; src/mindroom/tools/file.py:135
_normalize_search_root	function	lines 549-560	duplicate-found	resolve concrete search root ahead first glob component	src/mindroom/tools/file.py:48
CodingTools	class	lines 563-810	duplicate-found	Toolkit file operations read write grep find ls	src/mindroom/tools/file.py:69
CodingTools.__init__	method	lines 570-583	related-only	Toolkit init registers tools base_dir restrict_to_base_dir	src/mindroom/tools/file.py:72; src/mindroom/custom_tools/browser.py:251
CodingTools.read_file	method	lines 585-605	duplicate-found	read file with path restriction and line pagination	src/mindroom/tools/file.py:178; src/mindroom/tools/file.py:135
CodingTools.edit_file	method	lines 607-647	related-only	replace text in file write diff fuzzy matching	src/mindroom/tools/file.py:150
CodingTools.write_file	method	lines 649-673	duplicate-found	write file mkdir parents byte line count path restriction	src/mindroom/tools/file.py:116
CodingTools.grep	method	lines 675-741	none-found	ripgrep backed grep Python fallback context limit	none
CodingTools.find_files	method	lines 743-784	duplicate-found	find files glob pattern base_dir restriction	src/mindroom/tools/file.py:228
CodingTools.ls	method	lines 786-810	duplicate-found	list directory contents base_dir restriction	src/mindroom/tools/file.py:212
_truncate_line	function	lines 816-820	related-only	truncate single line max chars truncated marker	src/mindroom/history/compaction.py:1366; src/mindroom/tool_system/tool_calls.py:29
_RgEvent	class	lines 824-830	not-a-behavior-symbol	ripgrep event dataclass event_type path line line_number	none
_parse_rg_event	function	lines 833-857	none-found	parse ripgrep json event match context	none
_run_ripgrep	function	lines 860-897	none-found	shutil.which rg --json subprocess grep	none
_relativize_path	function	lines 900-905	duplicate-found	relative path for output fallback absolute	src/mindroom/tools/file.py:31
_format_rg_line	function	lines 908-912	none-found	format rg event marker path line text	none
_append_trailing_context	function	lines 915-932	none-found	append after context ripgrep limited output	none
_format_rg_output	function	lines 935-978	none-found	parse ripgrep json output format limited context	none
_validate_glob_pattern	function	lines 981-987	related-only	reject absolute glob patterns	src/mindroom/tools/file.py:48
_validate_grep_request	function	lines 990-1004	related-only	path exists limit context glob validation	src/mindroom/tools/file.py:231
_grep_file	function	lines 1007-1049	none-found	Python regex grep file context emitted lines limit	none
_filter_hidden_and_ignored	function	lines 1052-1077	related-only	filter hidden dotfiles root escape gitignored	src/mindroom/knowledge/manager.py:555; src/mindroom/knowledge/manager.py:611; src/mindroom/workspaces.py:37
_collect_grep_files	function	lines 1080-1106	related-only	collect glob files recursive hidden ignored filter	src/mindroom/tools/file.py:228; src/mindroom/knowledge/manager.py:632
_python_grep_fallback	function	lines 1109-1149	none-found	Python grep fallback regex literal ignore case	none
```

## Findings

### 1. Coding file operations duplicate the legacy file tool's path and IO flow

`src/mindroom/custom_tools/coding.py` implements base-dir restriction, escaped-path error text, read/write, list, and file discovery in `_outside_base_dir_message`, `_resolve_path`, `_resolve_and_read`, `_list_directory`, `_find_files_in`, and `CodingTools`.
`src/mindroom/tools/file.py` already has the same behavioral family in `_blocked_path_message` at line 23, `_is_within_base_dir` at line 39, `MindRoomFileTools.check_escape` at line 108, `save_file` at line 116, `read_file_chunk` at line 135, `read_file` at line 178, `list_files` at line 212, and `search_files` at line 228.

The duplication is functional rather than literal: both toolkits resolve user-supplied paths against a base directory, block traversal when `restrict_to_base_dir` is enabled, read and write UTF-8 text, list directory contents, and glob for files.
Differences to preserve: `CodingTools.read_file` returns numbered paginated output while `MindRoomFileTools.read_file` returns raw contents and directs large files to chunked reads; `CodingTools.find_files` filters hidden and gitignored files and has a limit; `MindRoomFileTools.search_files` returns JSON and may expose the base directory.

### 2. Glob-root normalization is nearly duplicated

`_normalize_search_root` in `src/mindroom/custom_tools/coding.py` lines 549-560 and `_split_search_pattern` in `src/mindroom/tools/file.py` lines 48-66 both split a path-like glob into static path components before the first magic component and a residual glob pattern.
Both also promote a non-glob terminal path component into the glob pattern when no glob magic exists.

Differences to preserve: `_split_search_pattern` handles absolute patterns explicitly by using the path anchor as the search root, while `_normalize_search_root` joins against a caller-provided `search_path` and is used only when base-dir restriction is disabled.

### 3. Root containment and hidden-file filtering are repeated but have different safety contracts

`_filter_hidden_and_ignored` in `src/mindroom/custom_tools/coding.py` lines 1052-1077, `include_knowledge_file` in `src/mindroom/knowledge/manager.py` lines 611-629, and `resolve_relative_path_within_root` in `src/mindroom/workspaces.py` lines 37-58 all protect root boundaries and reject unsafe paths.
The coding tool also filters dotfiles and gitignored paths; the knowledge manager filters configured semantic extensions and rejects symlink traversal more explicitly.

This is related behavior, but not a safe direct extraction candidate unless the shared helper is deliberately narrow.

## Proposed Generalization

1. Extract a small `mindroom.path_safety` helper only for base-dir-relative resolution and blocked-path messaging, with explicit parameters for whether the leaf may be missing and whether symlinks are followed.
2. Extract a small `split_glob_root(base_dir: Path, pattern: str, *, allow_absolute: bool)` helper from the duplicated glob-root splitting logic.
3. Keep `CodingTools` formatting, pagination, fuzzy edit, ripgrep, hidden/gitignored filtering, and `MindRoomFileTools` JSON/raw-output contracts separate.
4. Add focused tests that compare existing path traversal, absolute glob, non-glob path, and base-dir-disabled behavior before replacing either implementation.

No broad refactor recommended.

## Risk/Tests

The main risk is changing agent-visible tool output, especially error strings, JSON shape from `MindRoomFileTools.search_files`, and numbered pagination from `CodingTools.read_file`.
Tests should cover path traversal blocking, `restrict_to_base_dir=false`, absolute and relative glob splitting, hidden file filtering, gitignored file filtering, read pagination, and file-tool JSON output before any extraction.
