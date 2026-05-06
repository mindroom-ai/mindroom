# Duplication Audit: `src/mindroom/credentials.py`

## Summary

Top duplication candidates:

1. Scoped credential load/save/delete routing is duplicated between `src/mindroom/credentials.py` and dashboard target helpers in `src/mindroom/api/credentials.py`.
2. Shared credential grantability and layer merging are repeated in dashboard/API preview paths instead of reusing the same merge helper and scoped resolver semantics.
3. API-key update/read behavior is duplicated at the endpoint layer, but the API preserves dashboard-specific OAuth filtering and response masking, so only the small mutation pattern overlaps.

## Coverage TSV

```coverage-tsv
qualname	kind	lines	status	search_terms	candidates_checked
validate_service_name	function	lines 34-43	related-only	validate_service_name _validated_service service name fullmatch	src/mindroom/api/credentials.py:96
_scoped_credentials_dir_part	function	lines 46-49	none-found	scoped credentials dir sha256 safe_prefix private_oauth	none
CredentialsManager	class	lines 52-224	related-only	CredentialsManager credentials json load save list_services	src/mindroom/api/credentials.py:103; src/mindroom/credentials_sync.py:71
CredentialsManager.__init__	method	lines 55-86	related-only	credentials base_path shared_base_path mkdir parents	src/mindroom/constants.py:839; src/mindroom/workers/backends/local.py:164
CredentialsManager.storage_root	method	lines 89-91	none-found	storage_root base_path parent credentials manager	none
CredentialsManager.for_worker	method	lines 93-103	related-only	for_worker worker_root_path credentials .shared_credentials	src/mindroom/api/credentials.py:529; src/mindroom/workers/backends/kubernetes_resources.py:795
CredentialsManager.for_primary_runtime_scope	method	lines 105-110	related-only	for_primary_runtime_scope private_oauth requester agent	src/mindroom/api/credentials.py:607
CredentialsManager.shared_manager	method	lines 112-119	related-only	shared_manager shared_base_path base_path	src/mindroom/api/tools.py:183; src/mindroom/api/credentials.py:912
CredentialsManager.get_credentials_path	method	lines 121-132	none-found	_credentials.json get_credentials_path validate_service_name	none
CredentialsManager.load_credentials	method	lines 134-157	related-only	load_credentials json.load credentials file	src/mindroom/credentials_sync.py:72; src/mindroom/api/credentials.py:540
CredentialsManager.save_credentials	method	lines 159-169	related-only	save_credentials json.dump credentials	src/mindroom/credentials_sync.py:80; src/mindroom/api/credentials.py:592
CredentialsManager.delete_credentials	method	lines 171-180	related-only	delete_credentials unlink credentials	src/mindroom/api/credentials.py:621; src/mindroom/api/homeassistant_integration.py:365
CredentialsManager.list_services	method	lines 182-195	related-only	list_services *_credentials.json credential_service_policy	src/mindroom/api/credentials.py:897
CredentialsManager.get_api_key	method	lines 197-211	duplicate-found	get_api_key api_key credentials.get key_name	src/mindroom/api/credentials.py:1042
CredentialsManager.set_api_key	method	lines 213-224	duplicate-found	set_api_key credentials key_name _source ui save	src/mindroom/api/credentials.py:1014
credentials_base_path	function	lines 227-229	related-only	credentials_base_path storage_root credentials_dir	src/mindroom/constants.py:839
_default_shared_credentials_base_path	function	lines 232-233	not-a-behavior-symbol	default shared credentials base path identity	none
_runtime_shared_credentials_base_path	function	lines 236-240	none-found	MINDROOM_SHARED_CREDENTIALS_PATH env shared credentials path	none
_runtime_dedicated_worker_key	function	lines 243-246	none-found	MINDROOM_SANDBOX_DEDICATED_WORKER_KEY env strip	none
_runtime_dedicated_worker_root	function	lines 249-253	none-found	MINDROOM_SANDBOX_DEDICATED_WORKER_ROOT env resolve	none
get_credentials_manager	function	lines 261-282	related-only	global credentials manager signature lazy singleton	src/mindroom/credentials.py:285
get_runtime_credentials_manager	function	lines 285-306	related-only	global credentials manager runtime signature lazy singleton	src/mindroom/credentials.py:261
shared_credentials_manager	function	lines 309-313	related-only	shared_credentials_manager shared_base_path base_path shared_manager	src/mindroom/api/tools.py:183; src/mindroom/api/credentials.py:912
get_runtime_shared_credentials_manager	function	lines 316-318	related-only	get_runtime_shared_credentials_manager runtime shared credentials	src/mindroom/credentials_sync.py:71; src/mindroom/memory/config.py:51
_resolve_worker_credentials_manager	function	lines 321-348	related-only	resolve worker credentials manager worker_key worker_root_path	src/mindroom/api/credentials.py:523; src/mindroom/api/credentials.py:582
_merge_unscoped_credentials	function	lines 351-360	related-only	merge unscoped shared local credentials	src/mindroom/api/credentials.py:554
_merge_credential_layers	function	lines 363-372	duplicate-found	merge shared worker credentials dict update	src/mindroom/api/credentials.py:554
load_worker_grantable_shared_credentials	function	lines 375-391	related-only	load_worker_grantable_shared_credentials allowed_services worker_grantable	src/mindroom/api/tools.py:171; src/mindroom/api/credentials.py:554
list_worker_grantable_shared_services	function	lines 394-409	related-only	list grantable shared services load_worker_grantable_shared_credentials	src/mindroom/api/credentials.py:897
merge_scoped_credentials	function	lines 412-421	duplicate-found	merge_scoped_credentials shared worker scoped overrides	src/mindroom/api/credentials.py:554
sync_shared_credentials_to_worker	function	lines 424-466	none-found	sync shared credentials to worker copied_services mirrored_services	none
_primary_runtime_scoped_credentials_manager	function	lines 469-483	related-only	primary runtime scoped credentials requester identity agent_name	src/mindroom/api/credentials.py:607
load_scoped_credentials	function	lines 486-541	duplicate-found	load_scoped_credentials worker_target allowed_shared_services target manager shared worker merge	src/mindroom/api/credentials.py:540; src/mindroom/api/tools.py:263
save_scoped_credentials	function	lines 544-576	duplicate-found	save_scoped_credentials worker target primary runtime global target_manager	src/mindroom/api/credentials.py:592; src/mindroom/api/credentials.py:861
delete_scoped_credentials	function	lines 579-610	duplicate-found	delete_scoped_credentials worker target primary runtime global target_manager	src/mindroom/api/credentials.py:621; src/mindroom/api/credentials.py:866
```

