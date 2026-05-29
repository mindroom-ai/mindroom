# MindRoom macOS Menu Bar App Design

## Context

MindRoom currently supports command-line installation, `~/.mindroom` configuration, and a `mindroom service` command that installs a launchd user service on macOS.
AgentCLI has a proven macOS menu bar app pattern that bundles `uv`, uses SwiftUI, ships through a signed and notarized DMG, self-updates with Sparkle, and updates Homebrew cask metadata through CI.
MindRoom should adopt the same packaging and release shape without creating a separate app-private MindRoom configuration home.

## Goals

The app should let a non-terminal user install, configure, run, update, and inspect MindRoom from the macOS menu bar.
The app should use `~/.mindroom` for config, environment, and persistent MindRoom state so CLI and app usage stay interoperable.
The app should default to hosted `chat.mindroom.chat` setup while keeping self-hosted and local-stack setup visible.
The app should manage the existing `mindroom service` lifecycle instead of duplicating launchd logic in Swift.
The app should self-update the same way AgentCLI does.

## Non-Goals

The app will not embed the dashboard in a native webview.
The app will not implement a custom service manager in Swift.
The app will not maintain an app-private MindRoom config or storage tree.
The app will not bundle a MindRoom wheel in the first version unless release constraints make that necessary.
The app will not add a full setup wizard before the simpler menu flow has been tried.

## Architecture

Add a `macos/MindRoom` Swift package that builds a small LSUIElement menu bar app.
The app bundles a `uv` binary under `Contents/Resources/bin/uv`.
The app depends on Sparkle for app updates.
The app runs CLI commands through a small command runner that injects an app-controlled PATH with the bundled `uv` and uv tool bin directory first.
The app uses the default MindRoom runtime paths, so `mindroom` resolves `~/.mindroom/config.yaml`, `~/.mindroom/.env`, and `~/.mindroom/mindroom_data`.
The app reads service state by running `mindroom service status` and displays a compact running, stopped, missing-runtime, or missing-config status row in the menu.

## Runtime Installation

On launch, the app checks whether `mindroom --version` succeeds through the uv tool environment.
If the command is missing, the menu shows an install action instead of silently doing heavy work.
The install action runs `uv tool install --managed-python --python 3.13 mindroom`.
The update action runs `uv tool install --managed-python --python 3.13 --force mindroom`.
The app should set uv directories only for the uv tool installation itself when needed, but it should not redirect MindRoom config or storage away from `~/.mindroom`.
The app should surface command output in a troubleshooting menu and write app command failures to an app log file.

## Service Lifecycle

The primary start action runs `mindroom service install --no-confirm`.
This creates or refreshes the launchd service and starts it.
Start, stop, restart, and status actions call the existing `mindroom service start`, `mindroom service stop`, `mindroom service restart`, and `mindroom service status` commands.
The app should not call `mindroom run` directly for normal operation.
The app should rely on launchd to keep MindRoom running after the menu bar app quits.

## Configuration Menu

The configuration menu contains a default hosted setup action for `chat.mindroom.chat`.
That action runs the current hosted-profile `mindroom config init` command that writes to `~/.mindroom`.
The menu also exposes self-hosted config initialization and local-stack setup actions.
The hosted pairing action opens a simple pair-code dialog and runs `mindroom connect --pair-code <code>`.
The menu should also include a shortcut to open `https://chat.mindroom.chat` for pair-code generation.
Config actions should avoid overwriting existing config without showing the same safety behavior the CLI already provides.

## Utility Menu

The app includes an Open Dashboard action that opens `http://localhost:8765`.
The app includes Open Config Folder for `~/.mindroom`.
The app includes Open Logs Folder for `~/Library/Logs/mindroom`.
The app includes Runtime Status and Copy Last Output troubleshooting actions.
The app includes Check for App Updates through Sparkle.
The app includes Quit.

## Release And CI

Copy the AgentCLI release pattern into MindRoom with MindRoom-specific names, bundle IDs, appcast URL, and cask path.
The release workflow should build a signed and notarized DMG on macOS.
The workflow should upload the DMG and app zip to the GitHub release.
The workflow should run Sparkle `generate_appcast` to update `macos/appcast.xml`.
The workflow should update a Homebrew cask file with the new version and SHA256.
The workflow should commit the appcast and cask metadata back to `main`.
The existing CalVer release dispatcher should trigger this macOS app build alongside PyPI, Docker image, and Helm chart publishing.

## Tests And Verification

Swift tests should cover command construction, runtime-path behavior, and status parsing.
The build script should include a self-test mode that verifies bundled `uv`, Sparkle metadata, icon resources, and command runner setup.
CI should run the Swift package tests on macOS.
CI should verify the DMG contains `MindRoom.app`, bundled `uv`, Sparkle framework, Info.plist metadata, and app icon resources.
Manual verification should install the DMG, initialize hosted config, pair with `chat.mindroom.chat`, install the service, confirm launchd reports MindRoom running, open the dashboard, and confirm logs are accessible.

## Open Decisions

The exact hosted config command should match the current CLI surface at implementation time.
The Homebrew tap location should be chosen before adding the cask update script.
The app bundle identifier should be finalized before signing and Sparkle keys are generated.
