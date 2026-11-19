import AppKit

/// Whether MindRoom Chat is on this Mac, and the one install MindRoom may run for it.
@MainActor
final class ChatAppStore: ObservableObject {
    static let shared = ChatAppStore()

    @Published private(set) var installedURL: URL?
    /// The current stage of a running install.
    @Published private(set) var progress: String?
    @Published private(set) var error: String?

    private let installer: ChatAppInstaller
    private let verifyInstalled: (URL) throws -> Void
    private let open: (URL, @escaping (Error?) -> Void) -> Void

    init(
        installer: ChatAppInstaller = ChatAppInstaller(),
        verifyInstalled: @escaping (URL) throws -> Void = ChatAppSignature.verifyInstalled,
        open: @escaping (URL, @escaping (Error?) -> Void) -> Void = { app, done in
            NSWorkspace.shared.openApplication(at: app, configuration: .init()) { _, error in done(error) }
        }
    ) {
        self.installer = installer
        self.verifyInstalled = verifyInstalled
        self.open = open
        refresh()
    }

    /// Finds a copy without checking it: `openOrInstall` does that before it opens one.
    func refresh() {
        let found = installer.existingCopy()
        if found != installedURL { installedURL = found }
    }

    /// Opens MindRoom Chat, installing it first when this Mac has none. Only a click calls this.
    func openOrInstall() {
        refresh()
        guard progress == nil else { return }
        error = nil
        if let installedURL {
            // A bundle identifier or a path only finds a candidate; its signature says whose app it is.
            guard (try? verifyInstalled(installedURL)) != nil else {
                error = "\(installedURL.path) is not the MindRoom Chat that MindRoom publishes, so it was not opened. Remove it to install MindRoom Chat here."
                return
            }
            return launch(installedURL)
        }
        progress = "Starting…"
        Task {
            do {
                let installed = try await installer.install { stage in
                    Task { @MainActor in if self.progress != nil { self.progress = stage } }
                }
                installedURL = installed
                launch(installed)
            } catch {
                self.error = error.localizedDescription
            }
            progress = nil
        }
    }

    private func launch(_ app: URL) {
        open(app) { failure in
            guard let failure else { return }
            Task { @MainActor in self.error = "MindRoom Chat could not be opened: \(failure.localizedDescription)" }
        }
    }
}
