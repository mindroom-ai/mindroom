# MindRoom macOS Menu Bar App Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build a minimal macOS menu bar app that installs MindRoom through bundled `uv`, manages `mindroom service`, opens `~/.mindroom`, opens logs, opens the dashboard, and ships through AgentCLI-style Sparkle and Homebrew release metadata.

**Architecture:** Add a focused Swift package in `macos/MindRoom` with small collaborators for runtime paths, command construction, process execution, service status parsing, and menu wiring. Add one build script and release metadata files by adapting AgentCLI's proven macOS packaging flow while keeping MindRoom config and storage in `~/.mindroom`.

**Tech Stack:** Swift Package Manager, SwiftUI/AppKit menu bar APIs, Sparkle 2, bundled `uv`, existing Python `mindroom service` CLI, GitHub Actions, Homebrew cask.

---

## Implementation Tasks

### Task 1: Swift Package Skeleton And Runtime Tests

**Files.**
- Create: `macos/MindRoom/Package.swift`
- Create: `macos/MindRoom/Sources/MindRoom/AppMetadata.swift`
- Create: `macos/MindRoom/Sources/MindRoom/MindRoomRuntime.swift`
- Create: `macos/MindRoom/Sources/MindRoom/CommandResult.swift`
- Create: `macos/MindRoom/Tests/MindRoomTests/MindRoomRuntimeTests.swift`
- Create: `macos/MindRoom/Resources/Info.plist`
- Create: `macos/MindRoom/Resources/MindRoom.entitlements`

- [ ] **Step 1: Write failing runtime tests**

Add tests that assert the app uses `~/.mindroom`, constructs `uv tool install` for MindRoom, constructs `mindroom service install --no-confirm`, and prepends the bundled uv/tool paths without redirecting `MINDROOM_CONFIG_PATH`.

- [ ] **Step 2: Run tests to verify they fail**

Run `swift test --package-path macos/MindRoom`.
Expected result is FAIL because the Swift package and runtime types do not exist.

- [ ] **Step 3: Add minimal package and runtime types**

Add a Swift package with Sparkle dependency, a `CommandResult` struct, and a `MindRoomRuntime` struct with pure command-construction helpers.

- [ ] **Step 4: Run tests to verify they pass**

Run `swift test --package-path macos/MindRoom`.
Expected result is PASS.

- [ ] **Step 5: Commit**

Run `git add macos/MindRoom/Package.swift macos/MindRoom/Sources/MindRoom/AppMetadata.swift macos/MindRoom/Sources/MindRoom/MindRoomRuntime.swift macos/MindRoom/Sources/MindRoom/CommandResult.swift macos/MindRoom/Tests/MindRoomTests/MindRoomRuntimeTests.swift macos/MindRoom/Resources/Info.plist macos/MindRoom/Resources/MindRoom.entitlements && git commit -m "Add MindRoom macOS runtime package"`.

### Task 2: Command Runner And Service Status Tests

**Files.**
- Create: `macos/MindRoom/Sources/MindRoom/MindRoomCommand.swift`
- Create: `macos/MindRoom/Sources/MindRoom/MindRoomCommandRunner.swift`
- Create: `macos/MindRoom/Sources/MindRoom/ServiceStatus.swift`
- Create: `macos/MindRoom/Tests/MindRoomTests/ServiceStatusTests.swift`

- [ ] **Step 1: Write failing status parser tests**

Add tests for `running`, `installed but not running`, `not installed`, and missing runtime output.

- [ ] **Step 2: Run tests to verify they fail**

Run `swift test --package-path macos/MindRoom`.
Expected result is FAIL because status parser types do not exist.

- [ ] **Step 3: Add command and status code**

Add command enum cases for install runtime, update runtime, install service, lifecycle commands, config init, pairing, dashboard, logs, and config folder.
Add a command runner that executes commands asynchronously and captures output.
Add a service status parser that maps CLI output to menu status.

- [ ] **Step 4: Run tests to verify they pass**

Run `swift test --package-path macos/MindRoom`.
Expected result is PASS.

- [ ] **Step 5: Commit**

