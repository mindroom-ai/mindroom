# Restart Resume

[![License](https://img.shields.io/badge/license-MIT-blue.svg)](LICENSE)
[![Docs](https://img.shields.io/badge/docs-plugins-blue)](https://docs.mindroom.chat/plugins/)
[![Hooks](https://img.shields.io/badge/docs-hooks-blue)](https://docs.mindroom.chat/hooks/)

<img src="https://media.githubusercontent.com/media/mindroom-ai/mindroom/refs/heads/main/frontend/public/logo.png" alt="MindRoom Logo" align="right" width="120" />

Re-activate idle threads after a [MindRoom](https://github.com/mindroom-ai/mindroom) restart.

MindRoom already resumes in-progress work automatically when an agent was mid-reply or had a scheduled task pending. This plugin handles the other case: threads that are idle, but that you still want to resume after the next restart. Tag the thread before restarting, and the plugin will wake it once the bot comes back up.

## Features

- Scans all rooms on `bot:ready` for threads tagged for restart follow-up
- Sends a wake-up message with `trigger_dispatch=True` so the agent continues working
- Removes the restart tag after successful notification
- Uses a configurable tag name instead of hard-coding `pending-restart`
- Uses an atomic claim file to avoid duplicate notifications when multiple workers start at once

## How It Works

1. Tag a thread with `pending-restart`, or another configured tag name.
2. Restart MindRoom.
3. When the bot emits `bot:ready`, the `notify-after-restart` hook scans room state for tagged threads.
4. Each matching thread receives a restart-complete notification that triggers agent dispatch.
5. After a successful notification, the restart tag is removed from that thread.

## Hooks

| Hook | Event | Purpose |
|------|-------|---------|
| `notify-after-restart` | `bot:ready` | Scan for tagged threads, wake them, and clear the restart tag |

## Configuration

Plugin settings in `config.yaml`:

| Setting | Required | Description |
|---------|----------|-------------|
| `tag` | No | Thread tag to scan for on restart. Defaults to `pending-restart` |

The plugin also uses `state_root/.restart-claim` as an atomic claim file so only one startup worker processes the restart notifications.

## Setup

1. Copy this plugin to `~/.mindroom/plugins/restart-resume`.
2. Add the plugin to `config.yaml`:
   ```yaml
   plugins:
     - path: plugins/restart-resume
   ```
3. Restart MindRoom.

No agent tools are required. This plugin is hooks-only.
