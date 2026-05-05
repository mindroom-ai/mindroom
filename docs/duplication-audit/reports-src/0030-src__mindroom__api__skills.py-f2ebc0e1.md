## Summary

The top duplication candidate is the repeated conversion from `tool_system.skills._SkillListing` to API response fields in `list_skills` and `get_skill`.
There is also related-but-not-identical atomic text-file replacement in `update_skill` and config source writes.
The create/update/delete endpoints otherwise follow common API CRUD shape, but the behavior is domain-specific enough that no broad refactor is recommended.

## Coverage TSV

```coverage-tsv
qualname	kind	lines	status	search_terms	candidates_checked
SkillSummary	class	lines 20-26	related-only	"BaseModel summary response name description origin can_edit"	src/mindroom/api/tools.py:44; src/mindroom/api/workers.py:24; src/mindroom/api/schedules.py:39
SkillDetail	class	lines 29-32	related-only	"detail response extends summary content"	src/mindroom/api/workers.py:42; src/mindroom/api/schedules.py:59
CreateSkillRequest	class	lines 35-39	related-only	"CreateSkillRequest create request name description BaseModel"	src/mindroom/api/main.py:619; src/mindroom/custom_tools/config_manager.py:588; src/mindroom/api/schedules.py:66
SkillUpdateRequest	class	lines 42-45	related-only	"Update request content BaseModel raw source update"	src/mindroom/api/main.py:105; src/mindroom/api/main.py:541; src/mindroom/api/schedules.py:66
list_skills	async_function	lines 49-60	duplicate-found	"list skills SkillSummary listing name description origin can_edit"	src/mindroom/api/skills.py:75; src/mindroom/tool_system/skills.py:274; src/mindroom/api/tools.py:44
get_skill	async_function	lines 64-81	duplicate-found	"resolve skill listing read_text SkillDetail can_edit"	src/mindroom/api/skills.py:52; src/mindroom/api/config_lifecycle.py:767; src/mindroom/api/main.py:528
update_skill	async_function	lines 85-101	duplicate-found	"safe_replace tmp_path write_text update content read-only"	src/mindroom/api/config_lifecycle.py:209; src/mindroom/api/config_lifecycle.py:191; src/mindroom/custom_tools/subagents.py:159
create_skill	async_function	lines 105-130	related-only	"validate name strip regex already exists create yaml frontmatter description or name"	src/mindroom/tool_system/skills.py:403; src/mindroom/tool_system/skills.py:419; src/mindroom/custom_tools/config_manager.py:588; src/mindroom/api/main.py:619; src/mindroom/api/schedules.py:199
delete_skill	async_function	lines 134-149	related-only	"resolve listing read-only get_user_skills_dir rmtree delete success"	src/mindroom/api/main.py:643; src/mindroom/api/schedules.py:314; src/mindroom/api/matrix_operations.py:213
```

## Findings

### 1. Skill listing-to-response mapping is repeated inside the same module

`list_skills` builds `SkillSummary` from listing fields at `src/mindroom/api/skills.py:52`.
`get_skill` repeats the same field mapping into `SkillDetail` at `src/mindroom/api/skills.py:75`, with only `content` added.
Both call `skill_can_edit(listing.path)` and copy `name`, `description`, and `origin` from the same `_SkillListing` object produced by `src/mindroom/tool_system/skills.py:274`.

This is real duplication because both endpoints define the same API-facing projection of a skill listing.
The only difference to preserve is that detail responses include file content.

### 2. Atomic UTF-8 text replacement overlaps with config source persistence

`update_skill` writes a sibling `.tmp` file and calls `safe_replace` at `src/mindroom/api/skills.py:94`.
`src/mindroom/api/config_lifecycle.py:209` performs the same operation for raw config source, and `src/mindroom/api/config_lifecycle.py:191` does the YAML variant with the same temporary-file replacement step.

This is duplicated behavior at the file-write boundary: construct temp path, write UTF-8 text, atomically replace target.
The differences to preserve are API-specific error translation in `update_skill` and YAML serialization in `_save_config_to_file`.

### 3. Create-skill metadata normalization is related to skill-system frontmatter parsing, but not duplicate enough to extract

`create_skill` trims `payload.name`, validates a dashboard-specific slug regex, defaults empty description to the name, and writes YAML frontmatter at `src/mindroom/api/skills.py:107`.
`src/mindroom/tool_system/skills.py:403` normalizes parsed skill identity, and `src/mindroom/tool_system/skills.py:419` resolves name and description from `SKILL.md` frontmatter.

These are related because both define skill identity semantics, especially "description defaults to name."
They are not the same behavior: API creation also enforces a stricter filesystem-safe slug and duplicate-name checks before writing a new skill.

## Proposed Generalization

1. Add a private `_skill_summary_from_listing(listing: _SkillListing) -> SkillSummary` helper in `src/mindroom/api/skills.py`.
2. Have `get_skill` build `SkillDetail` from that projection plus `content`, or add `_skill_detail_from_listing(listing, content)`.
3. Consider a small existing-utility-level helper for atomic UTF-8 text replacement only if another non-config endpoint starts writing user-editable text files with the same pattern.
4. Do not extract the create-skill name validation or frontmatter generation yet; current behavior has enough endpoint-specific constraints to stay local.

## Risk/tests

The listing response helper would be very low risk if kept private to `api/skills.py`.
Tests should cover list/detail `can_edit` values, detail content, duplicate-name creation, invalid skill names, read-only update/delete, and update file replacement.
The atomic write helper would need tests that preserve existing `OSError` to `HTTPException(500)` translation in the skills endpoint and config write behavior in `config_lifecycle`.
