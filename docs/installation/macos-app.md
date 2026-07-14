# macOS Menu Bar App

MindRoom ships as a native macOS menu bar app for running the local MindRoom service without using terminal commands.
The app bundles `uv`, installs the `mindroom` CLI through `uv tool install`, and manages the existing launchd service through `mindroom service`.
MindRoom config, secrets, and runtime state stay in `~/.mindroom`, so the app and CLI use the same files.

## Requirements

- macOS 13 Ventura or later.
- An Apple silicon or 64-bit Intel Mac.
- Network access for installing the `mindroom` package and pairing with hosted MindRoom.
- A model provider credential in `~/.mindroom/.env`, unless you configure a local provider or a Codex ChatGPT login.

## Install

Install the signed app release with Homebrew.

```bash
brew install --cask mindroom-ai/tap/mindroom
```

Open **MindRoom** from `/Applications` or Spotlight.

## First Launch

The menu lists the setup steps in order under **Set Up Hosted MindRoom**.
Each step shows a dialog when it finishes, confirming what happened and what to do next.

1. **Install MindRoom Runtime** installs the `mindroom` CLI with the bundled `uv`.
2. **Initialize Hosted Config** writes `config.yaml` and `.env` to `~/.mindroom`, preconfigured for the hosted `chat.mindroom.chat` Matrix server.
   Re-running this step keeps existing files unchanged and recreates any missing ones.
3. **Open chat.mindroom.chat** opens the hosted MindRoom chat in your browser.
   Sign in to create your hosted account, then click the Local MindRoom icon in the left sidebar to generate a pair code.
4. **Pair Hosted MindRoom...** links this Mac to your hosted account using the pair code.
5. **Install/Ensure Service** installs and starts the MindRoom background service via launchd.

Then use **Open Dashboard** to open the local dashboard at `http://localhost:8765`.

### Why sign in to chat.mindroom.chat?

`chat.mindroom.chat` is MindRoom's hosted Matrix service.
Signing in with Apple, Google, or GitHub creates a hosted Matrix account for you on the `mindroom.chat` homeserver, so you do not need to run or configure a homeserver yourself.
Pairing connects the MindRoom runtime on your Mac to that account: your agents run locally, and the hosted server only relays your Matrix messages.
If you want to use your own Matrix homeserver instead, use **Initialize Self-Hosted Config** under **Other Setup**.

## Other Setup Modes

The **Other Setup** submenu holds the non-hosted flows.
Use **Initialize Self-Hosted Config** when you want to connect to your own Matrix homeserver.
Use **Run Local Stack Setup** when you want the local Matrix stack flow.
These actions still write to `~/.mindroom`.

## Service Controls

The menu exposes **Start Service**, **Stop Service**, **Restart Service**, and **Refresh Status**.
The service is managed by launchd, so MindRoom keeps running after the menu bar app quits.
Logs are available through **Open Logs Folder** at `~/Library/Logs/mindroom`.
Configuration is available through **Open Config Folder** at `~/.mindroom`.

## Troubleshooting

**The dashboard at `http://localhost:8765` does not respond.**
The dashboard is served by the MindRoom service, so check the status line at the top of the menu.
If the service is stopped or not installed, **Open Dashboard** offers the matching fix (for example **Start Service**).
If the service is running but the dashboard still fails, check **Open Logs Folder** for startup errors.
A common cause is a missing model provider credential in `~/.mindroom/.env` (for example `ANTHROPIC_API_KEY` or `OPENAI_API_KEY`).

**A setup step failed.**
The failure dialog shows the command output, and **Copy Last Output** in the menu copies the full output for a bug report.

**Pairing fails or the pair code expired.**
Generate a fresh pair code in `chat.mindroom.chat` (Local MindRoom icon in the left sidebar) and run **Pair Hosted MindRoom...** again.

## Updates

Use **Update MindRoom Runtime** to run `uv tool install --managed-python --python 3.13 --force mindroom`.
Use **Check for App Updates...** to update the signed macOS app through Sparkle.
Homebrew users can also update with Homebrew.

```bash
brew update
brew upgrade --cask mindroom
```

## Uninstall

```bash
brew uninstall --cask mindroom
```

Use Homebrew zap to remove app preferences and logs.

```bash
brew uninstall --zap --cask mindroom
```

The zap command intentionally does not delete `~/.mindroom`.
Remove that directory manually only when you want to delete MindRoom config, credentials, and persistent agent data.
