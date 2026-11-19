import AppKit
import Combine
import XCTest
@testable import MindRoom

final class ChatAppInstallTests: XCTestCase {
    private static let downloads = "https://github.com/mindroom-ai/mindroom-chat/releases/download"
    private var folder: URL!

    override func setUpWithError() throws {
        folder = FileManager.default.temporaryDirectory.appendingPathComponent("ChatAppInstallTests-\(UUID().uuidString)")
        try FileManager.default.createDirectory(at: folder, withIntermediateDirectories: true)
    }

    override func tearDown() {
        try? FileManager.default.setAttributes([.posixPermissions: 0o755], ofItemAtPath: folder.appendingPathComponent("Applications").path)
        try? FileManager.default.removeItem(at: folder)
    }

    // MARK: Release picking

    private func release(_ tag: String, draft: Bool = false, prerelease: Bool = false, assets: [(String, String)]) -> [String: Any] {
        ["tag_name": tag, "draft": draft, "prerelease": prerelease, "assets": assets.map { name, state in
            ["name": name, "state": state, "browser_download_url": "\(Self.downloads)/\(tag)/\(name)"]
        }]
    }

    private func picked(_ releases: [[String: Any]]) throws -> String? {
        ChatAppRelease.downloadURL(fromReleaseList: try JSONSerialization.data(withJSONObject: releases))?.absoluteString
    }

    func testPicksNewestPublishedReleaseThatHasAnImage() throws {
        XCTAssertEqual(try picked([
            // A release gets its image minutes after it is published.
            release("v9", assets: [("appcast.xml", "uploaded")]),
            release("v8", draft: true, assets: [("MindRoom-Chat.dmg", "uploaded")]),
            release("v7", prerelease: true, assets: [("MindRoom-Chat.dmg", "uploaded")]),
            // A failed upload leaves a `starter` asset with nothing to download.
            release("v6", assets: [("MindRoom-Chat.dmg", "starter")]),
            release("v5", assets: [("MindRoom.dmg", "uploaded"), ("MindRoom-Chat.dmg", "uploaded")]),
            release("v4", assets: [("MindRoom-Chat.dmg", "uploaded")]),
        ]), "\(Self.downloads)/v5/MindRoom-Chat.dmg")
    }

    func testFallsBackToOldImageName() throws {
        XCTAssertEqual(try picked([
            release("v3", assets: [("appcast.xml", "uploaded"), ("MindRoom.dmg", "uploaded")]),
            release("v2", assets: [("MindRoom-Chat.dmg", "uploaded")]),
        ]), "\(Self.downloads)/v3/MindRoom.dmg")
    }

