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
    var run: (_ tool: String, _ arguments: [String]) -> CommandResult = { tool, arguments in
        let process = MindRoomCommandProcess()
        // A hung tool must not hold the install, and with it quitting, for good.
        DispatchQueue.global().asyncAfter(deadline: .now() + 300) { process.cancel() }
        return process.run(MindRoomCommandInvocation(
            executableURL: URL(fileURLWithPath: tool), arguments: arguments, environment: [:]
        ))
    }
    var verify: (URL) throws -> Void = ChatAppSignature.verify

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

    /// `/Applications` when this user can write there, else `~/Applications`. Always under the chat
    /// app's own name: older images hold it as `MindRoom.app`, the name of this app.
    var destination: URL {
        let writable = FileManager.default.isWritableFile(atPath: applications.path)
        return (writable ? applications : home.appendingPathComponent("Applications")).appendingPathComponent(Self.appName)
    }

    func install(progress: @escaping (String) -> Void) async throws -> URL {
        let files = FileManager.default
        let destination = destination
        try refuseExistingCopy(at: destination)
        progress("Finding the newest MindRoom Chat…")
        guard let source = ChatAppRelease.downloadURL(fromReleaseList: try await fetch(ChatAppRelease.listURL)) else {
            throw ChatAppInstallError.noRelease
        }
        let work = files.temporaryDirectory.appendingPathComponent("MindRoomChatInstall-\(UUID().uuidString)")
        let image = work.appendingPathComponent("MindRoom-Chat.dmg")
        let mount = work.appendingPathComponent("image")
        // This install's own folder on the destination's volume, closed to other users, so nothing changes the copy
        // between its check and its move into place. No `.app`: the unchecked copy is no application to anything.
        let stage = destination.deletingLastPathComponent().appendingPathComponent(".MindRoomChatInstall-\(UUID().uuidString)")
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
        try files.createDirectory(at: stage.deletingLastPathComponent(), withIntermediateDirectories: true)
        try files.createDirectory(at: stage, withIntermediateDirectories: false, attributes: [.posixPermissions: 0o700])

        progress("Downloading MindRoom Chat…")
        try await download(source, image)
        progress("Checking MindRoom Chat…")
        // macOS 27 points to `diskutil image attach` instead; macOS 14, which this app supports, has hdiutil.
        let attach = run("/usr/bin/hdiutil", ["attach", image.path, "-readonly", "-nobrowse", "-noautoopen", "-mountpoint", mount.path])
        try check(attach)
        device = attach.output.split(whereSeparator: \.isWhitespace).first { $0.hasPrefix("/dev/disk") }.map(String.init) ?? mount.path
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
