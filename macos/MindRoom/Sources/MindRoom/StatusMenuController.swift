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
        menu.autoenablesItems = false
    }

    func start() {
        guard statusItem == nil else { return }
        runner.onCommandFinished = { [weak self] command, result in
            self?.showCommandResult(command, result: result)
        }
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
        if let runningTitle = runner.runningCommandTitle {
            menu.addItem(disabledItem("Running \(runningTitle)..."))
        }
        menu.addItem(.separator())

        menu.addItem(disabledItem("Set Up Hosted MindRoom"))
        menu.addItem(actionItem(
            "1. \(MindRoomCommand.installRuntime.title)",
            symbolName: "arrow.down.circle",
            action: #selector(installRuntime),
            toolTip: "Installs the mindroom CLI with the bundled uv."
        ))
        menu.addItem(actionItem(
            "2. \(MindRoomCommand.initializeHostedConfig.title)",
            symbolName: "person.2.wave.2",
            action: #selector(initializeHostedConfig),
            toolTip: "Writes config.yaml and .env to ~/.mindroom for the hosted chat.mindroom.chat Matrix server. Existing files are kept unchanged."
        ))
        menu.addItem(actionItem(
            "3. \(MindRoomCommand.openHostedChat.title)",
            symbolName: "safari",
            action: #selector(openHostedChat),
            toolTip: "Sign in to create your hosted account, then click the Local MindRoom icon in the sidebar to generate a pair code."
        ))
        menu.addItem(actionItem(
            "4. \(MindRoomCommand.pairHosted(pairCode: "").title)",
            symbolName: "link",
            action: #selector(pairHosted),
            toolTip: "Links this Mac to your hosted account using the pair code."
        ))
        menu.addItem(actionItem(
            "5. \(MindRoomCommand.installService.title)",
            symbolName: "checkmark.circle",
            action: #selector(installService),
            toolTip: "Installs and starts the MindRoom background service (launchd)."
        ))
        menu.addItem(.separator())

        menu.addItem(actionItem(MindRoomCommand.startService.title, symbolName: "play.circle", action: #selector(startService)))
        menu.addItem(actionItem(MindRoomCommand.stopService.title, symbolName: "stop.circle", action: #selector(stopService)))
        menu.addItem(actionItem(MindRoomCommand.restartService.title, symbolName: "arrow.clockwise.circle", action: #selector(restartService)))
        menu.addItem(actionItem(MindRoomCommand.serviceStatus.title, symbolName: "waveform.path.ecg", action: #selector(refreshStatus)))
        menu.addItem(.separator())

        menu.addItem(actionItem(
            MindRoomCommand.openDashboard.title,
            symbolName: "rectangle.3.group",
            action: #selector(openDashboard),
            toolTip: "Opens the local dashboard at http://localhost:8765, served by the MindRoom service."
        ))
        menu.addItem(actionItem(MindRoomCommand.openConfigFolder.title, symbolName: "folder", action: #selector(openConfigFolder)))
        menu.addItem(actionItem(MindRoomCommand.openLogsFolder.title, symbolName: "doc.text.magnifyingglass", action: #selector(openLogsFolder)))
        if !runner.lastOutput.isEmpty {
            menu.addItem(actionItem("Copy Last Output", symbolName: "doc.on.doc", action: #selector(copyLastOutput)))
        }
        menu.addItem(.separator())

        let otherSetup = NSMenuItem(title: "Other Setup", action: nil, keyEquivalent: "")
        let otherSetupMenu = NSMenu()
        otherSetupMenu.autoenablesItems = false
        otherSetupMenu.addItem(actionItem(
            MindRoomCommand.initializeSelfHostedConfig.title,
            symbolName: "server.rack",
            action: #selector(initializeSelfHostedConfig),
            toolTip: "Writes config.yaml and .env to ~/.mindroom for connecting to your own Matrix homeserver."
        ))
        otherSetupMenu.addItem(actionItem(MindRoomCommand.localStackSetup.title, symbolName: "shippingbox", action: #selector(localStackSetup)))
        otherSetup.submenu = otherSetupMenu
        menu.addItem(otherSetup)
        menu.addItem(actionItem(MindRoomCommand.updateRuntime.title, symbolName: "arrow.triangle.2.circlepath", action: #selector(updateRuntime)))
        menu.addItem(.separator())

        let loginItem = actionItem(loginItemController.menuTitle, symbolName: loginItemController.isEnabled ? "checkmark.circle" : "circle", action: #selector(toggleStartAtLogin))
        loginItem.isEnabled = loginItemController.canToggle
        menu.addItem(loginItem)

        let updateItem = actionItem("Check for App Updates...", symbolName: "arrow.down.circle", action: #selector(checkForUpdates))
        updateItem.isEnabled = appUpdater.canCheckForUpdates
        menu.addItem(updateItem)
        menu.addItem(.separator())
        menu.addItem(actionItem("Quit", symbolName: "power", action: #selector(quit)))

        if runner.isRunningCommand {
            disableRuntimeCommandItems(in: menu)
        }
    }

    /// Only one runtime command runs at a time, so gray the triggers out while one is in flight.
    private func disableRuntimeCommandItems(in menu: NSMenu) {
        let runtimeSelectors: Set<Selector> = [
            #selector(installRuntime), #selector(updateRuntime), #selector(installService),
            #selector(startService), #selector(stopService), #selector(restartService),
            #selector(initializeHostedConfig), #selector(initializeSelfHostedConfig),
            #selector(localStackSetup), #selector(pairHosted),
        ]
        for item in menu.items {
            if let submenu = item.submenu {
                disableRuntimeCommandItems(in: submenu)
            }
            if let action = item.action, runtimeSelectors.contains(action) {
                item.isEnabled = false
            }
        }
    }

    private func startStatusRefreshTimer() {
        guard statusRefreshTimer == nil else { return }
        let runner = runner
        let timer = Timer(timeInterval: 5, repeats: true) { [weak self] _ in
            Task { @MainActor in
                runner.refreshStatus()
                self?.refreshStatusIcon()
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

    private func actionItem(_ title: String, symbolName: String, action: Selector, toolTip: String? = nil) -> NSMenuItem {
        let item = NSMenuItem(title: title, action: action, keyEquivalent: "")
        item.target = self
        item.image = NSImage(systemSymbolName: symbolName, accessibilityDescription: nil)
        item.toolTip = toolTip
        return item
    }

    private func disabledItem(_ title: String) -> NSMenuItem {
        let item = NSMenuItem(title: title, action: nil, keyEquivalent: "")
        item.isEnabled = false
        return item
    }

    private func showCommandResult(_ command: MindRoomCommand, result: CommandResult) {
        NSApp.activate(ignoringOtherApps: true)
        let alert = NSAlert()
        if result.isSuccess {
            alert.alertStyle = .informational
            alert.messageText = "\(command.title) Finished"
            alert.informativeText = command.successMessage ?? result.condensedOutput
            alert.addButton(withTitle: "OK")
            alert.runModal()
            return
        }
        alert.alertStyle = .warning
        alert.messageText = "\(command.title) Failed"
        let output = result.condensedOutput
        var informativeText = output.isEmpty ? "The command exited with code \(result.exitCode)." : output
        if output.contains("No such option") {
            informativeText += "\n\nThe installed MindRoom runtime is older than this app. Use Update MindRoom Runtime, then try again."
        }
        alert.informativeText = informativeText
        alert.addButton(withTitle: "OK")
        alert.addButton(withTitle: "Copy Output")
        if alert.runModal() == .alertSecondButtonReturn {
            copyLastOutput()
        }
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
        NSApp.activate(ignoringOtherApps: true)
        let alert = NSAlert()
        alert.messageText = "Pair Hosted MindRoom"
        alert.informativeText = "In chat.mindroom.chat, click the Local MindRoom icon in the left sidebar to generate a pair code, then enter it here."
        let textField = NSTextField(frame: NSRect(x: 0, y: 0, width: 220, height: 24))
        textField.placeholderString = "ABCD-EFGH"
        alert.accessoryView = textField
        alert.addButton(withTitle: "Pair")
        alert.addButton(withTitle: "Cancel")

        guard alert.runModal() == .alertFirstButtonReturn else { return }
        let pairCode = textField.stringValue.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !pairCode.isEmpty else {
            showSimpleAlert(title: "Pair Code Missing", message: "Enter the pair code from chat.mindroom.chat to pair.")
            return
        }
        runner.run(.pairHosted(pairCode: pairCode))
    }

    @objc private func openDashboard() {
        let state = runner.serviceStatus.state
        guard state == .stopped || state == .notInstalled || state == .runtimeMissing else {
            runner.run(.openDashboard)
            return
        }

        NSApp.activate(ignoringOtherApps: true)
        let alert = NSAlert()
        alert.alertStyle = .warning
        alert.messageText = "MindRoom Is Not Running"
        let fix: (title: String, command: MindRoomCommand)
        switch state {
        case .stopped:
            alert.informativeText = "The dashboard at http://localhost:8765 is served by the MindRoom service, which is installed but stopped."
            fix = ("Start Service", .startService)
        case .notInstalled:
            alert.informativeText = "The dashboard at http://localhost:8765 is served by the MindRoom service, which is not installed yet. Follow the Set Up Hosted MindRoom steps in the menu."
            fix = ("Install Service", .installService)
        default:
            alert.informativeText = "The dashboard at http://localhost:8765 is served by the MindRoom service, but the MindRoom runtime is not installed yet. Follow the Set Up Hosted MindRoom steps in the menu."
            fix = ("Install Runtime", .installRuntime)
        }
        alert.addButton(withTitle: fix.title)
        alert.addButton(withTitle: "Open Anyway")
        alert.addButton(withTitle: "Cancel")

        switch alert.runModal() {
        case .alertFirstButtonReturn:
            runner.run(fix.command)
        case .alertSecondButtonReturn:
            runner.run(.openDashboard)
        default:
            break
        }
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
        do {
            try loginItemController.toggle()
        } catch {
            showSimpleAlert(title: "Start at Login Failed", message: error.localizedDescription)
        }
        rebuildMenu()
    }

    @objc private func checkForUpdates() {
        do {
            try appUpdater.checkForUpdates()
        } catch {
            showSimpleAlert(title: "App Update Check Failed", message: error.localizedDescription)
        }
    }

    private func showSimpleAlert(title: String, message: String) {
        NSApp.activate(ignoringOtherApps: true)
        let alert = NSAlert()
        alert.messageText = title
        alert.informativeText = message
        alert.addButton(withTitle: "OK")
        alert.runModal()
    }

    @objc private func quit() {
        NSApp.terminate(nil)
    }
}
