---
name: live-test
description: Run live end-to-end checks for this repository. Use when booting the local MindRoom stack or SaaS sandbox, starting an isolated local Matrix/backend instance, creating disposable Matrix accounts, sending or reading messages with Matty, hitting live API endpoints, taking frontend screenshots, or verifying behavior through the real UI instead of tests alone. Also use when a live-test workflow struggled and the skill itself should be improved.
---

# Live Test

Run the real product and collect runtime evidence.

## Workflow

1. Choose the surface you need to test.
- For the core MindRoom runtime, local Matrix, Matty, and bundled dashboard, read [references/core-mindroom.md](references/core-mindroom.md).
- For the core frontend screenshot flow, frontend-only dev server, SaaS sandbox, and platform frontend screenshots, read [references/frontend-and-platform.md](references/frontend-and-platform.md).

2. Prefer isolated local runs when the worktree or machine is already busy.
- Existing local instances often collide on ports, Matrix usernames, room aliases, and dashboard API ports.
- If you see conflicts, create a temporary config, set a unique `MINDROOM_NAMESPACE`, use a unique `mindroom_user.username`, isolate `MINDROOM_STORAGE_PATH`, and choose a non-default `--api-port`.
- If the isolated run writes a temporary `.env`, inspect it before hitting authenticated `/api/*` routes because it may contain the instance-specific `MINDROOM_API_KEY`.

3. Verify behavior, not just startup.
- For chat flows, send a real message and inspect the actual reply thread.
- For backend changes, hit the live endpoint on the same instance you started.
- For frontend changes, capture screenshots and inspect the generated PNGs.

4. Preserve evidence.
- Record the exact command, port, room name, room ID, thread ID, and returned payload or screenshot path.
- Prefer Matty `--format json` when you need stable confirmation.
- If room aliases or thread listings are flaky, fall back to the concrete room ID from backend logs and direct `matty thread` reads.

5. Improve this skill when it struggles.
- If a live run exposes missing instructions, stale ports, missing workarounds, or a better repo-specific path, update this skill or its references in the same task when reasonable.
- Keep `SKILL.md` concise and move detailed command sequences into the reference files.
