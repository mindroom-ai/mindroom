# PLAN-MERGED.md — ISSUE-175

## Goal
Switch the MindRoom browser tool from anonymous Chromium contexts to persistent on-disk Chromium profiles so Cinny's `localStorage` session survives across agent runs and screenshots show the real room timeline instead of the login screen.
Keep the implementation narrow by changing the browser tool, adding one headed login bootstrap script, extending tests, and documenting the one-time human login flow.

## Divergence Decisions
- `D1`: Use `service_workers="block"` in both the browser tool and the headed login helper because Cinny tolerates missing service-worker control and stale SW/cache state is the sharper redeploy risk.
- `D2`: Name profile directories `<profile_slug>` under each runtime's `storage_root` because production chat and lab already have distinct storage roots, while the helper script will resolve runtime paths from explicit args with `process_env={}` to avoid shell-env bleed-through.
- `D3`: Rehydrate every page in `context.pages` because `_resolve_tab()` only operates on tabs that were registered through `_register_tab()`.
- `D4`: Keep `headless=True` without `args=["--headless=new"]` in the first implementation because Playwright treats new headless as opt-in and the current code may launch an explicit system Chromium binary.

## File-By-File Changes
- `/srv/mindroom-worktrees/issue-175-plan-merge/src/mindroom/custom_tools/browser.py` touches current lines `19`, `168-176`, `179-184`, `929-990`, and `1023-1034` for shared helpers, persistent-context launch, multi-page rehydration, and context-only shutdown, with a projected delta of `+58/-15`.
- `/srv/mindroom-worktrees/issue-175-plan-merge/tests/test_browser_tool.py` touches current lines `13-19` and `260-306`, then appends new cases after line `306`, with a projected delta of `+94/-8`.
- `/srv/mindroom-worktrees/issue-175-plan-merge/scripts/browser-login-cinny.py` is a new file at lines `1-74` with a projected delta of `+74/-0`.
- `/srv/mindroom-worktrees/issue-175-plan-merge/scripts/README.md` touches current lines `7-17` and `23-37` for the new helper entry and usage example, with a projected delta of `+8/-0`.
- The projected total delta is `+234/-23`.
- No production screenshot change is planned in `/srv/mindroom-worktrees/issue-175-plan-merge/src/mindroom/custom_tools/browser.py:630-648` because the page path already uses Playwright `page.screenshot()` and the element/ref path already uses `locator().screenshot()`.

## Phases
### 1. Persistent Context Plumbing
1. Remove the separate `browser` handle from `_BrowserProfileState` at `/srv/mindroom-worktrees/issue-175-plan-merge/src/mindroom/custom_tools/browser.py:168-176` and treat `BrowserContext` plus `Playwright` as the durable state.
2. Replace `_ensure_profile()` and `_stop_profile()` at `/srv/mindroom-worktrees/issue-175-plan-merge/src/mindroom/custom_tools/browser.py:929-961` with `launch_persistent_context(...)`, the current executable override, the existing viewport, `headless=True`, `service_workers="block"`, `context.close()`, and `playwright.stop()`.

### 2. Tab Rehydration
1. Expand the startup block at `/srv/mindroom-worktrees/issue-175-plan-merge/src/mindroom/custom_tools/browser.py:946-952` to iterate every page in `context.pages`, call `_register_tab(...)` for each one, and set `active_target_id` to the first registered page.
2. Keep `/srv/mindroom-worktrees/issue-175-plan-merge/src/mindroom/custom_tools/browser.py:963-981` as the fallback that creates a fresh page only when the persistent context has no open pages or all registered pages were closed.

### 3. Profile-Dir Helper
1. Add a small shared helper near `/srv/mindroom-worktrees/issue-175-plan-merge/src/mindroom/custom_tools/browser.py:179-184` and `/srv/mindroom-worktrees/issue-175-plan-merge/src/mindroom/custom_tools/browser.py:1023-1034` that normalizes `profile_name`, resolves `<runtime_paths.storage_root>/browser-profiles/<profile_slug>`, and creates the directory eagerly.
2. Reuse that helper from both `BrowserTools` and the new login script so the runtime path contract is defined once and the on-disk naming scheme stays `<profile_slug>`.

