## Summary

Top candidate: `src/mindroom/tool_system/skills.py` has skill-specific tree snapshot logic that overlaps with the generic file watcher snapshot flow in `src/mindroom/file_watcher.py`.
The rest of the module is mostly domain-specific skill discovery, OpenClaw metadata eligibility, and Agno loader wrapping.
No broad refactor is recommended from this single-file audit.

## Coverage TSV

```coverage-tsv
qualname	kind	lines	status	search_terms	candidates_checked
_MindroomSkillsLoader	class	lines 52-96	related-only	SkillLoader LocalSkills load skills allowlist	 src/mindroom/agents.py:911; src/mindroom/tool_system/plugins.py:114
_MindroomSkillsLoader.load	method	lines 63-96	related-only	LocalSkills load allowlist eligibility duplicate skill name roots	 src/mindroom/agents.py:911; src/mindroom/tool_system/plugins.py:114
_MindroomSkills	class	lines 99-153	none-found	Skills subclass get_skill_script script execution blocked	none
_MindroomSkills.__init__	method	lines 102-104	none-found	Skills init loaders script_execution_blocked_skill_names	none
_MindroomSkills._load_skills	method	lines 106-126	related-only	loaders load duplicate skill name SkillValidationError	 src/mindroom/tool_system/plugins.py:114
_MindroomSkills._get_skill_script	method	lines 128-150	none-found	get_skill_script execute script_path blocked	none
_MindroomSkills._is_script_execution_blocked	method	lines 152-153	none-found	script execution blocked skill name set membership	none
build_agent_skills	function	lines 156-206	related-only	build_agent_skills workspace_skills_root agent_config.skills	 src/mindroom/agents.py:911
_SkillListing	class	lines 210-216	not-a-behavior-symbol	dataclass SkillListing name description path origin	none
_ResolvedSkillFrontmatter	class	lines 220-225	not-a-behavior-symbol	dataclass ResolvedSkillFrontmatter name description frontmatter	none
set_plugin_skill_roots	function	lines 228-232	related-only	set_plugin_skill_roots plugin roots clear cache	 src/mindroom/tool_system/plugins.py:101; src/mindroom/tool_system/plugins.py:245
get_plugin_skill_roots	function	lines 235-237	related-only	get_plugin_skill_roots plugin roots copy	 src/mindroom/tool_system/plugins.py:229; src/mindroom/tool_system/plugins.py:238
_get_plugin_skill_roots	function	lines 240-242	related-only	get_plugin_skill_roots wrapper	 src/mindroom/tool_system/plugins.py:25
get_user_skills_dir	function	lines 245-247	none-found	user skills dir .mindroom skills	none
_get_bundled_skills_dir	function	lines 250-256	none-found	bundled skills dev dir package dir exists	none
_get_default_skill_roots	function	lines 259-261	related-only	default roots bundled plugin user unique paths	 src/mindroom/tool_system/plugins.py:169
_get_agent_workspace_skill_root	function	lines 264-266	related-only	agent workspace skill root agent_workspace_root_path skills	 src/mindroom/agents.py:1145; src/mindroom/tool_system/worker_routing.py:550
_resolve_configured_skill_roots	function	lines 269-271	related-only	resolve configured roots unique paths defaults	 src/mindroom/tool_system/plugins.py:169
list_skill_listings	function	lines 274-300	related-only	list skills listing origin resolve frontmatter API	 src/mindroom/api/skills.py:48; src/mindroom/api/skills.py:63
resolve_skill_listing	function	lines 303-311	related-only	resolve skill listing normalized name	 src/mindroom/api/skills.py:63; src/mindroom/api/skills.py:84; src/mindroom/api/skills.py:104
skill_can_edit	function	lines 314-323	related-only	path under user root os.access editable	 src/mindroom/api/skills.py:48; src/mindroom/api/skills.py:133
clear_skill_cache	function	lines 326-328	related-only	clear cache invalidation	 src/mindroom/tool_system/plugins.py:207; src/mindroom/workers/runtime.py:56; src/mindroom/matrix/message_content.py:362
get_skill_snapshot	function	lines 331-338	duplicate-found	snapshot rglob stat mtime size roots	 src/mindroom/file_watcher.py:66
_snapshot_skill_files	function	lines 341-353	duplicate-found	rglob SKILL.md stat mtime_ns size snapshot	 src/mindroom/file_watcher.py:66
_iter_skill_dirs	function	lines 356-368	none-found	SKILL.md root child skill dirs hidden dirs sorted	none
_read_skill_frontmatter	function	lines 371-400	related-only	frontmatter regex yaml.safe_load read_text warning	 src/mindroom/config/main.py:960; src/mindroom/config/main.py:1762; src/mindroom/matrix/state.py:174
_normalize_skill_identity	function	lines 403-416	related-only	normalize name description strip fallback	 src/mindroom/api/skills.py:104
_resolve_skill_frontmatter	function	lines 419-444	related-only	read frontmatter normalize identity skill dir default name	 src/mindroom/api/skills.py:104
_load_root_skills	function	lines 447-467	related-only	LocalSkills cache snapshot load fallback cached	 src/mindroom/tool_system/dependencies.py:89; src/mindroom/tool_system/plugins.py:169
_normalize_skill	function	lines 470-485	related-only	normalize skill identity parse metadata mutation	 src/mindroom/tool_system/metadata.py:1
_parse_metadata	function	lines 488-505	related-only	json5 metadata mapping parse warning	 src/mindroom/tools/composio.py:77
_is_skill_eligible	function	lines 508-528	none-found	openclaw os always requires eligibility	none
_normalize_str_list	function	lines 531-538	related-only	normalize str list tuple set filter strings	 src/mindroom/config/main.py:1054; src/mindroom/config/main.py:1065
_matches_current_os	function	lines 541-544	related-only	platform.system os aliases	 src/mindroom/cli/config.py:306
_env_requirements_met	function	lines 547-558	related-only	env vars credential keys requirements	 src/mindroom/credentials_sync.py:72; src/mindroom/model_loading.py:154
_missing_bins	function	lines 561-562	related-only	shutil.which missing binaries	 src/mindroom/frontend_assets.py:57; src/mindroom/cli/local_stack.py:194; src/mindroom/custom_tools/coding.py:871
_any_bins_requirements_met	function	lines 565-566	related-only	shutil.which any binaries	 src/mindroom/frontend_assets.py:57; src/mindroom/custom_tools/browser.py:211
_config_requirements_met	function	lines 569-570	related-only	config path truthy requirements	 src/mindroom/commands/config_commands.py:57
_requirements_met	function	lines 573-604	none-found	openclaw requires env config bins anyBins	none
_config_path_truthy	function	lines 607-614	related-only	dot path mapping traversal truthy	 src/mindroom/commands/config_commands.py:57
_collect_credential_keys	function	lines 617-625	related-only	list_services load_credentials truthy keys	 src/mindroom/api/credentials.py:897; src/mindroom/credentials.py:182
_unique_paths	function	lines 628-637	related-only	unique resolved paths preserve order dict.fromkeys	 src/mindroom/tool_system/plugins.py:187
_root_origin	function	lines 640-647	none-found	root origin bundled user plugin custom	none
```

