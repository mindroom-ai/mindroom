import AppKit

enum ChatAppInstallError: LocalizedError, Equatable {
    case noRelease
    case alreadyExists(String)
    case untrusted(String)
    case failed(String)

    var errorDescription: String? {
        switch self {
        case .noRelease:
            return "No MindRoom Chat download was found on GitHub. Check your connection and try again in a few minutes."
        case .alreadyExists(let path):
            return "\(path) already exists and was left as it is. Open or remove it, then try again."
        case .untrusted(let reason):
            return "The downloaded app is not the notarized MindRoom Chat from MindRoom, so it was not installed. (\(reason))"
        case .failed(let reason):
            return "MindRoom Chat could not be installed: \(reason)"
        }
    }
}

/// Installs the released MindRoom Chat next to this app. MindRoom never updates or replaces a
/// copy afterwards: the chat app updates itself.
struct ChatAppInstaller {
    static let appName = "MindRoom Chat.app"

    var applications = URL(fileURLWithPath: "/Applications")
    var home = FileManager.default.homeDirectoryForCurrentUser
    var locate: () -> URL? = { NSWorkspace.shared.urlForApplication(withBundleIdentifier: ChatAppSignature.bundleIdentifier) }
    var fetch: (URL) async throws -> Data = { url in
        let (data, response) = try await URLSession.shared.data(from: url, delegate: GitHubRedirects())
        guard (response as? HTTPURLResponse)?.statusCode == 200 else { throw ChatAppInstallError.noRelease }
        return data
    }
    var download: (_ source: URL, _ file: URL) async throws -> Void = { source, file in
        let (temporary, response) = try await URLSession.shared.download(from: source, delegate: GitHubRedirects())
        guard (response as? HTTPURLResponse)?.statusCode == 200 else { throw ChatAppInstallError.noRelease }
        try FileManager.default.moveItem(at: temporary, to: file)
    }
    var run: (_ tool: String, _ arguments: [String]) -> CommandResult = { ChatAppInstaller.runTool($0, $1) }
    var verify: (URL) throws -> Void = ChatAppSignature.verify

    /// Runs a tool for at most `limit` seconds, then stops it (exit code 124): a hung tool must not hold the install,
    /// and with it quitting, for good. Output goes to a file, so a child that keeps a pipe open cannot hold it either.
    static func runTool(_ tool: String, _ arguments: [String], limit: TimeInterval = 300, grace: TimeInterval = 5) -> CommandResult {
        let output = FileManager.default.temporaryDirectory.appendingPathComponent("MindRoomChatInstall-\(UUID().uuidString).log")
        FileManager.default.createFile(atPath: output.path, contents: nil)
        defer { try? FileManager.default.removeItem(at: output) }
        let process = Process()
        let exited = DispatchSemaphore(value: 0)
        process.executableURL = URL(fileURLWithPath: tool)
        process.arguments = arguments
        process.standardInput = FileHandle.nullDevice
        process.standardOutput = FileHandle(forWritingAtPath: output.path)
        process.standardError = process.standardOutput
        process.terminationHandler = { _ in exited.signal() }
        do { try process.run() } catch { return CommandResult(exitCode: 127, output: error.localizedDescription) }
        guard exited.wait(timeout: .now() + limit) == .timedOut else {
            return CommandResult(exitCode: process.terminationStatus, output: String(decoding: (try? Data(contentsOf: output)) ?? Data(), as: UTF8.self))
        }
        process.terminate()
        if exited.wait(timeout: .now() + grace) == .timedOut { kill(process.processIdentifier, SIGKILL) }
        return CommandResult(exitCode: 124, output: "\(URL(fileURLWithPath: tool).lastPathComponent) did not finish in time.")
    }

    /// Whether only root, this user, and this Mac's administrators (group 80) can change `folder`, as with a standard
    /// `/Applications`. Anyone else who can write there could swap the copy's folder before the copy is in place.
    static func isTrusted(_ folder: URL) -> Bool {
        var info = stat()
        guard lstat(folder.path, &info) == 0, info.st_mode & S_IFMT == S_IFDIR else { return false }
        // An access control list can grant what the mode denies. Its entries are not read: any list, or not
        // being able to tell that there is none (which reads as ENOENT), makes the folder untrusted.
        let (list, error) = folder.path.withCString { (acl_get_file($0, ACL_TYPE_EXTENDED), errno) }
        if let list { acl_free(UnsafeMutableRawPointer(list)) }
        guard list == nil, error == ENOENT else { return false }
        return (info.st_uid == 0 || info.st_uid == getuid()) && info.st_mode & S_IWOTH == 0
            && (info.st_mode & S_IWGRP == 0 || info.st_gid == 80)
    }

