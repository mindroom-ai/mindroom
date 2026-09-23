---
name: live-test
description: Run live end-to-end checks for this repository. Use when booting the local MindRoom stack or SaaS sandbox, starting an isolated local Matrix/backend instance, creating disposable Matrix accounts, sending or reading messages with Matty, hitting live API endpoints, taking frontend screenshots, or verifying behavior through the real UI instead of tests alone. Also use when a live-test workflow struggled and the skill itself should be improved.
---

# Live Test

Run the real product and collect runtime evidence.

## ⚠️ NixOS Environment

On NixOS hosts, run commands inside the repo Node.js 24 dev shell so `libstdc++.so.6` is available.
Without it, numpy fails to import and you get `AttributeError: module 'mindroom' has no attribute 'bot'`.

```bash
nix-shell shell.nix
# then run normally inside the shell:
uv run pytest ...
uv run mindroom run
```

If `nix-shell shell.nix` cannot resolve `<nixpkgs>`, use:

```bash
nix-shell -I nixpkgs=/nix/var/nix/profiles/per-user/root/channels/nixos shell.nix
```

See `references/core-mindroom.md` for details.

The root shell includes Linux Chromium only on Linux so the backend and Node.js tools also work on macOS.

## Workflow

1. Choose the surface you need to test.
- For the core MindRoom runtime, local Matrix, Matty, and bundled dashboard, read [references/core-mindroom.md](references/core-mindroom.md).
- For the core frontend screenshot flow, frontend-only dev server, SaaS sandbox, and platform frontend screenshots, read [references/frontend-and-platform.md](references/frontend-and-platform.md).

2. Prefer isolated local runs when the worktree or machine is already busy.
- Existing local instances often collide on ports, Matrix usernames, room aliases, and dashboard API ports.
- If you see conflicts, create an isolated config, set a unique `MINDROOM_NAMESPACE`, use a unique `mindroom_user.username`, isolate `MINDROOM_STORAGE_PATH`, and choose a non-default `--api-port`.
- If the isolated run writes its own `.env`, inspect it before hitting authenticated `/api/*` routes because it may contain the instance-specific `MINDROOM_API_KEY`.
- Keep config, storage, logs, transcripts, and snapshots in an isolated persistent directory under `.baspowers/sdd/` or `~/.codex/worktrees/`. Do not put live-test evidence in a temporary directory.

3. Verify behavior, not just startup.
- For chat flows, send a real message and inspect the actual reply thread.
- For backend changes, hit the live endpoint on the same instance you started.
- For frontend changes, capture screenshots and inspect the generated PNGs.

4. Add exploratory model testing for risky agent-facing behavior.
- Deterministic model stubs are useful for reproducible protocol and transport assertions, but they do not satisfy this exploratory check because the script already controls every tool choice.
- Run a supported non-stub model inside the running MindRoom instance. Give it a goal through normal Matrix messages and let it use the configured tools and workspace; do not call the feature implementation directly from the test harness.
- Ask the agent to choose and execute additional edge cases, then reduce suspected failures to reproducible cases.
- Choose a goal with an independently observable effect and combine the changed behavior with one or two adjacent capabilities. For interruption or lifecycle work, inject a follow-up while work is active, issue `Stop`, and restart the process from outside the agent where those events are in scope.
- Observe the actual tool-call events or server logs. A reply that says a tool ran or a task succeeded is not evidence that it did.
- After the turn and after any restart, independently inspect the affected files, complete process output, database rows, receipts, or other durable state. Treat the model's report as a lead, not the oracle.
- When a failure might predate the change, reproduce the smallest equivalent scenario on an appropriate baseline build or otherwise isolate the changed condition. Label it `regression`, `pre-existing`, or `unresolved` with the supporting evidence.

5. Preserve evidence.
- Record the exact command, port, room name, room ID, thread ID, and returned payload or screenshot path.
- Prefer Matty `--format json` when you need stable confirmation.
- If room aliases or thread listings are flaky, fall back to the concrete room ID from backend logs and direct `matty thread` reads.
- Record the build, provider/model route, isolated config and storage paths, scenario prompts and timing, observed tool calls, independent oracle results, and every case not run.
- State only what the evidence establishes. Exploratory success does not guarantee the absence of bugs.

6. Stop deliberately.
- Before starting, set a wall-clock budget, a finite scenario list, and a retry limit per scenario. Stop when the evidence answers the hypotheses, the budget is exhausted, or an unrecoverable environment blocker appears; record incomplete and untested cases instead of silently extending the run.

7. Improve this skill when it struggles.
- If a live run exposes missing instructions, stale ports, missing workarounds, or a better repo-specific path, update this skill or its references in the same task when reasonable.
- Keep `SKILL.md` concise and move detailed command sequences into the reference files.
