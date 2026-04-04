# Round 1 Report

Status: complete.
Full backend suite passed with `3475 passed, 19 skipped` in `250.71s`.
Targeted frontend tests passed with `87 passed` across `src/store/configStore.test.ts` and `src/components/VoiceConfig/VoiceConfig.test.tsx`.

## Triage

- `F-1` FIX: Updated `tests/test_workloop_thread_scope.py` to match the current `auto_poke` contract, which suppresses the heartbeat tick instead of awaiting `message_sender`.
- `E-1` FIX: Restored `m.notice` visibility in Matrix read/thread-history paths and preserved top-level `msgtype` serialization.
- `E-2` FIX: Restored interactive edit cleanup and re-registration so edits clear stale question state and re-register buttons for new interactive content.
- `E-4` FIX: Restored per-thread summary locking in `thread_summary.py` and added concurrent-call coverage.
- `G-1` FIX: Reused the committed sandbox runner token during lifespan re-entry instead of wiping it.
- `G-2` FIX: Reused committed sandbox runner config/runtime state during lifespan re-entry instead of reparsing disk config.
- `D-1` FIX: Added strict top-level config validation and explicit rejection for removed root fields such as `mcp_servers`.
- `D-2` FIX: Added strict agent/defaults validation and explicit rejection for removed toolkit-era fields.
- `E-3` IGNORE: Reintroducing `room-threads` or a new thread-root enumeration API is a design-surface change that exceeds this minimal review-fix round, so I left it for explicit follow-up discussion.
- `C-1` FIX: Added committed-generation headers to config load endpoints, stored generation in the frontend config store, sent it on save/raw-save, and covered stale-save rejection.
- `F-2` FIX: Reworked the OpenAI compat runtime-swap test to exercise the real `_parse_chat_request()` path and spy on `_load_config()`.
- `F-3` FIX: Added `/v1/chat/completions` coverage for invalid runtime config and malformed YAML.
- `F-4` FIX: Added protected raw-config tests for invalid-reload recovery, generation enforcement, and auth-time runtime-swap behavior.
- `F-5` FIX: Added first-hop and deep synthetic `hook_dispatch` coverage for sidecar-prepared text and prepared-text dispatch paths.
- `F-6` FIX: Added explicit `python:` plugin resolution coverage for both success and failure paths.
- `C-2` IGNORE: Narrowing `Config.validate_with_runtime()` exception handling would require a broader error-taxonomy refactor, and I did not have a concrete misclassified internal bug to justify that change in this round.
- `C-3` FIX: Switched `VoiceConfig` to shared save-failure toast handling and disabled repeat submits while loading.
- `G-3` FIX: Stopped appending the mutating-command rejection footer to read-only `!config show` and `!config get` failures.
- `H-5` IGNORE: Broader editor save-result standardization is UX consistency work rather than a correctness regression, and the VoiceConfig-specific issue was already fixed under `C-3`.
- `H-7` IGNORE: `sys.modules` cleanup after plugin rollback remains a known non-functional limitation and was left unchanged.
- `H-4` IGNORE: Commit-history cleanup is cosmetic and out of scope for a targeted fix round.
- `H-6` IGNORE: The planning doc in the diff is PR hygiene rather than code correctness.
- `H-8` IGNORE: The redundant fixture cleanup is cosmetic and harmless.

## Tests Run

- `nix-shell --run 'uv run pytest tests/test_workloop_thread_scope.py tests/test_matrix_message_tool.py -x -n 0 --no-cov -v'`
- `nix-shell --run 'uv run pytest tests/test_thread_summary.py tests/api/test_sandbox_runner_api.py tests/test_agents.py -x -n 0 --no-cov -v'`
- `bun run test:unit src/store/configStore.test.ts src/components/VoiceConfig/VoiceConfig.test.tsx`
- `nix-shell --run 'uv run pytest tests/test_openai_compat.py tests/test_plugins.py tests/test_hook_sender.py tests/test_config_commands.py tests/api/test_api.py -x -n 0 --no-cov -v'`
- `nix-shell --run 'uv run pytest tests/ -x -n 0 --no-cov -v'`

# Round 2 Report

Status: complete.
Full backend suite passed with `3479 passed, 19 skipped` in `250.77s`.
Full frontend suite passed with `411 passed, 1 skipped`.

## Triage

- `H-R2-1` FIX: Added `config_lifecycle.persist_runtime_validated_config()` plus live app registration so first-party config writers publish a new committed API snapshot/generation immediately instead of waiting for the watcher, and routed `config_manager`, `self_config`, and `!config set` through it.
- `H-R2-2` FIX: Updated `deleteAgent()` to mark `teams` and `cultures` dirty alongside `agents`, and added save-path coverage to prove dependent references are serialized out of the payload.
- `H-R2-3` FIX: `saveRecoveryConfigSource()` now stores the generation returned by `/api/config/raw` immediately, before reload, so a failed reload does not strand the UI on an obsolete generation token.
- `C-R2-1` FIX: Replaced the blanket `TypeError`/`ValueError` catch in `Config.validate_with_runtime()` with dedicated plugin/tool validation exception types so expected config errors stay in the 422 path while unexpected internal errors escape normally.

## Tests Run

- `export NIX_PATH="nixpkgs=/nix/var/nix/profiles/per-user/root/channels/nixos"; nix-shell --run 'uv run pytest tests/api/test_api.py tests/test_self_config.py tests/test_plugins.py tests/test_config_commands.py -x -n 0 --no-cov -v'`
- `cd frontend && npx vitest run src/store/configStore.test.ts`
- `export NIX_PATH="nixpkgs=/nix/var/nix/profiles/per-user/root/channels/nixos"; nix-shell --run 'uv run pytest tests/ -x -n 0 --no-cov -v'`
- `cd frontend && npx vitest run`