    func testRefusesListsWithoutAUsableImage() throws {
        XCTAssertNil(ChatAppRelease.downloadURL(fromReleaseList: Data(#"{"message":"API rate limit exceeded"}"#.utf8)))
        XCTAssertNil(ChatAppRelease.downloadURL(fromReleaseList: Data("<html>".utf8)))
        XCTAssertNil(try picked([release("v1", assets: [("Other.dmg", "uploaded")])]))
        var elsewhere = release("v1", assets: [("MindRoom-Chat.dmg", "uploaded")])
        elsewhere["assets"] = [["name": "MindRoom-Chat.dmg", "state": "uploaded",
                                "browser_download_url": "https://example.org/mindroom-ai/mindroom-chat/releases/download/v1/MindRoom-Chat.dmg"]]
        XCTAssertNil(try picked([elsewhere]))
    }

    // MARK: Verification

    /// A runnable bundle that claims to be MindRoom Chat.
    private func impostor(_ name: String) throws -> URL {
        let app = folder.appendingPathComponent(name)
        try FileManager.default.createDirectory(at: app.appendingPathComponent("Contents/MacOS"), withIntermediateDirectories: true)
        try FileManager.default.copyItem(atPath: "/usr/bin/true", toPath: app.appendingPathComponent("Contents/MacOS/Chat").path)
        let info = #"<plist version="1.0"><dict><key>CFBundleIdentifier</key><string>chat.mindroom.app</string>"#
            + "<key>CFBundleExecutable</key><string>Chat</string><key>CFBundlePackageType</key><string>APPL</string></dict></plist>"
        try Data(info.utf8).write(to: app.appendingPathComponent("Contents/Info.plist"))
        return app
    }

    func testRefusesUnsignedAndForeignApps() throws {
        let unsigned = try impostor("Unsigned.app")
        // The right identifier with an intact signature, from a signer that is not MindRoom.
        let selfSigned = try impostor("SelfSigned.app")
        let codesign = MindRoomCommandProcess().run(MindRoomCommandInvocation(
            executableURL: URL(fileURLWithPath: "/usr/bin/codesign"),
            arguments: ["--force", "--sign", "-", "--identifier", "chat.mindroom.app", selfSigned.path], environment: [:]
        ))
        XCTAssertTrue(codesign.isSuccess, codesign.output)
        // Calculator is validly signed by Apple, but not MindRoom Chat.
        for app in [unsigned, selfSigned, URL(fileURLWithPath: "/System/Applications/Calculator.app")] {
            XCTAssertThrowsError(try ChatAppSignature.verify(app), app.path) { error in
                guard case ChatAppInstallError.untrusted = error else { return XCTFail("\(error)") }
            }
        }
    }

    // MARK: Install

    /// Stands in for hdiutil and ditto: attach puts `MindRoom.app` in the mount point, ditto copies.
    private final class FakeTools {
        var calls: [String] = []
        var downloadFolder: URL?

        func run(_ tool: String, _ arguments: [String]) -> CommandResult {
            let files = FileManager.default
            calls.append("\(URL(fileURLWithPath: tool).lastPathComponent) \(URL(fileURLWithPath: arguments[0]).lastPathComponent)")
            do {
                if arguments[0] == "attach" {
                    let app = URL(fileURLWithPath: arguments.last!).appendingPathComponent("MindRoom.app/Contents")
                    try files.createDirectory(at: app, withIntermediateDirectories: true)
                    try Data("chat".utf8).write(to: app.appendingPathComponent("Info.plist"))
                } else if tool.hasSuffix("ditto") {
                    try files.copyItem(atPath: arguments[0], toPath: arguments[1])
                }
                return CommandResult(exitCode: 0, output: "")
            } catch {
                return CommandResult(exitCode: 1, output: "\(error)")
            }
        }
    }

    private func installer(_ tools: FakeTools, verify: @escaping (URL) throws -> Void = { _ in }) throws -> ChatAppInstaller {
        let applications = folder.appendingPathComponent("Applications")
        try FileManager.default.createDirectory(at: applications, withIntermediateDirectories: true)
        let list = try JSONSerialization.data(withJSONObject: [release("v1", assets: [("MindRoom.dmg", "uploaded")])])
        return ChatAppInstaller(
            applications: applications, home: folder.appendingPathComponent("home"),
            fetch: { _ in list },
            download: { _, file in
                tools.downloadFolder = file.deletingLastPathComponent()
                try Data("image".utf8).write(to: file)
            },
            run: tools.run, verify: verify
        )
    }

    private func leftovers(_ tools: FakeTools) throws -> [String] {
        let files = FileManager.default
        var found = try files.contentsOfDirectory(atPath: folder.appendingPathComponent("Applications").path)
            .filter { $0 != "MindRoom Chat.app" }
        if let download = tools.downloadFolder, files.fileExists(atPath: download.path) { found.append(download.path) }
        return found
    }

    func testInstallsIntoApplicationsWhenWritableElseHome() throws {
        let installer = try installer(FakeTools())
        XCTAssertEqual(installer.destination.path, folder.appendingPathComponent("Applications/MindRoom Chat.app").path)
        try FileManager.default.setAttributes([.posixPermissions: 0o555], ofItemAtPath: installer.applications.path)
        XCTAssertEqual(installer.destination.path, folder.appendingPathComponent("home/Applications/MindRoom Chat.app").path)
    }

    func testInstallsVerifiedAppAndCleansUp() async throws {
        let tools = FakeTools()
        var verified: [String] = []
        var stages: [String] = []
        let installer = try installer(tools) { verified.append($0.lastPathComponent) }

        let installed = try await installer.install { stages.append($0) }

        XCTAssertEqual(installed.path, folder.appendingPathComponent("Applications/MindRoom Chat.app").path)
        XCTAssertEqual(try String(contentsOf: installed.appendingPathComponent("Contents/Info.plist")), "chat")
        // The copy that is checked is the copy that is installed, before it gets its name.
        XCTAssertEqual(verified, [".MindRoom Chat.installing"])
        XCTAssertEqual(tools.calls, ["hdiutil attach", "ditto MindRoom.app", "hdiutil detach"])
        XCTAssertEqual(try leftovers(tools), [])
        XCTAssertGreaterThanOrEqual(stages.count, 3)
    }

    func testInstallsIntoHomeApplicationsWhenApplicationsIsReadOnly() async throws {
        let tools = FakeTools()
        let installer = try installer(tools)
        try FileManager.default.setAttributes([.posixPermissions: 0o555], ofItemAtPath: installer.applications.path)

        let installed = try await installer.install { _ in }

        XCTAssertEqual(installed.path, folder.appendingPathComponent("home/Applications/MindRoom Chat.app").path)
        XCTAssertTrue(FileManager.default.fileExists(atPath: installed.path))
    }

    func testRefusedAppIsNotInstalledAndCleansUp() async throws {
        let tools = FakeTools()
        let installer = try installer(tools) { _ in throw ChatAppInstallError.untrusted("not ours") }

        do {
            _ = try await installer.install { _ in }
            XCTFail("A refused app was installed")
        } catch {
            XCTAssertEqual(error as? ChatAppInstallError, .untrusted("not ours"))
        }

        XCTAssertFalse(FileManager.default.fileExists(atPath: installer.destination.path))
        XCTAssertEqual(tools.calls.last, "hdiutil detach")
        XCTAssertEqual(try leftovers(tools), [])
    }

    func testNeverOverwritesAnExistingBundle() async throws {
        let tools = FakeTools()
        var installer = try installer(tools)
        var fetched = false
        installer.fetch = { _ in fetched = true; return Data() }
        try FileManager.default.createDirectory(at: installer.destination, withIntermediateDirectories: true)
        try Data("mine".utf8).write(to: installer.destination.appendingPathComponent("kept"))

        do {
            _ = try await installer.install { _ in }
            XCTFail("An existing bundle was replaced")
        } catch {
            XCTAssertEqual(error as? ChatAppInstallError, .alreadyExists(installer.destination.path))
        }

        XCTAssertFalse(fetched, "Nothing is downloaded for a target that exists")
        XCTAssertEqual(tools.calls, [])
        XCTAssertEqual(try String(contentsOf: installer.destination.appendingPathComponent("kept")), "mine")
    }

    func testTargetAppearingDuringInstallIsKept() async throws {
        let tools = FakeTools()
        var destination: URL!
        let installer = try installer(tools) { _ in
            // Something else puts an app there after the first check, while this install runs.
            try FileManager.default.createDirectory(at: destination, withIntermediateDirectories: true)
            try Data("mine".utf8).write(to: destination.appendingPathComponent("kept"))
        }
        destination = installer.destination

        do {
            _ = try await installer.install { _ in }
            XCTFail("A bundle that appeared during the install was replaced")
        } catch {
            XCTAssertEqual(error as? ChatAppInstallError, .alreadyExists(destination.path))
        }

        XCTAssertEqual(try String(contentsOf: destination.appendingPathComponent("kept")), "mine")
        XCTAssertEqual(try leftovers(tools), [])
    }

    func testFailedToolStopsTheInstallWithItsOutput() async throws {
        let tools = FakeTools()
        var installer = try installer(tools)
        installer.run = { tool, arguments in
            arguments[0] == "attach" ? CommandResult(exitCode: 1, output: "image not recognized\n") : tools.run(tool, arguments)
        }

        do {
            _ = try await installer.install { _ in }
            XCTFail("An unreadable image was installed")
        } catch {
            XCTAssertEqual(error as? ChatAppInstallError, .failed("image not recognized"))
        }

        XCTAssertFalse(FileManager.default.fileExists(atPath: installer.destination.path))
        XCTAssertEqual(try leftovers(tools), [])
    }

    // MARK: Store and menu

    @MainActor
    private func finished(_ store: ChatAppStore) async {
        let done = expectation(description: "Install finished")
        let subscription = store.$progress.filter { $0 == nil }.prefix(1).sink { _ in done.fulfill() }
        await fulfillment(of: [done], timeout: 5)
        withExtendedLifetime(subscription) {}
    }

    @MainActor
    func testOpensAnInstalledCopyWithoutInstalling() throws {
        // A copy from any source counts, under whatever name it has.
        let existing = folder.appendingPathComponent("Elsewhere/MindRoom.app")
        try FileManager.default.createDirectory(at: existing, withIntermediateDirectories: true)
        var opened: [URL] = []
        var installer = try installer(FakeTools())
        installer.fetch = { _ in XCTFail("Installed although a copy exists"); return Data() }
        let store = ChatAppStore(installer: installer, locate: { existing }, open: { opened.append($0) })

        XCTAssertEqual(store.installedURL, existing)
        store.openOrInstall()

        XCTAssertEqual(opened, [existing])
        XCTAssertNil(store.progress)
        // Launch Services can name a copy that was since removed.
        try FileManager.default.removeItem(at: existing)
        store.refresh()
        XCTAssertNil(store.installedURL)
    }

    @MainActor
    func testInstallsThenOpensWhenMissing() async throws {
        let tools = FakeTools()
        var opened: [String] = []
        let store = ChatAppStore(installer: try installer(tools), locate: { nil }, open: { opened.append($0.lastPathComponent) })
        XCTAssertNil(store.installedURL)

        store.openOrInstall()
        XCTAssertNotNil(store.progress)
        store.openOrInstall()
        await finished(store)

        XCTAssertEqual(opened, ["MindRoom Chat.app"])
        XCTAssertEqual(tools.calls.filter { $0 == "hdiutil attach" }.count, 1, "A second click starts no second install")
        XCTAssertNil(store.error)
        // Launch Services may not know the fresh copy yet.
        store.refresh()
        XCTAssertEqual(store.installedURL?.lastPathComponent, "MindRoom Chat.app")
    }

    @MainActor
    func testFailedInstallShowsTheReasonAndCanBeRetried() async throws {
        var refuse = true
        var opened = 0
        let installer = try installer(FakeTools()) { _ in
            if refuse { throw ChatAppInstallError.untrusted("not ours") }
        }
        let store = ChatAppStore(installer: installer, locate: { nil }, open: { _ in opened += 1 })

        store.openOrInstall()
        await finished(store)

        XCTAssertEqual(store.error, ChatAppInstallError.untrusted("not ours").errorDescription)
        XCTAssertTrue(store.error?.contains("was not installed") == true)
        XCTAssertNil(store.installedURL)
        XCTAssertEqual(opened, 0)

        refuse = false
        store.openOrInstall()
        XCTAssertNil(store.error)
        await finished(store)
        XCTAssertNotNil(store.installedURL)
        XCTAssertEqual(opened, 1)
    }

    @MainActor
    func testMenuInstallsWhenMissingAndOpensWhenPresent() async throws {
        var opened = 0
        let store = ChatAppStore(installer: try installer(FakeTools()), locate: { nil }, open: { _ in opened += 1 })
        let controller = StatusMenuController(
            runner: MindRoomCommandRunner(processRunner: { _, _ in CommandResult(exitCode: 0, output: "") }),
            desktop: DesktopControlStore(), chatApp: store
        )
        var shown: [AppSection?] = []
        controller.showWindow = { shown.append($0) }
        let menu = NSMenu()

        controller.menuNeedsUpdate(menu)
        let install = try XCTUnwrap(menu.items.first { $0.title == "Install MindRoom Chat…" })
        XCTAssertFalse(menu.items.contains { $0.title == "Open MindRoom Chat" })
        XCTAssertTrue(NSApplication.shared.sendAction(try XCTUnwrap(install.action), to: install.target, from: install))
        // Progress shows on Overview, so installing from the menu is never silent.
        XCTAssertEqual(shown, [.overview])

        controller.menuNeedsUpdate(menu)
        XCTAssertEqual(menu.items.first { $0.title == "Installing MindRoom Chat…" }?.isEnabled, false)
        await finished(store)
        XCTAssertEqual(opened, 1)

        controller.menuNeedsUpdate(menu)
        let open = try XCTUnwrap(menu.items.first { $0.title == "Open MindRoom Chat" })
        XCTAssertTrue(NSApplication.shared.sendAction(try XCTUnwrap(open.action), to: open.target, from: open))
        XCTAssertEqual(opened, 2)
        XCTAssertEqual(shown, [.overview], "Opening the installed app leaves MindRoom's window alone")
        XCTAssertNotNil(menu.items.first { $0.title == "Open Chat" }, "The embedded chat stays")
    }

    /// Opt-in: the real release, image, copy, and signature check, into a folder standing in for /Applications.
    func testRealReleaseInstallsIntoGivenFolder() async throws {
        guard let target = ProcessInfo.processInfo.environment["MINDROOM_CHAT_INSTALL_E2E_DIR"] else {
            throw XCTSkip("Set MINDROOM_CHAT_INSTALL_E2E_DIR to an empty folder to install the real MindRoom Chat there.")
        }
        var installer = ChatAppInstaller()
        installer.applications = URL(fileURLWithPath: target)
        var stages: [String] = []

        let installed = try await installer.install { stages.append($0) }

        print("Installed \(installed.path) after stages \(stages)")
        XCTAssertEqual(installed.path, "\(target)/MindRoom Chat.app")
        XCTAssertEqual(Bundle(url: installed)?.bundleIdentifier, "chat.mindroom.app")
        XCTAssertNoThrow(try ChatAppSignature.verify(installed))
        XCTAssertEqual(try FileManager.default.contentsOfDirectory(atPath: target), ["MindRoom Chat.app"])
    }
}
