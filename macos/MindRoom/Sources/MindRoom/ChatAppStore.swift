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
    private let locate: () -> URL?
    private let open: (URL) -> Void

    init(
        installer: ChatAppInstaller = ChatAppInstaller(),
        locate: @escaping () -> URL? = {
            NSWorkspace.shared.urlForApplication(withBundleIdentifier: ChatAppSignature.bundleIdentifier)
        },
        open: @escaping (URL) -> Void = { NSWorkspace.shared.openApplication(at: $0, configuration: .init()) }
    ) {
        self.installer = installer
        self.locate = locate
        self.open = open
        refresh()
    }

    /// A copy from any source counts (disk image, Homebrew, App Store): they share the bundle
    /// identifier. A copy this app just installed counts before Launch Services has noticed it.
    func refresh() {
        let found = [locate(), installedURL].compactMap { $0 }
            .first { FileManager.default.fileExists(atPath: $0.path) }
        if found != installedURL { installedURL = found }
    }

    /// Opens MindRoom Chat, installing it first when this Mac has none. Only a click calls this.
    func openOrInstall() {
        refresh()
        if let installedURL { return open(installedURL) }
        guard progress == nil else { return }
        error = nil
        progress = "Starting…"
        Task {
            do {
                let installed = try await installer.install { stage in
                    Task { @MainActor in if self.progress != nil { self.progress = stage } }
                }
                installedURL = installed
                open(installed)
            } catch {
                self.error = error.localizedDescription
            }
            progress = nil
        }
    }
}