Run `git add macos/MindRoom/Sources/MindRoom/MindRoomCommand.swift macos/MindRoom/Sources/MindRoom/MindRoomCommandRunner.swift macos/MindRoom/Sources/MindRoom/ServiceStatus.swift macos/MindRoom/Tests/MindRoomTests/ServiceStatusTests.swift && git commit -m "Add MindRoom macOS command runner"`.

### Task 3: Menu Bar App UI

**Files.**
- Create: `macos/MindRoom/Sources/MindRoom/MindRoomApp.swift`
- Create: `macos/MindRoom/Sources/MindRoom/AppDelegate.swift`
- Create: `macos/MindRoom/Sources/MindRoom/StatusMenuController.swift`
- Create: `macos/MindRoom/Sources/MindRoom/AppUpdater.swift`
- Create: `macos/MindRoom/Sources/MindRoom/LoginItemController.swift`

- [ ] **Step 1: Write compile-focused app shell**

Add a minimal SwiftUI app delegate, menu controller, updater wrapper, and login item controller.
The menu includes Install or Update Runtime, Ensure Service, Start, Stop, Restart, Initialize Hosted Config, Initialize Self-Hosted Config, Pair Hosted MindRoom, Open Dashboard, Open Config Folder, Open Logs Folder, Check for Updates, and Quit.

- [ ] **Step 2: Run Swift tests and build**

Run `swift test --package-path macos/MindRoom && swift build -c release --package-path macos/MindRoom`.
Expected result is PASS.

- [ ] **Step 3: Commit**

Run `git add macos/MindRoom/Sources/MindRoom/MindRoomApp.swift macos/MindRoom/Sources/MindRoom/AppDelegate.swift macos/MindRoom/Sources/MindRoom/StatusMenuController.swift macos/MindRoom/Sources/MindRoom/AppUpdater.swift macos/MindRoom/Sources/MindRoom/LoginItemController.swift && git commit -m "Add MindRoom macOS menu app"`.

### Task 4: Build Script, Appcast, Cask, And Release Workflow

**Files.**
- Create: `macos/build-macos-app.sh`
- Create: `macos/appcast.xml`
- Create: `Casks/mindroom.rb`
- Create: `.github/scripts/update_mindroom_cask.py`
- Create: `.github/scripts/normalize_appcast.py`
- Modify: `.github/workflows/release.yml`
- Modify: `.github/workflows/calver-auto-release.yml`

- [ ] **Step 1: Add packaging files**

Adapt AgentCLI's build script to build `MindRoom.app`, bundle `uv`, copy Sparkle, stamp versions, codesign, create a DMG, and optionally notarize.
Add an initial empty Sparkle appcast and a Homebrew cask.

- [ ] **Step 2: Add release workflow integration**

Extend the release workflow with a macOS job that builds the DMG and zip, uploads both to the release, updates appcast, updates cask SHA256, and commits metadata to `main`.
Update the CalVer dispatcher so macOS app publishing runs with the existing release publishers.

- [ ] **Step 3: Run script self-test and metadata checks**

Run `SPARKLE_PUBLIC_ED_KEY=test macos/build-macos-app.sh`.
Expected result is PASS on macOS and produce `dist/macos/MindRoom.app`.
Run `swift test --package-path macos/MindRoom`.
Expected result is PASS.

- [ ] **Step 4: Commit**

Run `git add macos/build-macos-app.sh macos/appcast.xml Casks/mindroom.rb .github/scripts/update_mindroom_cask.py .github/scripts/normalize_appcast.py .github/workflows/release.yml .github/workflows/calver-auto-release.yml && git commit -m "Add MindRoom macOS release packaging"`.

### Task 5: Documentation And Final Verification

**Files.**
- Modify: `README.md`
- Create: `docs/installation/macos-app.md`

- [ ] **Step 1: Add docs**

Document installing the cask, opening the app, installing runtime, initializing hosted config, pairing, installing the service, opening the dashboard, updating, and uninstalling.

- [ ] **Step 2: Run final verification**

Run `swift test --package-path macos/MindRoom`.
Expected result is PASS.
Run `macos/build-macos-app.sh`.
Expected result is PASS.
Run `uv run pytest tests/test_services.py -q`.
Expected result is PASS.

- [ ] **Step 3: Commit**

Run `git add README.md docs/installation/macos-app.md && git commit -m "Document MindRoom macOS app"`.