    /// A MindRoom Chat that is already on this Mac, from any source (disk image, Homebrew, App
    /// Store): they share the bundle identifier. Launch Services can miss a copy, so the places
    /// this app and Homebrew install to are looked at too; `MindRoom.app` is the chat app's old
    /// name there, and otherwise this app.
    func existingCopy() -> URL? {
        let known = [applications, home.appendingPathComponent("Applications")].flatMap { folder in
            [Self.appName, "MindRoom.app"].map { folder.appendingPathComponent($0) }
        }.filter { app in
            let info = NSDictionary(contentsOf: app.appendingPathComponent("Contents/Info.plist"))
            return info?["CFBundleIdentifier"] as? String == ChatAppSignature.bundleIdentifier
        }
        return ([locate()].compactMap { $0 } + known).first { FileManager.default.fileExists(atPath: $0.path) }
    }

    /// `/Applications` when this user can write there and it is trusted, else `~/Applications`. Always under the
    /// chat app's own name: older images hold it as `MindRoom.app`, the name of this app.
    var destination: URL {
        let writable = FileManager.default.isWritableFile(atPath: applications.path) && Self.isTrusted(applications)
        return (writable ? applications : home.appendingPathComponent("Applications")).appendingPathComponent(Self.appName)
    }

    func install(progress: @escaping (String) -> Void) async throws -> URL {
        let files = FileManager.default
        let destination = destination
        let parent = destination.deletingLastPathComponent()
        try refuseExistingCopy(at: destination)
        try files.createDirectory(at: parent, withIntermediateDirectories: true, attributes: [.posixPermissions: 0o755])
        guard Self.isTrusted(parent) else { throw ChatAppInstallError.failed("\(parent.path) is not a folder that only you and this Mac's administrators can change.") }
        progress("Finding the newest MindRoom Chat…")
        guard let source = ChatAppRelease.downloadURL(fromReleaseList: try await fetch(ChatAppRelease.listURL)) else {
            throw ChatAppInstallError.noRelease
        }
        let work = files.temporaryDirectory.appendingPathComponent("MindRoomChatInstall-\(UUID().uuidString)")
        let image = work.appendingPathComponent("MindRoom-Chat.dmg")
        let mount = work.appendingPathComponent("image")
        // This install's own folder on the destination's volume, closed to other users, so nothing changes the copy
        // between its check and its move into place. No `.app`: the unchecked copy is no application to anything.
        let stage = parent.appendingPathComponent(".MindRoomChatInstall-\(UUID().uuidString)")
        let staged = stage.appendingPathComponent("MindRoom Chat")
        var device: String?
        defer {
            // Once more when the image is busy. A live attachment keeps its folder: deleting under it reaches into the image.
            let detached = device.map { device in
                (0 ..< 2).contains { _ in run("/usr/bin/hdiutil", ["detach", device, "-force"]).isSuccess }
            } ?? true
            if detached { try? files.removeItem(at: work) }
            try? files.removeItem(at: stage)
        }
        try files.createDirectory(at: mount, withIntermediateDirectories: true)
        try files.createDirectory(at: stage, withIntermediateDirectories: false, attributes: [.posixPermissions: 0o700])

        progress("Downloading MindRoom Chat…")
        try await download(source, image)
        progress("Checking MindRoom Chat…")
        // macOS 27 points to `diskutil image attach` instead; macOS 14, which this app supports, has hdiutil.
        let attach = run("/usr/bin/hdiutil", ["attach", image.path, "-readonly", "-nobrowse", "-noautoopen", "-mountpoint", mount.path])
        // A failed attach can leave the image mounted, and a stopped one may have: then the work folder stays unless a detach succeeds.
        let volume = { (url: URL) in (try? files.attributesOfItem(atPath: url.path))?[.systemNumber] as? Int }
        device = attach.output.split(whereSeparator: \.isWhitespace).first { $0.hasPrefix("/dev/disk") }.map(String.init)
            ?? (attach.exitCode == 124 || volume(mount) != volume(work) ? mount.path : nil)
        try check(attach)
        // Only a real folder of the image: ditto would follow a link to somewhere else.
        guard let app = try files.contentsOfDirectory(at: mount, includingPropertiesForKeys: [.isDirectoryKey])
            .first(where: { $0.pathExtension == "app" }),
            try app.resourceValues(forKeys: [.isDirectoryKey]).isDirectory == true else {
            throw ChatAppInstallError.failed("the download holds no app.")
        }
        try check(run("/usr/bin/ditto", [app.path, staged.path]))
        // The copy that is checked is the copy that gets installed.
        try verify(staged)
        progress("Installing MindRoom Chat…")
        // A copy may have arrived, or Launch Services may have noticed one, during the download.
        try refuseExistingCopy(at: destination)
        // In one step, and never onto something that is there.
        guard renamex_np(staged.path, destination.path, UInt32(RENAME_EXCL)) == 0 else {
            throw errno == EEXIST ? ChatAppInstallError.alreadyExists(destination.path) : .failed(String(cString: strerror(errno)))
        }
        return destination
    }

    private func refuseExistingCopy(at destination: URL) throws {
        if let existing = existingCopy() ?? (FileManager.default.fileExists(atPath: destination.path) ? destination : nil) {
            throw ChatAppInstallError.alreadyExists(existing.path)
        }
    }

    private func check(_ result: CommandResult) throws {
        guard result.isSuccess else { throw ChatAppInstallError.failed(result.condensedOutput) }
    }
}
