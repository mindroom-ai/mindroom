## Summary

Top duplication candidate: `_sync_github_private_credentials` repeats the same env-owned credential save policy implemented by `_sync_service_credentials`, with only the hard-coded `github_private` payload and log messages differing.
Related-only duplication exists for Ollama host reads: `get_ollama_host` centralizes `CredentialsManager.load_credentials("ollama")`, but `memory/config.py` repeats the same direct load in three places.
No broad refactor is recommended from this audit because the active duplication is narrow and behavior-sensitive.

## Coverage TSV

```coverage-tsv
qualname	kind	lines	status	search_terms	candidates_checked
_CredentialSeedDeclaration	class	lines 39-41	none-found	"_CredentialSeedDeclaration", "credential seed declaration", "source_env_var seed"	none
get_secret_from_env	function	lines 44-61	related-only	"get_secret_from_env", "runtime_env_path", "env_value", "_FILE", "read_text encoding utf-8 strip"	src/mindroom/avatar_generation.py:601; src/mindroom/cli/config.py:615; src/mindroom/voice_handler.py:352; src/mindroom/matrix/provisioning.py:14; src/mindroom/matrix/provisioning.py:20; src/mindroom/config/matrix.py:261
_sync_github_private_credentials	function	lines 64-92	duplicate-found	"github_private", "GITHUB_TOKEN", "_source env", "save_credentials", "credential_seeded_from_env"	src/mindroom/credentials_sync.py:95; src/mindroom/credentials_sync.py:307; src/mindroom/credentials.py:424
_sync_service_credentials	function	lines 95-119	duplicate-found	"_source env", "load_credentials", "save_credentials", "credential_env_sync_skipped"	src/mindroom/credentials_sync.py:64; src/mindroom/credentials.py:424; src/mindroom/api/credentials.py:489
_read_text_file	function	lines 122-126	related-only	"read_text encoding utf-8 strip", "except OSError return None"	src/mindroom/agents.py:203; src/mindroom/memory/auto_flush.py:208; src/mindroom/matrix/sync_tokens.py:111; src/mindroom/custom_tools/subagents.py:101
_resolve_seed_file_path	function	lines 129-133	related-only	"Path(raw_path).expanduser", "config_dir / path", "resolve_config_relative_path", "expanduser resolve"	src/mindroom/config/matrix.py:255; src/mindroom/constants.py:311; src/mindroom/cli/connect.py:109; src/mindroom/custom_tools/attachments.py:184
_coerce_seed_entries	function	lines 136-152	none-found	"object with a 'seeds' list", "credential seed at index", "Mapping and list coercion"	none
_decode_seed_entries	function	lines 155-176	none-found	"credential_seed_declaration_json_invalid", "json.loads raw_json", "credential_seed_declaration_invalid"	none
_load_declared_credential_seeds	function	lines 179-211	none-found	"CREDENTIAL_SEEDS_FILE_ENV", "CREDENTIAL_SEEDS_JSON_ENV", "_CredentialSeedDeclaration"	none
_resolve_seed_value	function	lines 214-235	related-only	"objects with env file value", "value_mapping.get env", "value_mapping.get file"	src/mindroom/config/matrix.py:261; src/mindroom/matrix/provisioning.py:26; src/mindroom/model_loading.py:62
_resolve_seed_credentials	function	lines 238-269	none-found	"credentials object", "may not set internal field", "credential_seed_value_missing"	none
_sync_declared_credential_seeds	function	lines 272-304	none-found	"validate_service_name", "_resolve_seed_credentials", "_sync_service_credentials", "synced_count"	none
sync_env_to_credentials	function	lines 307-359	related-only	"PROVIDER_ENV_KEYS", "_ENV_TO_SERVICE_MAP", "GOOGLE_APPLICATION_CREDENTIALS", "credentials_synced_from_env"	src/mindroom/api/main.py:358; src/mindroom/orchestrator.py:1941; src/mindroom/model_loading.py:77
get_api_key_for_provider	function	lines 362-386	related-only	"get_api_key_for_provider", "provider == gemini", "creds_manager.get_api_key"	src/mindroom/model_loading.py:54; src/mindroom/memory/config.py:69; src/mindroom/memory/config.py:107
get_ollama_host	function	lines 389-400	related-only	"get_ollama_host", "load_credentials(\"ollama\")", "ollama_creds host"	src/mindroom/model_loading.py:93; src/mindroom/memory/config.py:77; src/mindroom/memory/config.py:95; src/mindroom/memory/config.py:122
```

## Findings

### 1. GitHub token sync duplicates the generic env-owned credential sync policy

- Primary behavior: `src/mindroom/credentials_sync.py:64` loads `GITHUB_TOKEN`, checks existing `github_private` credentials, skips credentials whose `_source` is not `"env"`, saves a payload with `_source: "env"`, and logs whether it seeded or updated.
- Duplicate behavior: `src/mindroom/credentials_sync.py:95` implements the same load-existing, skip-non-env-source, save-with-`_source: env`, and seeded/updated log policy for all named services.
- Why this is duplicated: both functions enforce the same ownership rule for env-sourced credentials.
  The only meaningful differences are the hard-coded service name, the GitHub username/token payload shape, and legacy human-readable log messages in the GitHub function.
- Difference to preserve: `github_private` maps one token into `{"username": "x-access-token", "token": github_token}` rather than the provider default `{"api_key": env_value}`.
  Its missing-token debug message is also specific to `GITHUB_TOKEN`/`GITHUB_TOKEN_FILE`.

### 2. Ollama host credential reads are centralized but not consistently used

- Primary behavior: `src/mindroom/credentials_sync.py:389` loads the shared `ollama` credential and returns its `host`.
- Related repeated behavior: `src/mindroom/memory/config.py:77`, `src/mindroom/memory/config.py:95`, and `src/mindroom/memory/config.py:122` directly load `creds_manager.load_credentials("ollama")` and read `"host"` with local fallback handling.
- Why this is related rather than a direct duplicate: `get_ollama_host` takes `RuntimePaths`, while `memory/config.py` already has a `CredentialsManager`.
  Reusing it directly would either require changing the helper signature or deriving a runtime manager again.
- Difference to preserve: memory configuration combines stored host values with configured host/default fallbacks and Agno-specific key names such as `ollama_base_url`.

## Proposed Generalization

1. Fold `_sync_github_private_credentials` through `_sync_service_credentials` by building the GitHub payload locally, then calling `_sync_service_credentials(service="github_private", credentials=..., env_var="GITHUB_TOKEN", runtime_paths=runtime_paths)`.
2. Preserve the GitHub-specific missing-token debug log.
3. Accept the structured generic seeded/updated log event names unless callers/tests require the current GitHub-specific info messages.
4. Optionally add a small `get_ollama_host_from_credentials(creds_manager: CredentialsManager) -> str | None` helper only if future work is already touching `memory/config.py`; otherwise leave the current direct reads alone.

## Risk/tests

Risk is low for the GitHub sync dedupe if tests assert behavior through saved credentials and skip rules.
Risk is moderate only if tests or log consumers depend on the exact GitHub info log strings.
Relevant tests should cover seeding `github_private`, updating when existing `_source` is `"env"`, skipping when `_source` is `"ui"` or missing, and preserving the `username`/`token` payload.
For the Ollama related-only case, tests would need to cover memory embedder, configured memory LLM, and fallback memory LLM host precedence before changing helper boundaries.