### 4. Login Script
1. Add `/srv/mindroom-worktrees/issue-175-plan-merge/scripts/browser-login-cinny.py` with `--config-path`, optional `--storage-path`, optional `--profile`, and required `--url`.
2. Have the script call `resolve_runtime_paths(config_path=..., storage_path=..., process_env={})` so the current shell's exported `MINDROOM_CONFIG_PATH` or `MINDROOM_STORAGE_PATH` cannot accidentally point a lab login at the chat runtime.
3. Launch the same persistent profile headed, print the resolved `user_data_dir`, navigate to the requested Cinny URL, and wait for Enter after Bas sees the room timeline.

### 5. Tests
1. Rewrite `test_ensure_profile_uses_runtime_browser_executable()` at `/srv/mindroom-worktrees/issue-175-plan-merge/tests/test_browser_tool.py:261-306` around `launch_persistent_context()` and assert `user_data_dir`, `viewport`, `headless=True`, `service_workers="block"`, and `executable_path`.
2. Add tests for eager profile-dir creation, all-page rehydration, empty-page fallback to `new_page()`, context-only shutdown, and the unchanged element/ref screenshot path.

### 6. Docs
1. Update `/srv/mindroom-worktrees/issue-175-plan-merge/scripts/README.md:7-17` and `/srv/mindroom-worktrees/issue-175-plan-merge/scripts/README.md:23-37` with a one-time Cinny login entry and an explicit `uv run python scripts/browser-login-cinny.py --config-path ... --url ...` example.
2. Keep the browser module docstring short and put the operator workflow in the new script docstring plus `scripts/README.md` instead of widening product documentation outside this issue's scope.

## Test Plan
### Unit
1. Run `uv run pytest /srv/mindroom-worktrees/issue-175-plan-merge/tests/test_browser_tool.py -x -n 0 --no-cov -v`.
2. Run `uv run pre-commit run --files /srv/mindroom-worktrees/issue-175-plan-merge/src/mindroom/custom_tools/browser.py /srv/mindroom-worktrees/issue-175-plan-merge/tests/test_browser_tool.py /srv/mindroom-worktrees/issue-175-plan-merge/scripts/browser-login-cinny.py /srv/mindroom-worktrees/issue-175-plan-merge/scripts/README.md`.

### Live Test
1. Ask Bas to run or approve `sudo systemctl restart mindroom-chat.service` before the live check.
2. Seed the persistent profile with `uv run python /srv/mindroom-worktrees/issue-175-plan-merge/scripts/browser-login-cinny.py --config-path /home/basnijholt/.mindroom-chat/config.yaml --url http://localhost:8090/!TFs182DGokWnICCUm6:mindroom.lab.mindroom.chat`.
3. Log in manually as `e2e-test-bot` / `e2e-test-pw-2026`, wait for the room timeline, and press Enter to close the helper.
4. Have an agent run `browser navigate http://localhost:8090/!TFs182DGokWnICCUm6:mindroom.lab.mindroom.chat`.
5. Have the agent run `browser screenshot fullPage:true`.
6. Verify the captured PNG has more than `50` distinct colors with a short PIL or equivalent color-count check.
7. Verify the same PNG renders as the Cinny three-pane room layout with `chafa --size=80x40 <png>` instead of the centered `Welcome to MindRoom` login view.
8. Run `browser stop`, then repeat the same `browser navigate` and `browser screenshot` sequence.
9. Confirm the second post-stop screenshot still shows the room timeline, proving the on-disk profile survives the browser process lifetime.
10. Run one `browser screenshot` with `element` or `ref` once during live validation to confirm the existing element-capture path still works on tall Cinny views.

## Risks And Open Questions
- The only genuine open product question is whether the host Chromium selected by `BROWSER_EXECUTABLE_PATH` or `shutil.which()` still needs a later `--headless=new` override in persistent mode, and the live test above is the decision gate.
- Existing anonymous sessions do not migrate, so the first post-deploy screenshot will still show login until Bas runs the one-time headed helper for each runtime/profile pair he cares about.