## Findings

### 1. Dashboard credential target helpers duplicate scoped credential routing

`src/mindroom/credentials.py:486` resolves reads across unscoped, shared, primary-runtime scoped, worker-scoped, local-shared, and allowlisted grantable shared layers.
`src/mindroom/api/credentials.py:540` implements a parallel read path for `RequestCredentialsTarget`: primary-runtime global credentials read from `base_manager`, unscoped reads from `target_manager`, primary-runtime policy cases delegate to `load_scoped_credentials`, and the remaining worker path manually loads allowlisted shared credentials, loads worker credentials, and overlays worker values.

The manual merge in `src/mindroom/api/credentials.py:554-566` duplicates `_merge_credential_layers` and `merge_scoped_credentials` behavior from `src/mindroom/credentials.py:363-421`.
The main difference to preserve is dashboard-specific target resolution: `RequestCredentialsTarget.target_manager` is pre-resolved from request/config state, while `load_scoped_credentials` derives worker managers from a `ResolvedWorkerTarget`.

`src/mindroom/api/credentials.py:592` and `src/mindroom/api/credentials.py:621` similarly duplicate the high-level routing branches in `save_scoped_credentials` and `delete_scoped_credentials`.
They add a dashboard-only primary-runtime-global branch before choosing either `target_manager` or the scoped helper.

### 2. Shared grantable credential preview repeats a subset of scoped loading semantics

`src/mindroom/api/tools.py:171` implements dashboard preview credential loading by choosing `credentials_manager.shared_manager().load_credentials()` when no allowlist is supplied and `load_worker_grantable_shared_credentials()` when one is supplied.
This is intentionally narrower than `load_scoped_credentials`, because non-authoritative dashboard previews must not inspect requester-owned private credential state.
Still, it repeats the same shared-manager and allowed-services decision used inside `src/mindroom/credentials.py:486-541`.

The difference to preserve is security-related: authoritative tool status calls use `load_scoped_credentials` at `src/mindroom/api/tools.py:267`, while non-authoritative draft previews only inspect shared/grantable credentials at `src/mindroom/api/tools.py:274`.

### 3. API-key update/read pattern overlaps with `CredentialsManager` convenience methods

`CredentialsManager.get_api_key` at `src/mindroom/credentials.py:197` and `CredentialsManager.set_api_key` at `src/mindroom/credentials.py:213` load a service document, read or mutate `key_name`, and save it.
The dashboard endpoints repeat that mechanical pattern at `src/mindroom/api/credentials.py:1033-1037` and `src/mindroom/api/credentials.py:1059-1061`.

This is real duplication at the mutation/read level, but not a direct replacement candidate because the endpoints also validate route/payload service agreement, reject OAuth-managed fields, reject stored OAuth token documents, set `_source="ui"`, mask values, and optionally return the raw key.

## Proposed Generalization

Extract one small credentials-layer helper in `src/mindroom/credentials.py` for "merge shared grantable credentials with an explicit target manager", or expose `_merge_credential_layers` through a public helper such as `merge_credential_layers`.
Then `src/mindroom/api/credentials.py:540` can reuse the same merge behavior while keeping dashboard target resolution local.

For the larger scoped routing duplication, consider a small dataclass in `credentials.py` that represents an already-resolved credential target with `base_manager`, `target_manager`, `worker_scope`, `worker_target`, and `allowed_shared_services`.
Only add this if future edits keep changing both `credentials.py` and `api/credentials.py`; today the duplication is meaningful but intertwined with dashboard policy enough that a broad refactor is not recommended.

No refactor is recommended for `_load_shared_preview_credentials`, because the narrower behavior is deliberate.
No refactor is recommended for dashboard API-key endpoints beyond possibly adding a tiny helper for "set key and source" inside the API module.

## Risk/Tests

The main risk in deduplicating scoped credential routing is changing where OAuth and worker-scoped credentials are stored.
Tests should cover unscoped global credentials, worker override over shared credentials, allowlisted shared credentials, non-allowlisted shared credentials, primary-runtime global credentials, primary-runtime scoped credentials for `user` and `user_agent`, and local-shared credential services.

Dashboard-specific tests should verify `/api/credentials/{service}`, `/api/credentials/{service}/api-key`, delete, copy, and list behavior for unscoped, shared, user, and user-agent execution scopes.
Tool availability tests should separately preserve the non-authoritative preview behavior that only reads shared/grantable credentials.
