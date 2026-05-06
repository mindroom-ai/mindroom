## Summary

Top duplication candidates:

1. Explicit room/root-space avatar synchronization in `src/mindroom/avatar_generation.py` overlaps with automatic room/root-space avatar application in `src/mindroom/matrix/rooms.py` and the shared low-level checks in `src/mindroom/matrix/avatar.py`.
2. Managed avatar target enumeration repeats the repository's common "agents + router + teams" entity traversal pattern from entity/user helpers, with avatar-specific additions for rooms and the root space.
3. Avatar path existence checks are thin wrappers around `constants.workspace_avatar_path()` and `constants.resolve_avatar_path()`, so the overlap is related but mostly intentional.

```coverage-tsv
qualname	kind	lines	status	search_terms	candidates_checked
AvatarTeamMember	class	lines 121-125	related-only	team member role dataclass team_config.agents roles	`src/mindroom/agent_descriptions.py:26`, `src/mindroom/matrix/users.py:816`, `src/mindroom/entity_resolution.py:31`
AvatarTarget	class	lines 129-135	related-only	avatar target entity_type entity_name role agents teams rooms spaces	`src/mindroom/entity_resolution.py:48`, `src/mindroom/matrix/users.py:768`, `src/mindroom/agent_descriptions.py:17`
AvatarPromptSettings	class	lines 139-146	related-only	AvatarPromptsConfig prompt style overrides character_style room_style	`src/mindroom/config/main.py:176`
get_console	function	lines 150-152	related-only	rich Console shared console cli config	`src/mindroom/cli/config.py:50`, `src/mindroom/cli/doctor.py:23`
load_validated_config	function	lines 155-157	related-only	load_config tolerate_plugin_load_errors wrapper	`src/mindroom/config/main.py:1750`, `src/mindroom/orchestrator.py:931`, `src/mindroom/orchestrator.py:1310`
get_avatar_path	function	lines 160-168	related-only	workspace_avatar_path mkdir parents avatar path	`src/mindroom/constants.py:913`, `src/mindroom/constants.py:894`
_managed_room_avatar_keys	function	lines 171-173	related-only	get_all_configured_rooms startswith aliases room keys	`src/mindroom/config/main.py:1582`, `src/mindroom/matrix/rooms.py:416`, `src/mindroom/api/schedules.py:97`
_managed_avatar_targets	function	lines 176-184	duplicate-found	agents router teams configured rooms matrix_space root_space targets	`src/mindroom/entity_resolution.py:48`, `src/mindroom/matrix/users.py:768`, `src/mindroom/matrix/invited_rooms_store.py:79`, `src/mindroom/orchestrator.py:725`
_missing_avatar_targets	function	lines 187-196	related-only	resolve_avatar_path exists managed targets missing avatars	`src/mindroom/bot.py:890`, `src/mindroom/matrix/rooms.py:59`, `src/mindroom/constants.py:922`
has_missing_managed_avatars	function	lines 199-201	related-only	bool missing avatar targets startup check	none
resolve_avatar_prompt_settings	function	lines 204-223	related-only	AvatarPromptsConfig defaults fallback prompts	`src/mindroom/config/main.py:176`
generate_prompt	async_function	lines 226-280	none-found	google genai generate_content avatar prompt system prompt visual elements panel	none
extract_image_bytes	function	lines 283-288	related-only	response.parts inline_data data image bytes	`src/mindroom/mcp/results.py:67`, `src/mindroom/matrix/image_handler.py:20`, `src/mindroom/attachment_media.py:22`
generate_avatar	async_function	lines 291-332	none-found	Gemini image response_modalities ImageConfig write_bytes avatar generation	none
_build_router_user	function	lines 335-350	related-only	router Matrix account AgentMatrixUser MatrixID server name	`src/mindroom/matrix/users.py:790`, `src/mindroom/entity_resolution.py:42`, `src/mindroom/entity_resolution.py:48`
_sync_avatar_target	async_function	lines 353-370	duplicate-found	room_has_avatar set_room_avatar_from_file force skip success failure	`src/mindroom/matrix/avatar.py:114`, `src/mindroom/matrix/rooms.py:45`
_sync_configured_room_avatars	async_function	lines 373-409	duplicate-found	configured rooms resolve avatar path get_room_id set room avatar	`src/mindroom/matrix/rooms.py:381`, `src/mindroom/matrix/rooms.py:416`, `src/mindroom/config/main.py:1582`
_sync_root_space_avatar	async_function	lines 412-438	duplicate-found	matrix_space enabled space_room_id root_space avatar set	`src/mindroom/matrix/rooms.py:524`, `src/mindroom/matrix/rooms.py:42`
set_room_avatars_in_matrix	async_function	lines 441-495	related-only	router login matrix state sync room avatars close client	`src/mindroom/bot.py:1105`, `src/mindroom/matrix/users.py:734`, `src/mindroom/orchestrator.py:1476`
_build_avatar_generation_targets	function	lines 498-562	related-only	agents teams router rooms root_space roles team members target build	`src/mindroom/agent_descriptions.py:17`, `src/mindroom/topic_generator.py:48`, `src/mindroom/entity_resolution.py:48`
_print_avatar_generation_plan	function	lines 565-573	none-found	count missing agents teams rooms spaces Rich print	none
_remaining_missing_avatar_targets	function	lines 576-585	related-only	workspace_avatar_path exists remaining missing after generation	`src/mindroom/constants.py:913`, `src/mindroom/avatar_generation.py:187`
_generate_missing_avatars	async_function	lines 588-660	related-only	get secret env google client gather return_exceptions progress failures	`src/mindroom/credentials_sync.py:1`, `src/mindroom/background_tasks.py:92`, `src/mindroom/knowledge/manager.py:1632`
run_avatar_generation	async_function	lines 663-670	related-only	load config force all targets missing targets raise error	none
```