## Findings

### 1. Skill snapshotting overlaps with generic tree snapshot behavior

`src/mindroom/tool_system/skills.py:331` and `src/mindroom/tool_system/skills.py:341` build deterministic snapshots by walking roots, filtering files, reading stat metadata, ignoring unreadable entries, sorting, and returning immutable comparison data.
`src/mindroom/file_watcher.py:66` performs the same core behavior for arbitrary directory trees: walk with `rglob`, filter relevant files, stat each file with `st_mtime_ns`, ignore stat failures, and return snapshot data for change detection.

The behavior is duplicated at the operation level, not literally identical.
The skill version is narrower: it only includes `SKILL.md`, stores path strings plus `st_mtime_ns` and `st_size`, and sorts list output.
The file watcher version includes all relevant files, stores `Path -> st_mtime_ns`, and excludes caches/temp files.

## Proposed Generalization

No immediate refactor recommended unless another watcher/snapshot path is being touched.
If this duplication grows, introduce a small helper such as `mindroom.file_snapshots.snapshot_files(root, predicate) -> list[FileSnapshot]` and keep skill-specific selection (`path.name == "SKILL.md"`) and payload shape at the call site.
That would avoid coupling skill loading to the file watcher’s ignore policy.

## Risk/tests

Changing skill snapshotting risks stale or over-eager skill cache invalidation.
Tests should cover unchanged snapshots, changed `SKILL.md` content size, changed mtime, unreadable/deleted files, missing roots, and multiple roots with stable ordering.
Existing file watcher tests, if present, would not be enough because skill snapshots intentionally include size and only target `SKILL.md`.
