import AppKit
import Combine

@MainActor
final class StatusMenuController: NSObject, NSMenuDelegate {
    static let shared = StatusMenuController()

    var showWindow: (AppSection?) -> Void = { AppWindowController.shared.show(section: $0) }

    private let runner: MindRoomCommandRunner
    private let desktop: DesktopControlStore
    private let menu = NSMenu()
    private var statusItem: NSStatusItem?
    private var statusRefreshTimer: Timer?
    private var subscriptions = Set<AnyCancellable>()

    init(runner: MindRoomCommandRunner = .shared, desktop: DesktopControlStore = .shared) {
        self.runner = runner
        self.desktop = desktop
        super.init()
        menu.delegate = self
        menu.autoenablesItems = false
    }

    func start() {
        guard statusItem == nil else { return }
        let item = NSStatusBar.system.statusItem(withLength: NSStatusItem.variableLength)
        item.menu = menu
        item.button?.image = MindRoomBrand.menuImage
        item.button?.imagePosition = .imageOnly
        item.button?.setAccessibilityLabel("MindRoom")
        statusItem = item
        runner.objectWillChange.merge(with: desktop.objectWillChange)
            .receive(on: RunLoop.main)
            .sink { [weak self] _ in self?.refresh() }
            .store(in: &subscriptions)
        let timer = Timer(timeInterval: 5, repeats: true) { [weak self] _ in
            Task { @MainActor in self?.runner.refreshStatus() }
        }
        RunLoop.main.add(timer, forMode: .common)
        statusRefreshTimer = timer
        refresh()
    }

    func stop() {
        statusRefreshTimer?.invalidate()
        statusRefreshTimer = nil
        subscriptions.removeAll()
        if let statusItem { NSStatusBar.system.removeStatusItem(statusItem) }
        statusItem = nil
    }

    private func refresh() {
        statusItem?.button?.toolTip = "Local agents: \(runner.serviceStatus.state.shortTitle)\nComputer access: \(desktop.desktopStatusLabel)"
        menuNeedsUpdate(menu)
    }

    func menuNeedsUpdate(_ menu: NSMenu) {
        menu.removeAllItems()
        menu.addItem(.sectionHeader(title: "MindRoom"))
        menu.addItem(actionItem("Open MindRoom…", symbol: "macwindow", action: #selector(openWindow)))
        menu.addItem(actionItem("Open Chat", symbol: "bubble.left.and.bubble.right", action: #selector(openChat)))
        menu.addItem(.separator())
        menu.addItem(actionItem(
            "Local agents: \(runner.serviceStatus.state.shortTitle)…",
            symbol: "server.rack", action: #selector(openLocalAgents)
        ))
        if let title = runner.runningCommandTitle {
            menu.addItem(disabledItem("\(title)…"))
        } else if let feedback = runner.feedback, !feedback.result.isSuccess {
            menu.addItem(actionItem("Last Action Failed…", symbol: "exclamationmark.triangle", action: #selector(openLocalAgents)))
        }
        if !runner.serviceStatus.state.needsSetup, let action = runner.serviceStatus.state.primaryAction {
            let title = action == .stopService ? "Stop Local Agents" : "Start Local Agents"
            let item = actionItem(title, symbol: action == .stopService ? "stop.circle" : "play.circle", action: #selector(toggleLocalAgents))
            item.isEnabled = !runner.isRunningCommand
            menu.addItem(item)
        } else if !runner.serviceStatus.state.needsSetup {
            menu.addItem(actionItem("Refresh Local Agent Status", symbol: "arrow.clockwise", action: #selector(refreshStatus)))
        }
        menu.addItem(actionItem(
            "Computer access: \(desktop.desktopStatusLabel)…",
            symbol: "desktopcomputer", action: #selector(openComputerAccess)
        ))
        if desktop.status.authority.controlAvailable {
            menu.addItem(actionItem("Revoke Computer Control", symbol: "hand.raised", action: #selector(revokeComputerControl)))
        }
        if desktop.status.canStopBridge {
            menu.addItem(actionItem("Stop Computer Access", symbol: "stop.circle", action: #selector(stopComputerAccess)))
        }
        menu.addItem(.separator())
        menu.addItem(actionItem("Settings…", symbol: "gearshape", action: #selector(openSettings)))
        let quitItem = actionItem("Quit MindRoom", symbol: "power", action: #selector(quit))
        quitItem.toolTip = desktop.status.canStopBridge
            ? "Computer access stops; local agents keep running."
            : "Local agents keep running after quitting."
        menu.addItem(quitItem)
    }

    private func actionItem(_ title: String, symbol: String, action: Selector) -> NSMenuItem {
        let item = NSMenuItem(title: title, action: action, keyEquivalent: "")
        item.image = NSImage(systemSymbolName: symbol, accessibilityDescription: nil)
        item.target = self
        return item
    }

    private func disabledItem(_ title: String) -> NSMenuItem {
        let item = NSMenuItem(title: title, action: nil, keyEquivalent: "")
        item.isEnabled = false
        return item
    }

    @objc private func openWindow() { showWindow(nil) }
    @objc private func openLocalAgents() { showWindow(.localAgents) }
    @objc private func openComputerAccess() { showWindow(.computerAccess) }
    @objc private func openSettings() { showWindow(.settings) }
    @objc private func openChat() { runner.run(.openHostedChat) }
    @objc private func refreshStatus() { runner.refreshStatus() }
    @objc private func revokeComputerControl() { desktop.revokeControl() }
    @objc private func stopComputerAccess() { desktop.stop() }
    @objc private func toggleLocalAgents() {
        guard !runner.serviceStatus.state.needsSetup, let action = runner.serviceStatus.state.primaryAction else { return }
        runner.run(action)
    }
    @objc private func quit() { NSApp.terminate(nil) }
}
