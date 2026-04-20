# DEBATE.md — ISSUE-175

This note records why the merged plan chose one side of each required divergence and which evidence drove the decision.

## D1 — Service Worker Handling
- Decision: use `service_workers="block"` in both the runtime browser tool and the headed login helper.
- Evidence: `/var/www/cinny/src/index.tsx:81-117` wraps `navigator.serviceWorker.register(...)` in `try/catch`, keeps booting if registration fails, and only waits through `waitForServiceWorkerControl()`.
- Evidence: `/var/www/cinny/src/sw-session.ts:1-44` caps that wait at `3000ms`, so a blocked service worker causes delay rather than a hang.
- Evidence: `/var/www/cinny/src/app/utils/mediaUrl.ts:45-68` falls back to `access_token` query auth when there is no controlling service worker.
- Evidence: `/var/www/cinny/src/client/initMatrix.ts:584-600` already ships a manual service-worker and cache clear path, which is strong evidence that stale cache state is a real operational risk after redeploys.
- Synthesis: Codex wins on D1 because Cinny already tolerates a missing controller while stale service-worker state is exactly the failure class Bas wants to stop seeing after `npm run build` and a service restart.

## D2 — Profile Dir Naming
- Decision: use `<profile_slug>` under each runtime's `storage_root`, not `<runtime_slug>-<profile_slug>`.
- Evidence: `/srv/mindroom/src/mindroom/constants.py:90-94` and `/srv/mindroom/src/mindroom/constants.py:202-210` make `storage_root` the runtime boundary, with exported process env taking precedence over the config-adjacent `.env`.
- Evidence: `/nix/store/50gcy3jlg52qf9izlkpy3kqylkbv8nyv-unit-mindroom-chat.service/mindroom-chat.service:10-11` and `/nix/store/1lyxgi6yi48w4nc5ac6jmi5ad3qig21l-unit-mindroom-lab.service/mindroom-lab.service:10-11` show production chat and lab already export distinct `MINDROOM_STORAGE_PATH` values.
- Evidence: `/home/basnijholt/.mindroom-lab/.env:6-7` matches the lab-specific path, so the runtime's own config contract is already separate.
- Evidence: `/srv/mindroom/src/mindroom/constants.py:241-254` means helper scripts that inherit the wrong shell env can still be polluted unless they resolve runtime paths from explicit args with `process_env={}`.
- Synthesis: Claude wins on the on-disk naming simplicity because production storage roots are already distinct, and Codex's stronger runtime-targeting concern is retained in the helper script design instead of the directory name itself.

## D3 — Tab Rehydration Depth
- Decision: rehydrate every page in `context.pages`.
- Evidence: `/srv/mindroom-worktrees/issue-175-plan-merge/src/mindroom/custom_tools/browser.py:963-981` only knows how to focus tabs that already exist in `state.tabs`.
- Evidence: `/srv/mindroom-worktrees/issue-175-plan-merge/src/mindroom/custom_tools/browser.py:983-990` shows `_register_tab()` is the only place that makes a page addressable and wires console, dialog, and close handlers.
- Synthesis: Codex wins on D3 because registering only `context.pages[0]` would orphan any other restored pages from the persistent session, and the all-pages loop is a very small amount of code for a cleaner state model.

## D4 — Headless Mode Flag
- Decision: keep `headless=True` without explicit `args=["--headless=new"]` in the first implementation.
- Evidence: `/srv/mindroom-worktrees/issue-175-plan-merge/src/mindroom/custom_tools/browser.py:941-945` already prefers an explicit system executable, so a Chromium-specific custom arg would be speculative and harder to justify without live evidence.
- Evidence: local `help(playwright.async_api.BrowserType.launch_persistent_context)` from `/srv/mindroom/.venv` lists `channel` and documents `service_workers`, but it does not make new headless the default path.
- Evidence: the official Playwright Python browser docs at `https://playwright.dev/python/docs/browsers#chromium-new-headless-mode` describe new headless as an opt-in `channel="chromium"` path and separately describe the default headless shell behavior.
- Synthesis: Claude's concern is valid enough to keep as a live-test gate, but the merged plan keeps the simpler default until the real Cinny smoke test proves otherwise.

## Outcome
- Adopt Codex on D1 and D3.
- Adopt Claude on D2 and D4, while keeping Codex's explicit helper-script runtime targeting as part of D2's implementation detail.
- Leave screenshot mechanics out of scope because `/srv/mindroom-worktrees/issue-175-plan-merge/src/mindroom/custom_tools/browser.py:630-648` already uses Playwright page and locator screenshots rather than the broken DevTools path from the separate MEMORY note.
