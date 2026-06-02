# macOS Menu Bar App

MindRoom ships as a native macOS menu bar app for running the local MindRoom service without using terminal commands.
The app bundles `uv`, installs the `mindroom` CLI through `uv tool install`, and manages the existing launchd service through `mindroom service`.
MindRoom config, secrets, and runtime state stay in `~/.mindroom`, so the app and CLI use the same files.

## Requirements

- macOS 13 Ventura or later.
- Network access for installing the `mindroom` package and pairing with hosted MindRoom.
- A model provider credential in `~/.mindroom/.env`, unless you configure a local or subscription-backed provider.

## Install

Install the signed app release with Homebrew.

```bash
brew install --cask mindroom-ai/mindroom/mindroom
```

Open **MindRoom** from `/Applications` or Spotlight.

## First Launch

Use **Install MindRoom Runtime** to install the CLI runtime with bundled `uv`.
Use **Initialize Hosted Config** for the default `chat.mindroom.chat` profile.
Open `https://chat.mindroom.chat`, generate a local MindRoom pair code, and use **Pair Hosted MindRoom...** in the menu.
Use **Install/Ensure Service** to run `mindroom service install --no-confirm`.
Use **Open Dashboard** to open `http://localhost:8765`.

## Other Setup Modes

Use **Initialize Self-Hosted Config** when you want to connect to your own Matrix homeserver.
Use **Run Local Stack Setup** when you want the local Matrix stack flow.
These actions still write to `~/.mindroom`.

## Service Controls

The menu exposes **Start Service**, **Stop Service**, **Restart Service**, and **Refresh Status**.
The service is managed by launchd, so MindRoom keeps running after the menu bar app quits.
Logs are available through **Open Logs Folder** at `~/Library/Logs/mindroom`.
Configuration is available through **Open Config Folder** at `~/.mindroom`.

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
