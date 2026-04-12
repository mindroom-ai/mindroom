Live-test skill for MindRoom repository — runs real product and collects runtime evidence.

## ⛔ CRITICAL SAFETY
- **NEVER touch `mindroom-chat.service` (port 8766)** — that is PRODUCTION
- **USE `mindroom-lab.service` (port 8765)** — that is the dev/test instance, safe to restart
- **Matrix homeserver (port 8008)** is always running — NEVER start another

## Quick Start

**For most live tests, use the lab service.** Read `references/core-mindroom.md` for the full recipe.

1. If your code is on `main`: `sudo systemctl restart mindroom-lab.service`
2. If your code is on a worktree branch: stop the lab, run from your worktree with lab config, test, restart lab
3. Create a disposable Matrix user (two-step UIAA with registration token — see reference)
4. Find a bot that's in a room (scan all bots — see reference)
5. Invite your test user, join room, send message mentioning the bot
6. Verify bot responds, capture evidence
7. Evidence = exact commands + observed output (HARD GATE for merge)

## References

- **`references/core-mindroom.md`** — Backend / Matrix live testing (lab service, isolated instance, user creation, room joining, messaging, troubleshooting)
- **`references/frontend-and-platform.md`** — Frontend / dashboard / SaaS screenshot workflows

## NixOS Requirement

**Must use `nix-shell shell.nix`** before running `uv run` commands (provides `libstdc++.so.6`).
Fallback: `nix-shell -I nixpkgs=/nix/var/nix/profiles/per-user/root/channels/nixos shell.nix`

## Key Principles

1. **Verify behavior, not just startup** — send real messages, get real bot responses
2. **Isolate** — use unique namespace, port, and user to avoid collision with production
3. **Preserve evidence** — record commands, ports, room IDs, payloads, screenshot paths
4. **Update this skill** when live runs expose gaps in the instructions