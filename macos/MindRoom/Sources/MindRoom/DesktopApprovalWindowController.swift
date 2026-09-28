import AppKit
import Combine
import SwiftUI

/// One locally owned approval panel. Remote requests may reveal it, but never activate the app.
@MainActor
final class DesktopApprovalWindowController: NSWindowController {
    static let shared = DesktopApprovalWindowController()

    private let store: DesktopControlStore
    private var subscription: AnyCancellable?
    private var expiryTimer: Timer?
    private var lastPresentedRequestID: String?

    init(store: DesktopControlStore? = nil) {
        self.store = store ?? .shared
        super.init(window: nil)
    }

    required init?(coder: NSCoder) { fatalError("init(coder:) has not been implemented") }

    func start() {
        guard subscription == nil else { return }
        subscription = store.$status
            .receive(on: DispatchQueue.main)
            .sink { [weak self] status in self?.update(status) }
        let timer = Timer(timeInterval: 1, repeats: true) { [weak self] _ in
            MainActor.assumeIsolated {
                guard let self else { return }
                self.update(self.store.status)
            }
        }
        RunLoop.main.add(timer, forMode: .common)
        expiryTimer = timer
    }

    func stop() {
        subscription = nil
        expiryTimer?.invalidate()
        expiryTimer = nil
        lastPresentedRequestID = nil
        close()
    }

    /// An explicit menu-bar review may activate the panel, including after its close button was used.
    func showPending() {
        guard let request = pendingRequest(in: store.status) else { return }
        lastPresentedRequestID = request.requestID
        prepareWindow()
        window?.makeKeyAndOrderFront(nil)
    }

    private func update(_ status: DesktopStatus) {
        guard let request = pendingRequest(in: status) else {
            lastPresentedRequestID = nil
            close()
            return
        }
        guard request.requestID != lastPresentedRequestID else { return }
        lastPresentedRequestID = request.requestID
        prepareWindow()
        window?.orderFrontRegardless()
    }

    private func pendingRequest(in status: DesktopStatus) -> DesktopShellRequest? {
        guard case let .pending(request) = status.shellApprovalState,
              request.expiresAtMilliseconds > Date().timeIntervalSince1970 * 1000 else { return nil }
        return request
    }

    private func prepareWindow() {
        guard window == nil else { return }
        let panel = NSPanel(
            contentRect: NSRect(x: 0, y: 0, width: 680, height: 540),
            styleMask: [.titled, .closable, .nonactivatingPanel],
            backing: .buffered, defer: false
        )
        panel.title = "Approve Shell Command"
        panel.isReleasedWhenClosed = false
        panel.isFloatingPanel = true
        panel.hidesOnDeactivate = false
        // A pending approval must remain visible and clickable over an app-modal folder picker.
        panel.worksWhenModal = true
        panel.level = .modalPanel
        panel.isExcludedFromWindowsMenu = false
        panel.collectionBehavior = [.canJoinAllSpaces, .fullScreenAuxiliary]
        panel.contentView = NSHostingView(rootView: DesktopApprovalPopup(store: store, dismiss: { [weak self] in self?.close() }))
        panel.center()
        window = panel
    }
}

private struct DesktopApprovalPopup: View {
    @ObservedObject var store: DesktopControlStore
    let dismiss: () -> Void

    var body: some View {
        VStack(alignment: .leading, spacing: 16) {
            HStack {
                MindRoomLogo().frame(width: 32, height: 32)
                VStack(alignment: .leading, spacing: 2) {
                    Text("MindRoom needs your approval").font(.title3.bold())
                    Text("Review what will run on this Mac.").foregroundStyle(.secondary)
                }
                Spacer()
            }
            ScrollView {
                DesktopShellApprovalView(store: store)
                    .id(store.status.shell.pending?.requestID)
                if let error = store.errorMessage {
                    Label(error, systemImage: "exclamationmark.triangle").foregroundStyle(.red)
                    if let recovery = store.recovery { Text(recovery).foregroundStyle(.secondary) }
                }
            }
            HStack {
                Text("Closing this window leaves the command waiting.").font(.caption).foregroundStyle(.secondary)
                Spacer()
                Button("Later", action: dismiss).keyboardShortcut(.cancelAction)
            }
        }
        .padding(20)
        .frame(width: 680, height: 540)
    }
}