## Findings

### 1. Room/root-space avatar sync duplicates automatic room avatar application

`src/mindroom/avatar_generation.py:353` implements `_sync_avatar_target()` by checking `room_has_avatar()`, calling `set_room_avatar_from_file()`, and returning a tri-state result for set/skipped/failed.
`src/mindroom/matrix/avatar.py:114` already provides `check_and_set_avatar()`, which performs the same "skip if room already has an avatar, otherwise set it" operation for rooms, although it returns `True` for both already-set and newly-set outcomes.
`src/mindroom/matrix/rooms.py:45` wraps the same avatar-file existence and `check_and_set_avatar()` flow for room creation, logging success/failure and treating failures as cosmetic.

The behavior is duplicated at the Matrix operation level: both paths resolve a managed asset, skip absent/already-set avatars, and upload plus write `m.room.avatar` state through the shared low-level helper.
The differences to preserve are important: the CLI sync path needs `force=True` to replace existing avatars and needs counts plus failed labels, while room creation deliberately never aborts room creation on avatar failure.

### 2. Configured room and root-space avatar traversal repeats room creation reconciliation

`src/mindroom/avatar_generation.py:373` iterates managed configured room keys, resolves each room avatar, finds the Matrix room ID, and calls the sync helper.
`src/mindroom/matrix/rooms.py:381` applies the same category/name convention immediately after creating a managed room.
`src/mindroom/avatar_generation.py:412` and `src/mindroom/matrix/rooms.py:524` separately encode the root-space avatar category/name pair: `spaces/root_space`.

This is not a literal duplicate loop, because one path reconciles existing rooms on demand and the other runs during room creation.
It is still shared behavior: both rely on the same managed asset namespace and same root-space avatar key.
The constant duplication is small but active: `ROOT_SPACE_AVATAR_NAME` in `avatar_generation.py:43` and `_ROOT_SPACE_AVATAR_KEY` in `matrix/rooms.py:42` must stay in sync.

### 3. Managed entity enumeration overlaps with existing entity traversal helpers

`src/mindroom/avatar_generation.py:176` builds managed avatar targets as configured agents, router, teams, configured rooms, and optional root space.
Other modules repeatedly enumerate configured agents plus router plus teams for their own purposes: `src/mindroom/entity_resolution.py:48`, `src/mindroom/matrix/users.py:768`, `src/mindroom/matrix/invited_rooms_store.py:79`, and `src/mindroom/orchestrator.py:725`.

The overlap is real for the bot entities, but avatar generation has extra room and space targets.
The existing helpers return different projections such as Matrix IDs, user accounts, or startup entity names, so a broad shared abstraction would likely add indirection without much payoff.

## Proposed Generalization

No broad refactor recommended.

A minimal future cleanup would be:

1. Move the root-space avatar key to a shared constant near avatar path helpers, or export it from one Matrix/avatar constants location.
2. Add a small room-avatar reconciliation helper that accepts `force: bool` and returns an explicit enum such as `set`, `skipped`, or `failed`.
3. Use that helper from `_sync_avatar_target()` and `_set_room_avatar_if_available()`, keeping CLI counting and room-creation logging at their respective call sites.
4. Leave `AvatarTarget` construction local to `avatar_generation.py`; it is specific to Gemini prompt generation and does not justify a shared entity abstraction.

## Risk/tests

The main behavior risk is conflating "already had avatar" with "successfully set avatar".
`check_and_set_avatar()` currently returns `True` for both, while the CLI sync flow distinguishes skip counts from set counts.
Any deduplication should preserve the CLI `force=True` replacement behavior and the room-creation policy that avatar failures do not abort room creation.

Tests to update or add would cover:

- Existing-room avatar sync with missing file, already-set avatar, newly-set avatar, failed upload/state write, and `force=True`.
- Root-space avatar sync when Matrix spaces are disabled, when no state `space_room_id` exists, and when the avatar file is absent.
- Room creation still continues if avatar upload/state write fails.
