import AppKit
import Foundation

@MainActor
final class StatusMenuController: NSObject, NSMenuDelegate {
    static let shared = StatusMenuController()

    private let runner = MindRoomCommandRunner.shared
    private let appUpdater = AppUpdater.shared
    private let loginItemController = LoginItemController.shared
    private let menu = NSMenu()
    private var statusItem: NSStatusItem?
    private var statusRefreshTimer: Timer?

    private override init() {
        super.init()
        menu.delegate = self
    }

    func start() {
        guard statusItem == nil else { return }
        let item = NSStatusBar.system.statusItem(withLength: NSStatusItem.variableLength)
        item.menu = menu
        statusItem = item
        rebuildMenu()
        refreshStatusIcon()
        startStatusRefreshTimer()
    }

    func stop() {
        statusRefreshTimer?.invalidate()
        statusRefreshTimer = nil
        if let statusItem {
            NSStatusBar.system.removeStatusItem(statusItem)
        }
        statusItem = nil
    }

    func menuNeedsUpdate(_ menu: NSMenu) {
        rebuildMenu()
        refreshStatusIcon()
    }

    private func rebuildMenu() {
        menu.removeAllItems()

        menu.addItem(disabledItem("Status: \(runner.serviceStatus.message)"))
        if runner.isRunningCommand {
            menu.addItem(disabledItem("Running command..."))
        }
        menu.addItem(.separator())

        menu.addItem(actionItem(MindRoomCommand.installRuntime.title, symbolName: "arrow.down.circle", action: #selector(installRuntime)))
        menu.addItem(actionItem(MindRoomCommand.updateRuntime.title, symbolName: "arrow.triangle.2.circlepath", action: #selector(updateRuntime)))
        menu.addItem(.separator())

        menu.addItem(actionItem(MindRoomCommand.installService.title, symbolName: "checkmark.circle", action: #selector(installService)))
        menu.addItem(actionItem(MindRoomCommand.startService.title, symbolName: "play.circle", action: #selector(startService)))
        menu.addItem(actionItem(MindRoomCommand.stopService.title, symbolName: "stop.circle", action: #selector(stopService)))
        menu.addItem(actionItem(MindRoomCommand.restartService.title, symbolName: "arrow.clockwise.circle", action: #selector(restartService)))
        menu.addItem(actionItem(MindRoomCommand.serviceStatus.title, symbolName: "waveform.path.ecg", action: #selector(refreshStatus)))
        menu.addItem(.separator())

        menu.addItem(actionItem(MindRoomCommand.initializeHostedConfig.title, symbolName: "person.2.wave.2", action: #selector(initializeHostedConfig)))
        menu.addItem(actionItem(MindRoomCommand.initializeSelfHostedConfig.title, symbolName: "server.rack", action: #selector(initializeSelfHostedConfig)))
        menu.addItem(actionItem(MindRoomCommand.localStackSetup.title, symbolName: "shippingbox", action: #selector(localStackSetup)))
        menu.addItem(actionItem(MindRoomCommand.pairHosted(pairCode: "").title, symbolName: "link", action: #selector(pairHosted)))
        menu.addItem(actionItem(MindRoomCommand.openHostedChat.title, symbolName: "safari", action: #selector(openHostedChat)))
        menu.addItem(.separator())

        menu.addItem(actionItem(MindRoomCommand.openDashboard.title, symbolName: "rectangle.3.group", action: #selector(openDashboard)))
        menu.addItem(actionItem(MindRoomCommand.openConfigFolder.title, symbolName: "folder", action: #selector(openConfigFolder)))
        menu.addItem(actionItem(MindRoomCommand.openLogsFolder.title, symbolName: "doc.text.magnifyingglass", action: #selector(openLogsFolder)))
        if !runner.lastOutput.isEmpty {
            menu.addItem(actionItem("Copy Last Output", symbolName: "doc.on.doc", action: #selector(copyLastOutput)))
        }
        menu.addItem(.separator())

        let loginItem = actionItem(loginItemController.menuTitle, symbolName: loginItemController.isEnabled ? "checkmark.circle" : "circle", action: #selector(toggleStartAtLogin))
        loginItem.isEnabled = loginItemController.canToggle
        menu.addItem(loginItem)

        let updateItem = actionItem("Check for App Updates...", symbolName: "arrow.down.circle", action: #selector(checkForUpdates))
        updateItem.isEnabled = appUpdater.canCheckForUpdates
        menu.addItem(updateItem)
        menu.addItem(.separator())
        menu.addItem(actionItem("Quit", symbolName: "power", action: #selector(quit)))
    }

    private func startStatusRefreshTimer() {
        guard statusRefreshTimer == nil else { return }
        let timer = Timer(timeInterval: 5, repeats: true) { _ in
            Task { @MainActor in
                MindRoomCommandRunner.shared.refreshStatus()
                self.refreshStatusIcon()
            }
        }
        RunLoop.main.add(timer, forMode: .common)
        statusRefreshTimer = timer
    }

    private func refreshStatusIcon() {
        guard let button = statusItem?.button else { return }
        button.image = NSImage(systemSymbolName: iconName, accessibilityDescription: "MindRoom")
        button.imagePosition = .imageOnly
        button.toolTip = runner.serviceStatus.message
    }

    private var iconName: String {
        switch runner.serviceStatus.state {
        case .running:
            return "brain.head.profile"
        case .stopped, .notInstalled:
            return "brain"
        case .runtimeMissing:
            return "exclamationmark.triangle"
        case .unknown:
            return "questionmark.circle"
        }
    }

    private func actionItem(_ title: String, symbolName: String, action: Selector) -> NSMenuItem {
        let item = NSMenuItem(title: title, action: action, keyEquivalent: "")
        item.target = self
        item.image = NSImage(systemSymbolName: symbolName, accessibilityDescription: nil)
        return item
    }

    private func disabledItem(_ title: String) -> NSMenuItem {
        let item = NSMenuItem(title: title, action: nil, keyEquivalent: "")
        item.isEnabled = false
        return item
    }

    @objc private func installRuntime() {
        runner.run(.installRuntime)
    }

    @objc private func updateRuntime() {
        runner.run(.updateRuntime)
    }

    @objc private func installService() {
        runner.run(.installService)
    }

    @objc private func startService() {
        runner.run(.startService)
    }

    @objc private func stopService() {
        runner.run(.stopService)
    }

    @objc private func restartService() {
        runner.run(.restartService)
    }

    @objc private func refreshStatus() {
        runner.refreshStatus()
    }

    @objc private func initializeHostedConfig() {
        runner.run(.initializeHostedConfig)
    }

    @objc private func initializeSelfHostedConfig() {
        runner.run(.initializeSelfHostedConfig)
    }

    @objc private func localStackSetup() {
        runner.run(.localStackSetup)
    }

    @objc private func pairHosted() {
        let alert = NSAlert()
        alert.messageText = "Pair Hosted MindRoom"
        alert.informativeText = "Enter the pair code from chat.mindroom.chat."
        let textField = NSTextField(frame: NSRect(x: 0, y: 0, width: 220, height: 24))
        textField.placeholderString = "ABCD-EFGH"
        alert.accessoryView = textField
        alert.addButton(withTitle: "Pair")
        alert.addButton(withTitle: "Cancel")

        guard alert.runModal() == .alertFirstButtonReturn else { return }
        runner.run(.pairHosted(pairCode: textField.stringValue))
    }

    @objc private func openDashboard() {
        runner.run(.openDashboard)
    }

    @objc private func openHostedChat() {
        runner.run(.openHostedChat)
    }

    @objc private func openConfigFolder() {
        runner.run(.openConfigFolder)
    }

    @objc private func openLogsFolder() {
        runner.run(.openLogsFolder)
    }

    @objc private func copyLastOutput() {
        NSPasteboard.general.clearContents()
        NSPasteboard.general.setString(runner.lastOutput, forType: .string)
    }

    @objc private func toggleStartAtLogin() {
        loginItemController.toggle()
        rebuildMenu()
    }

    @objc private func checkForUpdates() {
        appUpdater.checkForUpdates()
    }

    @objc private func quit() {
        NSApp.terminate(nil)
    }
}
