import AppKit
import Combine

@MainActor
final class StatusMenuController: NSObject, NSMenuDelegate {
    static let shared = StatusMenuController()

    var showWindow: (AppSection?) -> Void = { AppWindowController.shared.show(section: $0) }

    private let runner = MindRoomCommandRunner.shared
    private let desktop = DesktopControlStore.shared
    private let menu = NSMenu()
    private var statusItem: NSStatusItem?
    private var statusRefreshTimer: Timer?
    private var subscriptions = Set<AnyCancellable>()

    private override init() {
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

    func menuNeedsUpdate(_ menu: NSMenu) { refresh() }

    private func refresh() {
        statusItem?.button?.toolTip = "Local agents: \(runner.serviceStatus.state.shortTitle)\nComputer access: \(desktop.desktopStatusLabel)"
        menu.removeAllItems()
        menu.addItem(disabledItem("MindRoom"))
        menu.addItem(disabledItem("Local agents: \(runner.serviceStatus.state.shortTitle)"))
        menu.addItem(disabledItem("Computer access: \(desktop.desktopStatusLabel)"))
        if let title = runner.runningCommandTitle {
            menu.addItem(disabledItem("\(title)…"))
        } else if let feedback = runner.feedback, !feedback.result.isSuccess {
            menu.addItem(actionItem("Last Action Failed — View Details…", action: #selector(openLocalAgents)))
        }
        menu.addItem(.separator())
        menu.addItem(actionItem("Open MindRoom…", action: #selector(openWindow)))
        menu.addItem(actionItem("Open Chat", action: #selector(openChat)))
        menu.addItem(.separator())
        if runner.serviceStatus.state.needsSetup {
            menu.addItem(actionItem("Set Up Local Agents…", action: #selector(openLocalAgents)))
        } else if let action = runner.serviceStatus.state.primaryAction {
            let title = action == .stopService ? "Stop Local Agents" : "Start Local Agents"
            let item = actionItem(title, action: #selector(toggleLocalAgents))
            item.isEnabled = !runner.isRunningCommand
            menu.addItem(item)
        } else {
            menu.addItem(actionItem("Refresh Local Agent Status", action: #selector(refreshStatus)))
        }
        if desktop.status.authority.controlAvailable {
            menu.addItem(actionItem("Revoke Computer Control", action: #selector(revokeComputerControl)))
        }
        if desktop.status.canStopBridge {
            menu.addItem(actionItem("Stop Computer Access", action: #selector(stopComputerAccess)))
        }
        menu.addItem(actionItem("Computer Access…", action: #selector(openComputerAccess)))
        menu.addItem(.separator())
        menu.addItem(actionItem("Settings…", action: #selector(openSettings)))
        menu.addItem(actionItem("Quit MindRoom App", action: #selector(quit)))
        menu.addItem(disabledItem(desktop.status.canStopBridge
                                  ? "Computer access stops; local agents keep running."
                                  : "Local agents keep running after quitting."))
    }

    private func actionItem(_ title: String, action: Selector) -> NSMenuItem {
        let item = NSMenuItem(title: title, action: action, keyEquivalent: "")
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
