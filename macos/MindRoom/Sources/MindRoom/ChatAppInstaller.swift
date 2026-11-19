import Foundation

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
    var fetch: (URL) async throws -> Data = { try await URLSession.shared.data(from: $0).0 }
    var download: (_ source: URL, _ file: URL) async throws -> Void = { source, file in
        let (temporary, response) = try await URLSession.shared.download(from: source)
        guard (response as? HTTPURLResponse)?.statusCode == 200 else { throw ChatAppInstallError.noRelease }
        try FileManager.default.moveItem(at: temporary, to: file)
    }
    var run: (_ tool: String, _ arguments: [String]) -> CommandResult = { tool, arguments in
        MindRoomCommandProcess().run(MindRoomCommandInvocation(
            executableURL: URL(fileURLWithPath: tool), arguments: arguments, environment: [:]
        ))
    }
    var verify: (URL) throws -> Void = ChatAppSignature.verify

    /// `/Applications` when this user can write there, else `~/Applications`. Always under the chat
    /// app's own name: older images hold it as `MindRoom.app`, the name of this app.
    var destination: URL {
        let writable = FileManager.default.isWritableFile(atPath: applications.path)
        return (writable ? applications : home.appendingPathComponent("Applications")).appendingPathComponent(Self.appName)
    }

    func install(progress: @escaping (String) -> Void) async throws -> URL {
        let files = FileManager.default
        let destination = destination
        guard !files.fileExists(atPath: destination.path) else { throw ChatAppInstallError.alreadyExists(destination.path) }
        progress("Finding the newest MindRoom Chat…")
        guard let source = ChatAppRelease.downloadURL(fromReleaseList: try await fetch(ChatAppRelease.listURL)) else {
            throw ChatAppInstallError.noRelease
        }
        let work = files.temporaryDirectory.appendingPathComponent("MindRoomChatInstall-\(UUID().uuidString)")
        let image = work.appendingPathComponent("MindRoom-Chat.dmg")
        let mount = work.appendingPathComponent("image")
        // Hidden and without `.app`, so nothing treats the unchecked copy as an application.
        let staged = destination.deletingLastPathComponent().appendingPathComponent(".MindRoom Chat.installing")
        var attached = false
        defer {
            if attached { _ = run("/usr/bin/hdiutil", ["detach", mount.path, "-force"]) }
            try? files.removeItem(at: staged)
            try? files.removeItem(at: work)
        }
        try files.createDirectory(at: mount, withIntermediateDirectories: true)
        try files.createDirectory(at: staged.deletingLastPathComponent(), withIntermediateDirectories: true)
        try? files.removeItem(at: staged)

        progress("Downloading MindRoom Chat…")
        try await download(source, image)
        progress("Checking MindRoom Chat…")
        // `diskutil image attach` replaces this from macOS 15, but macOS 14 has only hdiutil.
        try check(run("/usr/bin/hdiutil", ["attach", image.path, "-readonly", "-nobrowse", "-noautoopen", "-mountpoint", mount.path]))
        attached = true
        guard let app = try files.contentsOfDirectory(at: mount, includingPropertiesForKeys: nil)
            .first(where: { $0.pathExtension == "app" }) else {
            throw ChatAppInstallError.failed("the download holds no app.")
        }
        try check(run("/usr/bin/ditto", [app.path, staged.path]))
        // The copy that is checked is the copy that gets installed.
        try verify(staged)
        progress("Installing MindRoom Chat…")
        do {
            // A move fails rather than replaces, should a bundle have appeared meanwhile.
            try files.moveItem(at: staged, to: destination)
        } catch {
            throw files.fileExists(atPath: destination.path)
                ? ChatAppInstallError.alreadyExists(destination.path) : error
        }
        return destination
    }

    private func check(_ result: CommandResult) throws {
        guard result.isSuccess else { throw ChatAppInstallError.failed(result.condensedOutput) }
    }
}
