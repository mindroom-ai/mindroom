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

    /// A runnable, unsigned bundle with the given identifier; `marker` tells copies apart.
    @discardableResult
    private static func makeApp(at app: URL, identifier: String = "chat.mindroom.app", marker: String = "chat") throws -> URL {
        let files = FileManager.default
        try files.createDirectory(at: app.appendingPathComponent("Contents/MacOS"), withIntermediateDirectories: true)
        try files.createDirectory(at: app.appendingPathComponent("Contents/Resources"), withIntermediateDirectories: true)
        try files.copyItem(atPath: "/usr/bin/true", toPath: app.appendingPathComponent("Contents/MacOS/Chat").path)
        let info = "<plist version=\"1.0\"><dict><key>CFBundleIdentifier</key><string>\(identifier)</string>"
            + "<key>CFBundleExecutable</key><string>Chat</string><key>CFBundlePackageType</key><string>APPL</string></dict></plist>"
        try Data(info.utf8).write(to: app.appendingPathComponent("Contents/Info.plist"))
        try Data(marker.utf8).write(to: app.appendingPathComponent("Contents/Resources/marker"))
        return app
    }

    private func marker(_ app: URL) -> String? {
        try? String(contentsOf: app.appendingPathComponent("Contents/Resources/marker"))
    }

    /// Bundles that claim to be MindRoom Chat: one unsigned, and one with the right identifier and
    /// an intact signature from a signer that is not MindRoom.
    private func impostors() throws -> [URL] {
        let unsigned = try Self.makeApp(at: folder.appendingPathComponent("Unsigned.app"))
        let selfSigned = try Self.makeApp(at: folder.appendingPathComponent("SelfSigned.app"))
        let codesign = MindRoomCommandProcess().run(MindRoomCommandInvocation(
            executableURL: URL(fileURLWithPath: "/usr/bin/codesign"),
            arguments: ["--force", "--sign", "-", "--identifier", "chat.mindroom.app", selfSigned.path], environment: [:]
        ))
        XCTAssertTrue(codesign.isSuccess, codesign.output)
        return [unsigned, selfSigned]
    }

    func testRefusesUnsignedAndForeignApps() throws {
        // Calculator is validly signed by Apple, but not MindRoom Chat.
        for app in try impostors() + [URL(fileURLWithPath: "/System/Applications/Calculator.app")] {
            for verify in [ChatAppSignature.verify, ChatAppSignature.verifyInstalled] {
                XCTAssertThrowsError(try verify(app), app.path) { error in
                    guard case ChatAppInstallError.untrusted = error else { return XCTFail("\(error)") }
                }
            }
        }
    }

    func testFollowsRedirectsOnlyToGitHub() {
        let task = URLSession.shared.dataTask(with: ChatAppRelease.listURL)
        let redirect = HTTPURLResponse(url: ChatAppRelease.listURL, statusCode: 302, httpVersion: nil, headerFields: nil)!
        for (url, allowed) in [
            ("https://release-assets.githubusercontent.com/github-production-release-asset/1/MindRoom-Chat.dmg", true),
            ("https://objects.githubusercontent.com/github-production-release-asset/1", true),
            ("\(Self.downloads)/v1/MindRoom-Chat.dmg", true),
            ("http://github.com/mindroom-ai/mindroom-chat", false),
            ("https://example.org/MindRoom-Chat.dmg", false),
            ("https://github.com.example.org/MindRoom-Chat.dmg", false),
        ] {
            var followed: URLRequest?
            GitHubRedirects().urlSession(
                URLSession.shared, task: task, willPerformHTTPRedirection: redirect, newRequest: URLRequest(url: URL(string: url)!)
            ) { followed = $0 }
            XCTAssertEqual(followed != nil, allowed, url)
        }
    }

    // MARK: Install

    /// Stands in for hdiutil and ditto: attach puts `MindRoom.app` in the mount point, ditto copies.
    private final class FakeTools {
        var calls: [String] = []
        var detached: [String] = []
        var downloadFolder: URL?
        var marker = "chat"
        /// Makes the image's app a link to this folder, outside the image.
        var linkTarget: URL?
        var detachFails = false

        func run(_ tool: String, _ arguments: [String]) -> CommandResult {
            let files = FileManager.default
            calls.append("\(URL(fileURLWithPath: tool).lastPathComponent) \(URL(fileURLWithPath: arguments[0]).lastPathComponent)")
            do {
                if arguments[0] == "attach" {
                    let app = URL(fileURLWithPath: arguments.last!).appendingPathComponent("MindRoom.app")
                    if let linkTarget {
                        try files.createSymbolicLink(at: app, withDestinationURL: linkTarget)
                    } else {
                        try ChatAppInstallTests.makeApp(at: app, marker: marker)
                    }
                    // What hdiutil prints: the whole disk first, then the partition with its mount point.
                    return CommandResult(exitCode: 0, output: "/dev/disk99          \tGUID_partition_scheme          \t\n"
                        + "/dev/disk99s1        \tApple_HFS                      \t\(arguments.last!)\n")
                } else if arguments[0] == "detach" {
                    detached.append(arguments[1])
                    if detachFails { return CommandResult(exitCode: 1, output: "hdiutil: couldn't unmount: Resource busy") }
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
        try FileManager.default.createDirectory(at: applications, withIntermediateDirectories: true, attributes: [.posixPermissions: 0o755])
        let list = try JSONSerialization.data(withJSONObject: [release("v1", assets: [("MindRoom.dmg", "uploaded")])])
        return ChatAppInstaller(
            applications: applications, home: folder.appendingPathComponent("home"),
            // Never this Mac's own copy, nor its Applications folders.
            locate: { nil },
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
        var found = try ["Applications", "home/Applications"].map { folder.appendingPathComponent($0).path }
            .filter { files.fileExists(atPath: $0) }.flatMap { try files.contentsOfDirectory(atPath: $0) }
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

    func testTrustsOnlyFoldersThatOthersCannotChange() throws {
        // A standard /Applications: root's, writable by the admin group.
        XCTAssertTrue(ChatAppInstaller.isTrusted(URL(fileURLWithPath: "/Applications")))
        XCTAssertFalse(ChatAppInstaller.isTrusted(URL(fileURLWithPath: "/private/tmp")), "Writable by everyone")
        XCTAssertFalse(ChatAppInstaller.isTrusted(folder.appendingPathComponent("missing")))
        let applications = try installer(FakeTools()).applications
        for (mode, trusted) in [(0o755, true), (0o700, true), (0o757, false), (0o777, false), (0o1777, false)] {
            try FileManager.default.setAttributes([.posixPermissions: mode], ofItemAtPath: applications.path)
            XCTAssertEqual(ChatAppInstaller.isTrusted(applications), trusted, String(mode, radix: 8))
        }
        // Group-writable is trusted only for the admin group, which a temporary folder never has.
        try FileManager.default.setAttributes([.posixPermissions: 0o775], ofItemAtPath: applications.path)
        let group = try XCTUnwrap(try FileManager.default.attributesOfItem(atPath: applications.path)[.groupOwnerAccountID] as? Int)
        XCTAssertEqual(ChatAppInstaller.isTrusted(applications), group == 80)
        let link = folder.appendingPathComponent("Link")
        try FileManager.default.createSymbolicLink(at: link, withDestinationURL: URL(fileURLWithPath: "/Applications"))
        XCTAssertFalse(ChatAppInstaller.isTrusted(link), "A link can be pointed elsewhere")
    }

    private func chmod(_ arguments: String..., on folder: URL) {
        let result = ChatAppInstaller.runTool("/bin/chmod", arguments + [folder.path])
        XCTAssertTrue(result.isSuccess, result.output)
    }

    func testTrustsNoFolderWithAnAccessControlList() throws {
        let applications = try installer(FakeTools()).applications
        XCTAssertTrue(ChatAppInstaller.isTrusted(applications))
        // An entry can grant what the mode denies. Entries are not read: any one of them is enough.
        for entry in ["everyone allow add_file,add_subdirectory,delete_child", "everyone deny delete"] {
            chmod("+a", entry, on: applications)
            XCTAssertFalse(ChatAppInstaller.isTrusted(applications), entry)
            chmod("-N", on: applications)
            XCTAssertTrue(ChatAppInstaller.isTrusted(applications), "Without its list the folder is trusted again")
        }
    }

    func testSkipsAnApplicationsFolderWithAnAccessControlList() async throws {
        let tools = FakeTools()
        let installer = try installer(tools)
        chmod("+a", "everyone allow add_file,add_subdirectory,delete_child", on: installer.applications)

        let installed = try await installer.install { _ in }

        XCTAssertEqual(installed.path, folder.appendingPathComponent("home/Applications/MindRoom Chat.app").path)
        XCTAssertEqual(try FileManager.default.contentsOfDirectory(atPath: installer.applications.path), [], "Nothing is staged there either")
        XCTAssertEqual(try leftovers(tools), [])
    }

    func testSkipsAnApplicationsFolderThatOthersCanChange() async throws {
        let tools = FakeTools()
        var installer = try installer(tools)
        try FileManager.default.setAttributes([.posixPermissions: 0o777], ofItemAtPath: installer.applications.path)

        let installed = try await installer.install { _ in }

        XCTAssertEqual(installed.path, folder.appendingPathComponent("home/Applications/MindRoom Chat.app").path)
        XCTAssertEqual(try FileManager.default.contentsOfDirectory(atPath: installer.applications.path), [], "Nothing is staged there either")

        // With the home folder open to others as well, there is nowhere to install.
        try FileManager.default.removeItem(at: installed)
        try FileManager.default.setAttributes([.posixPermissions: 0o777], ofItemAtPath: installed.deletingLastPathComponent().path)
        var fetched = false
        installer.fetch = { _ in fetched = true; return Data() }
        tools.calls = []
        do {
            _ = try await installer.install { _ in }
            XCTFail("Installed into a folder that others can change")
        } catch {
            let parent = installed.deletingLastPathComponent().path
            XCTAssertEqual(error as? ChatAppInstallError, .failed("\(parent) is not a folder that only you and this Mac's administrators can change."))
        }
        XCTAssertFalse(fetched, "Nothing is downloaded")
        XCTAssertEqual(tools.calls, [])
        XCTAssertEqual(try leftovers(tools), [])
    }

    func testStopsAToolThatDoesNotFinish() throws {
        // Ignores the request to stop, so only the kill ends it.
        var started = Date()
        let pidFile = folder.appendingPathComponent("pid")
        let stuck = ChatAppInstaller.runTool("/bin/sh", ["-c", "trap '' TERM; echo $$ > '\(pidFile.path)'; sleep 20; echo late"], limit: 0.5, grace: 0.5)
        XCTAssertEqual(stuck, CommandResult(exitCode: 124, output: "sh did not finish in time."))
        XCTAssertLessThan(Date().timeIntervalSince(started), 5)
        let pid = try XCTUnwrap(Int32(try String(contentsOf: pidFile, encoding: .utf8).trimmingCharacters(in: .whitespacesAndNewlines)))
        for _ in 0 ..< 40 where kill(pid, 0) == 0 { usleep(50000) }
        XCTAssertNotEqual(kill(pid, 0), 0, "The tool was killed")
        // A child that keeps the output open does not hold the wait.
        started = Date()
        let detached = ChatAppInstaller.runTool("/bin/sh", ["-c", "sleep 20 & echo done; exit 3"])
        XCTAssertEqual(detached, CommandResult(exitCode: 3, output: "done\n"))
        XCTAssertLessThan(Date().timeIntervalSince(started), 5)
        XCTAssertEqual(ChatAppInstaller.runTool("/nonexistent/tool", []).exitCode, 127)
    }

    func testStoppedAttachIsDetachedBeforeItsFolderGoes() async throws {
        for detachFails in [false, true] {
            let tools = FakeTools()
            tools.detachFails = detachFails
            var installer = try installer(tools)
            // hdiutil was stopped at its time limit: the image may be attached all the same.
            installer.run = { tool, arguments in
                arguments[0] == "attach" ? CommandResult(exitCode: 124, output: "hdiutil did not finish in time.") : tools.run(tool, arguments)
            }

            do {
                _ = try await installer.install { _ in }
                XCTFail("Installed from an image that did not attach")
            } catch {
                XCTAssertEqual(error as? ChatAppInstallError, .failed("hdiutil did not finish in time."))
            }

            let work = try XCTUnwrap(tools.downloadFolder)
            let mount = work.appendingPathComponent("image").path
            XCTAssertEqual(tools.detached, detachFails ? [mount, mount] : [mount])
            XCTAssertEqual(FileManager.default.fileExists(atPath: work.path), detachFails, "The folder goes only after a confirmed detach")
            try? FileManager.default.removeItem(at: work)
        }
    }

    /// With the real hdiutil: an attach that reports failure after mounting is found by its mount point.
    func testFailedAttachThatMountedIsDetached() async throws {
        let tools = FakeTools()
        var installer = try installer(tools)
        installer.download = { _, file in
            tools.downloadFolder = file.deletingLastPathComponent()
            let created = ChatAppInstaller.runTool("/usr/bin/hdiutil", ["create", "-size", "4m", "-fs", "HFS+", "-volname", "MindRoomChatTest", file.path])
            XCTAssertTrue(created.isSuccess, created.output)
        }
        var attached = false
        installer.run = { tool, arguments in
            let result = ChatAppInstaller.runTool(tool, arguments)
            guard arguments[0] == "attach" else { return result }
            XCTAssertTrue(result.isSuccess, result.output)
            attached = ChatAppInstaller.runTool("/sbin/mount", []).output.contains(tools.downloadFolder!.lastPathComponent)
            return CommandResult(exitCode: 1, output: "attach failed")
        }

        do {
            _ = try await installer.install { _ in }
            XCTFail("Installed from an image whose attach failed")
        } catch {
            XCTAssertEqual(error as? ChatAppInstallError, .failed("attach failed"))
        }

        XCTAssertTrue(attached, "The image was mounted")
        let mounted = ChatAppInstaller.runTool("/sbin/mount", []).output
        XCTAssertFalse(mounted.contains(try XCTUnwrap(tools.downloadFolder).lastPathComponent), "The image is detached")
        XCTAssertEqual(try leftovers(tools), [])
    }

    func testInstallsVerifiedAppAndCleansUp() async throws {
        let tools = FakeTools()
        var stage: URL?
        var stagePermissions: Int?
        var stages: [String] = []
        let installer = try installer(tools) { staged in
            stage = staged.deletingLastPathComponent()
            stagePermissions = try FileManager.default.attributesOfItem(atPath: stage!.path)[.posixPermissions] as? Int
        }

        let installed = try await installer.install { stages.append($0) }

        XCTAssertEqual(installed.path, folder.appendingPathComponent("Applications/MindRoom Chat.app").path)
        XCTAssertEqual(marker(installed), "chat")
        // The copy that is checked is the copy that is installed, in a folder of this install's
        // own on the destination's volume, closed to other users, before it gets its name.
        XCTAssertEqual(stage?.deletingLastPathComponent().path, installer.applications.path)
        XCTAssertTrue(stage?.lastPathComponent.hasPrefix(".MindRoomChatInstall-") == true)
        XCTAssertEqual(stagePermissions, 0o700)
        XCTAssertEqual(tools.calls, ["hdiutil attach", "ditto MindRoom.app", "hdiutil detach"])
        XCTAssertEqual(tools.detached, ["/dev/disk99"], "The attached device is detached, whatever its mount point became")
        XCTAssertEqual(try leftovers(tools), [])
        XCTAssertGreaterThanOrEqual(stages.count, 3)
    }

    /// Runs async work to its end from synchronous code, as another process beside this one would.
    private func runToEnd(_ work: @escaping () async -> Void) {
        let done = DispatchSemaphore(value: 0)
        Task.detached {
            await work()
            done.signal()
        }
        done.wait()
    }

    func testConcurrentInstallsKeepTheirOwnStage() async throws {
        let tools = FakeTools()
        let otherTools = FakeTools()
        otherTools.marker = "other"
        let other = try installer(otherTools) { _ in throw ChatAppInstallError.untrusted("not ours") }
        var otherError: Error?
        var verified: String?
        let installer = try installer(tools) { staged in
            verified = self.marker(staged)
            // After this copy was checked, a second install into the same folder stages its own
            // copy, is refused, and cleans up.
            self.runToEnd {
                do { _ = try await other.install { _ in } } catch { otherError = error }
            }
        }

        let installed = try await installer.install { _ in }

        XCTAssertEqual(otherError as? ChatAppInstallError, .untrusted("not ours"))
        XCTAssertEqual(otherTools.calls, ["hdiutil attach", "ditto MindRoom.app", "hdiutil detach"])
        XCTAssertEqual(verified, "chat")
        XCTAssertEqual(marker(installed), verified, "The published bytes are the checked bytes")
        XCTAssertEqual(try leftovers(tools), [])
    }

    func testRefusesAnAppThatIsALinkOutOfTheImage() async throws {
        let tools = FakeTools()
        tools.linkTarget = try Self.makeApp(at: folder.appendingPathComponent("Outside/MindRoom.app"))
        let installer = try installer(tools)

        do {
            _ = try await installer.install { _ in }
            XCTFail("A link out of the image was installed")
        } catch {
            XCTAssertEqual(error as? ChatAppInstallError, .failed("the download holds no app."))
        }

        XCTAssertEqual(tools.calls, ["hdiutil attach", "hdiutil detach"], "Nothing outside the image is copied")
        XCTAssertFalse(FileManager.default.fileExists(atPath: installer.destination.path))
        XCTAssertEqual(try leftovers(tools), [])
    }

    func testCopyNoticedDuringTheDownloadIsNotDuplicated() async throws {
        let tools = FakeTools()
        let elsewhere = try Self.makeApp(at: folder.appendingPathComponent("Elsewhere/MindRoom.app"))
        var registered = false
        // Launch Services notices a copy from another source while this install runs.
        var installer = try installer(tools) { _ in registered = true }
        installer.locate = { registered ? elsewhere : nil }

        do {
            _ = try await installer.install { _ in }
            XCTFail("A second copy was installed")
        } catch {
            XCTAssertEqual(error as? ChatAppInstallError, .alreadyExists(elsewhere.path))
        }

        XCTAssertFalse(FileManager.default.fileExists(atPath: installer.destination.path))
        XCTAssertEqual(try leftovers(tools), [])
    }

    func testFailedDetachKeepsTheMountFolder() async throws {
        let tools = FakeTools()
        tools.detachFails = true

        let installed = try await installer(tools).install { _ in }

        XCTAssertEqual(marker(installed), "chat")
        XCTAssertEqual(tools.detached, ["/dev/disk99", "/dev/disk99"], "A busy image is detached once more")
        let mount = try XCTUnwrap(tools.downloadFolder).appendingPathComponent("image")
        XCTAssertTrue(FileManager.default.fileExists(atPath: mount.path), "Nothing is deleted under a live attachment")
        try FileManager.default.removeItem(at: mount.deletingLastPathComponent())
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
        let existing = try Self.makeApp(at: folder.appendingPathComponent("Elsewhere/MindRoom.app"))
        var checked: [URL] = []
        var opened: [URL] = []
        var installer = try installer(FakeTools())
        installer.locate = { existing }
        installer.fetch = { _ in XCTFail("Installed although a copy exists"); return Data() }
        let store = ChatAppStore(installer: installer, verifyInstalled: { checked.append($0) }, open: { app, _ in opened.append(app) })

        XCTAssertEqual(store.installedURL, existing)
        store.openOrInstall()

        XCTAssertEqual(checked, [existing], "The copy that opens is the copy that was checked")
        XCTAssertEqual(opened, [existing])
        XCTAssertNil(store.progress)
        // Launch Services can name a copy that was since removed.
        try FileManager.default.removeItem(at: existing)
        store.refresh()
        XCTAssertNil(store.installedURL)
    }

    @MainActor
    func testNeverOpensACopyThatIsNotMindRoomChat() throws {
        for app in try impostors() {
            var installer = try installer(FakeTools())
            installer.locate = { app }
            installer.fetch = { _ in XCTFail("Installed beside an existing copy"); return Data() }
            var opened = 0
            // The real signature check: Launch Services naming a bundle proves nothing about it.
            let store = ChatAppStore(installer: installer, open: { _, _ in opened += 1 })
            let controller = StatusMenuController(
                runner: MindRoomCommandRunner(processRunner: { _, _ in CommandResult(exitCode: 0, output: "") }),
                desktop: DesktopControlStore(), chatApp: store
            )
            var shown: [AppSection?] = []
            controller.showWindow = { shown.append($0) }
            let menu = NSMenu()
            controller.menuNeedsUpdate(menu)
            let open = try XCTUnwrap(menu.items.first { $0.title == "Open MindRoom Chat" })

            XCTAssertTrue(NSApplication.shared.sendAction(try XCTUnwrap(open.action), to: open.target, from: open))

            XCTAssertEqual(opened, 0, app.lastPathComponent)
            XCTAssertTrue(store.error?.contains("was not opened") == true, store.error ?? "no error")
            XCTAssertTrue(store.error?.contains(app.path) == true)
            XCTAssertNil(store.progress)
            XCTAssertEqual(shown, [.overview], "The reason shows on Overview")
        }
    }

    @MainActor
    func testReportsACopyThatFailsToOpen() async throws {
        let existing = try Self.makeApp(at: folder.appendingPathComponent("Elsewhere/MindRoom Chat.app"))
        var installer = try installer(FakeTools())
        installer.locate = { existing }
        let store = ChatAppStore(installer: installer, verifyInstalled: { _ in }, open: { _, done in done(CocoaError(.executableLoad)) })
        let reported = expectation(description: "Launch failure reported")
        let subscription = store.$error.compactMap { $0 }.prefix(1).sink { _ in reported.fulfill() }

        store.openOrInstall()
        await fulfillment(of: [reported], timeout: 5)

        XCTAssertTrue(store.error?.hasPrefix("MindRoom Chat could not be opened: ") == true, store.error ?? "")
        withExtendedLifetime(subscription) {}
    }

    @MainActor
    func testFindsCopiesLaunchServicesMisses() throws {
        // Launch Services names nothing; the copy is where this app, Homebrew, or an old image put it.
        for path in ["Applications/MindRoom Chat.app", "home/Applications/MindRoom Chat.app",
                     "Applications/MindRoom.app", "home/Applications/MindRoom.app"] {
            let copy = try Self.makeApp(at: folder.appendingPathComponent(path))
            var opened: [String] = []
            var installer = try installer(FakeTools())
            installer.fetch = { _ in XCTFail("Installed although \(path) exists"); return Data() }
            let store = ChatAppStore(installer: installer, verifyInstalled: { _ in }, open: { app, _ in opened.append(app.path) })

            XCTAssertEqual(store.installedURL?.path, copy.path)
            store.openOrInstall()

            XCTAssertEqual(opened, [copy.path])
            XCTAssertNil(store.progress, "No install starts beside \(path)")
            try FileManager.default.removeItem(at: copy)
        }
        // `MindRoom.app` in Applications is normally this app, not the chat app.
        try Self.makeApp(at: folder.appendingPathComponent("Applications/MindRoom.app"), identifier: "chat.mindroom.menubar")
        XCTAssertNil(try installer(FakeTools()).existingCopy())
    }

    @MainActor
    func testInstallsThenOpensWhenMissing() async throws {
        let tools = FakeTools()
        var opened: [String] = []
        let store = ChatAppStore(installer: try installer(tools), open: { app, _ in opened.append(app.lastPathComponent) })
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
        let store = ChatAppStore(installer: installer, open: { _, _ in opened += 1 })

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
        let store = ChatAppStore(installer: try installer(FakeTools()), verifyInstalled: { _ in }, open: { _, _ in opened += 1 })
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
        // Only into an existing, empty, writable folder: nothing here may reach the real ~/Applications.
        guard (try? FileManager.default.contentsOfDirectory(atPath: target)) == [] else {
            return XCTFail("\(target) must be an existing, empty folder; nothing was installed.")
        }
        var installer = ChatAppInstaller()
        installer.applications = URL(fileURLWithPath: target)
        installer.home = folder.appendingPathComponent("home")
        // This Mac's own copy must neither stop the test nor be touched by it.
        installer.locate = { nil }
        guard installer.destination.path == "\(target)/MindRoom Chat.app" else {
            return XCTFail("\(target) is not writable by you alone; nothing was installed.")
        }
        var stages: [String] = []

        let installed = try await installer.install { stages.append($0) }

        print("Installed \(installed.path) after stages \(stages)")
        XCTAssertEqual(installed.path, "\(target)/MindRoom Chat.app")
        XCTAssertEqual(Bundle(url: installed)?.bundleIdentifier, "chat.mindroom.app")
        XCTAssertNoThrow(try ChatAppSignature.verify(installed))
        // The Developer ID branch of the check before opening, on the real build.
        let started = Date()
        XCTAssertNoThrow(try ChatAppSignature.verifyInstalled(installed))
        print("verifyInstalled took \(Int(Date().timeIntervalSince(started) * 1000)) ms")
        if let own = NSWorkspace.shared.urlForApplication(withBundleIdentifier: ChatAppSignature.bundleIdentifier) {
            print("This Mac's own copy \(own.path): \(Result { try ChatAppSignature.verifyInstalled(own) })")
        }
        XCTAssertFalse(FileManager.default.fileExists(atPath: installer.home.path), "Nothing fell back to a home folder")
        XCTAssertEqual(try FileManager.default.contentsOfDirectory(atPath: target), ["MindRoom Chat.app"])
    }
}
