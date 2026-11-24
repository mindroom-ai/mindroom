# MindRoom Installs MindRoom Chat Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: implement this plan task-by-task with the skill named in **Execution**. Steps use checkbox (`- [ ]`) syntax.

**Execution:** baspowers:executing-plans — four short tasks that share one small interface, built in the session that wrote the spec.

**Goal:** MindRoom for macOS installs the released, notarized MindRoom Chat next to itself with one click and opens it afterwards.

**Architecture:** A pure release picker, a signature check, and an installer whose network, tools, and verification are injectable feed one `@MainActor` store. The Overview card and the menu item only call that store.

**Tech Stack:** Swift 5.9 package `macos/MindRoom` (AppKit, SwiftUI, Security), XCTest, `hdiutil`, `ditto`.

**Spec:** `docs/baspowers/specs/2026-11-19-mac-install-chat-app-design.md`

## Global Constraints

- Bundle identifier `chat.mindroom.app`; team `DNA6966LGZ`; installed name `MindRoom Chat.app`.
- Asset names in order: `MindRoom-Chat.dmg`, `MindRoom.dmg`; only `state == "uploaded"`; no drafts or prereleases.
- Download URLs start with `https://github.com/mindroom-ai/mindroom-chat/releases/download/`.
- Never overwrite a bundle, never install when Launch Services knows a copy, never install without a click.
- The requirement string and the four `SecCSFlags` are copied verbatim from the spec.
- At most 300 net product Swift lines. Tests use placeholder data only.
- Test command: `swift test --package-path macos/MindRoom --filter ChatAppInstallTests` (on a Mac; this container has no Swift).

## Review Focus

1. Newest release without an image (the minutes after publishing): the older release's image is used. → Task 1.
2. A validly signed app of another developer inside the image: refused, nothing left at the target. → Tasks 2 and 3.
3. Target appears between the first check and the rename: the rename fails, the existing bundle is untouched. → Task 3.
4. `/Applications` not writable: `~/Applications` is created and used. → Task 3.
5. Launch Services has not registered the fresh copy yet: the store still reports it installed. → Task 4.

---

### Task 1: Release picker

**Files:**
- Create: `macos/MindRoom/Sources/MindRoom/ChatAppRelease.swift`
- Test: `macos/MindRoom/Tests/MindRoomTests/ChatAppInstallTests.swift`

**Interfaces:**
- Produces: `enum ChatAppRelease { static let listURL: URL; static func downloadURL(fromReleaseList: Data) -> URL? }`

- [ ] Tests `testPicksNewestPublishedReleaseThatHasAnImage` (newest has only `appcast.xml`; a draft and a prerelease with images; a `starter` asset; then a release with both names → the `MindRoom-Chat.dmg` URL), `testFallsBackToOldImageName`, `testRefusesListsWithoutAUsableImage` (`{}`; not JSON; URL on another host).
- [ ] Implement with `Decodable` structs and the walk from the spec.
- [ ] Commit.

### Task 2: Signature check

**Files:**
- Create: `macos/MindRoom/Sources/MindRoom/ChatAppSignature.swift`
- Test: same test file

**Interfaces:**
- Produces: `enum ChatAppSignature { static let bundleIdentifier: String; static let requirement: String; static func verify(_ app: URL) throws }`; throws `ChatAppInstallError.untrusted(String)` (the error enum lives in `ChatAppInstaller.swift`, Task 3).

- [ ] Test `testRefusesUnsignedAndForeignApps`: a temp folder `Fake.app/Contents/Info.plist` and `/System/Applications/Calculator.app` both throw `.untrusted`.
- [ ] Implement with `SecStaticCodeCreateWithPath`, `SecRequirementCreateWithString`, `SecStaticCodeCheckValidity`.
- [ ] Commit (with Task 3 if the error enum is needed to compile).

### Task 3: Installer

**Files:**
- Create: `macos/MindRoom/Sources/MindRoom/ChatAppInstaller.swift`
- Test: same test file

**Interfaces:**
- Produces:
  - `enum ChatAppInstallError: LocalizedError, Equatable { case noRelease, alreadyExists(String), untrusted(String), failed(String) }`
  - `struct ChatAppInstaller { var applications: URL; var home: URL; var fetch: (URL) async throws -> Data; var download: (URL, URL) async throws -> Void; var run: (String, [String]) -> CommandResult; var verify: (URL) throws -> Void; var destination: URL { get }; func install(progress: @escaping (String) -> Void) async throws -> URL }`
- Consumes: Task 1 and 2 interfaces, `CommandResult`, `MindRoomCommandProcess`.

- [ ] Tests: `testInstallsIntoApplicationsWhenWritableElseHome`; `testInstallsVerifiedAppAndCleansUp` (fake `run`: `attach` creates `MindRoom.app` in the mount point, `ditto` copies, `detach` is recorded; asserts the target exists as `MindRoom Chat.app`, no `.MindRoom Chat.installing`, the download's folder is gone, `detach` ran); `testRefusedAppIsNotInstalledAndCleansUp`; `testNeverOverwritesAnExistingBundle` (no fetch happens); `testTargetAppearingDuringInstallIsKept`.
- [ ] Implement the eight install steps of the spec; one `defer` does step 8.
- [ ] Opt-in `testRealReleaseInstallsIntoGivenFolder` (`MINDROOM_CHAT_INSTALL_E2E_DIR`, else `XCTSkip`): real installer, asserts the installed bundle identifier and that `ChatAppSignature.verify` accepts it.
- [ ] Commit.

### Task 4: Store, Overview card, menu item, quit guard, docs

**Files:**
- Create: `macos/MindRoom/Sources/MindRoom/ChatAppStore.swift`, `macos/MindRoom/Sources/MindRoom/ChatAppCard.swift`
- Modify: `StatusMenuController.swift` (init parameter `chatApp`, item after **Open Chat**), `MindRoomRootView.swift` (card in `overview`), `AppDelegate.swift` (quit guard), `docs/installation/macos-app.md`, `macos/README.md`
- Test: same test file

**Interfaces:**
- Produces: `@MainActor final class ChatAppStore: ObservableObject { static let shared; @Published private(set) var installedURL: URL?; @Published private(set) var progress: String?; @Published private(set) var error: String?; init(installer:locate:open:); func refresh(); func openOrInstall() }`

- [ ] Tests: `testOpensAnInstalledCopyWithoutInstalling`, `testInstallsThenOpensWhenMissing` (and `installedURL` survives a `refresh()` while `locate` still returns nil), `testFailedInstallShowsTheReason`, `testMenuOffersInstallOrOpen` (titles `Install MindRoom Chat…` / `Open MindRoom Chat`).
- [ ] Implement store, card, menu item, quit guard.
- [ ] Docs: a **MindRoom Chat** section in `docs/installation/macos-app.md` and a paragraph in `macos/README.md`.
- [ ] Run the whole package suite and the opt-in real test on the MacBook M5; commit.
